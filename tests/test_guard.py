"""The session guard's allow/deny matrix (register R-AN-85 "Tests", §16.18 A7 decisions 4, 5, 7 and 20).

Every case runs twice: in-process on the decision (`check`, the module executed from the copy inside the test root,
so that its own place is checked), and as a subprocess exactly as Claude Code runs the hook: the command of
`.claude/settings.json` through /bin/sh, the event on stdin, CLAUDE_PROJECT_DIR in the environment. A refusal must
print exactly the compact JSON line and the message and exit 2; an allowed call prints nothing and exits 0.
Second review of A7 (decision 20): a Bash `timeout` below the settings' 3600000 ms is refused `bash_option`, since
it would let an allowed command move to the background.
"""
import ast
import json
import shutil
from pathlib import Path

import pytest

from guardkit import B, DRAFTS, GUARD, LINES, data, event, load, make_root, run_hook

MSG = {
    "input": "the tool call could not be checked, so it is refused.",
    "error": "the guard could not complete its checks, so the call is refused.",
    "tool": "only Bash, Read and Write are available in this session, as analyst/ROUTINE_PROMPT.md says. Do not try "
            "another way.",
    "bash_form": "run only the commands written in analyst/ROUTINE_PROMPT.md, each exactly as written and on its "
                 "own: no cd, nothing before or after it. Do not try another way.",
    "bash_option": "run the command in the foreground, with no option other than a description, and leave out the "
                   "timeout (one below 3600000 ms is refused).",
    "bash_cwd": "commands run only from the repository root.",
    "read_path": "read only files inside this repository, outside .git, by their absolute paths.",
    "write_path": "write only the one file your step names, .analyst_run/plan.json, .analyst_run/lesson.json or "
                  ".analyst_run/playbook.md under the repository root, by its absolute path; to correct it, write "
                  "the whole file again.",
    "write_size": "the file is larger than 4 MiB.",
}
MIB4 = 4194304


def refusal_out(code):
    m = f"guard: {code}: {MSG[code]}"
    return ('{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny",'
            '"permissionDecisionReason":' + json.dumps(m) + "}}\n"), m + "\n"


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    r, d0 = make_root(tmp_path_factory.mktemp("guard"))
    return r, d0, load(Path(r) / ".claude" / "hooks" / "guard.py")


def assert_allowed(g, d0, payload):
    assert g.check(data(payload), d0) is None
    assert run_hook(d0, payload) == (0, "", "")


def assert_refused(g, d0, payload, code, **kw):
    assert g.check(data(payload), d0) == code
    out, err = refusal_out(code)
    assert run_hook(d0, payload, **kw) == (2, out, err)


# ---------------------------------------------------------------- the cases (d: the root the paths are formed under)
def bash(cmd, **kw):
    return lambda d: event("Bash", dict(command=cmd, **kw), cwd=d)


def read(rel):
    return lambda d: event("Read", {"file_path": f"{d}/{rel}"})


def read_at(path):
    return lambda d: event("Read", {"file_path": path})


def write(rel, content="{}\n"):
    return lambda d: event("Write", {"file_path": f"{d}/{rel}", "content": content})


def write_at(path, content="{}\n"):
    return lambda d: event("Write", {"file_path": path, "content": content})


def tool(name):
    return lambda d: event(name, {"file_path": f"{d}/analyst/ROUTINE_PROMPT.md", "command": B + "start"}, cwd=d)


def raw(b):
    return lambda d: b


ALLOWED = (
    [(f"line:{l[len(B):]}", bash(l)) for l in LINES]
    + [(f"spaces:{l[len(B):]}", bash("  " + l + " ")) for l in LINES]
    + [("bash_ignored_fields", bash(LINES[0], description="Start the window", timeout=3600000,
                                    run_in_background=False, dangerouslyDisableSandbox=None))]
    + [(f"timeout_ok:{v!r}", bash(LINES[1], timeout=v)) for v in (None, 3600000, 3600000.0, 7200000)]
    + [(f"read:{p}", read(p)) for p in ("analyst/ROUTINE_PROMPT.md", "analyst/prompts/system.md",
                                         ".analyst_run/context.json", ".claude/settings.json", ".gitignore")]
    + [("read_extra_keys", lambda d: event("Read", {"file_path": f"{d}/analyst/ROUTINE_PROMPT.md", "offset": 1,
                                                    "limit": 20, "pages": "1"}))]
    + [(f"write:{f}", write(f".analyst_run/{f}")) for f in DRAFTS]
    + [("write_4mib", write(".analyst_run/playbook.md", "x" * MIB4))]
)

REFUSED = [
    # bash_form: every shell construct, another interpreter or flag, a cd prefix, any other argument
    ("then_gh", bash(B + "start; gh api repos/Mohamedalhaaj/gold-analyst-inbox/hooks"), "bash_form"),
    ("and_curl", bash(B + "start && curl https://example.com"), "bash_form"),
    ("pipe_sh", bash(B + "start | sh"), "bash_form"),
    ("newline", bash(B + "start\ngh api repos/Mohamedalhaaj/gold-analyst-inbox/hooks"), "bash_form"),
    ("subst", bash(B + "lock $(echo .analyst_run/plan.json)"), "bash_form"),
    ("backquotes", bash(B + "lock `echo .analyst_run/plan.json`"), "bash_form"),
    ("env_prefix", bash("FOO=1 " + B + "start"), "bash_form"),
    ("env_cmd", bash("env " + B + "start"), "bash_form"),
    ("python_c", bash("python3 -c 'print(1)'"), "bash_form"),
    ("python_I", bash("python3 -I analyst/run_window.py start"), "bash_form"),
    ("python2", bash("python analyst/run_window.py start"), "bash_form"),
    ("dot_slash", bash("./analyst/run_window.py start"), "bash_form"),
    ("two_spaces", bash("python3  analyst/run_window.py start"), "bash_form"),
    ("tab_end", bash(B + "start\t"), "bash_form"),
    ("quoted", bash(B + '"start"'), "bash_form"),
    ("glob", bash(B + "st*"), "bash_form"),
    ("traversal", bash(B + "lock ../../x"), "bash_form"),
    ("exam", bash(B + "lock --exam .analyst_run/plan.json"), "bash_form"),
    ("cd_prefix", lambda d: event("Bash", {"command": f"cd {d} && {B}start"}, cwd=d), "bash_form"),
    ("gh", bash("gh api repos/Mohamedalhaaj/gold-analyst-inbox/hooks"), "bash_form"),
    ("git", bash("git status"), "bash_form"),
    ("curl", bash("curl https://example.com"), "bash_form"),
    ("redirect", bash(B + "start > /tmp/x"), "bash_form"),
    ("ampersand", bash(B + "start &"), "bash_form"),
    ("one_mib", bash(B + "start " + "a" * (1 << 20)), "bash_form"),
    ("empty_command", bash(""), "bash_form"),
    # bash_option, bash_cwd
    ("background", bash(LINES[0], run_in_background=True), "bash_option"),
    ("no_sandbox", bash(LINES[0], dangerouslyDisableSandbox=True), "bash_option"),
    ("background_zero", bash(LINES[0], run_in_background=0), "bash_option"),
    ("unknown_key", bash(LINES[0], sandbox=False), "bash_option"),
    *[(f"timeout:{v!r}", bash(LINES[1], timeout=v), "bash_option") for v in (
        1000, 120000, 600000, 3599999, 3599999.5, 0, -1, True, False, "3600000", [3600000], {"ms": 3600000})],
    ("cwd_other", lambda d: event("Bash", {"command": LINES[0]}, cwd=f"{d}/analyst"), "bash_cwd"),
    ("cwd_missing", lambda d: event("Bash", {"command": LINES[0]}), "bash_cwd"),
    ("cwd_relative", lambda d: event("Bash", {"command": LINES[0]}, cwd="."), "bash_cwd"),
    # write_path, write_size
    *[(f"write:{p}", write(p), "write_path") for p in (
        ".claude/settings.json", ".claude/settings.local.json", ".claude/hooks/guard.py", "analyst/ga/x.py",
        "analyst/run_window.py", ".github/workflows/x.yml", ".git/config", ".mcp.json", "CLAUDE.md",
        ".analyst_run/../analyst/x.py", ".analyst_run/./plan.json", ".analyst_run/context.json",
        ".analyst_run/plan.json.bak", ".analyst_run//plan.json")],
    ("write_relative", write_at(".analyst_run/plan.json"), "write_path"),
    ("write_tmp", write_at("/tmp/.analyst_run/plan.json"), "write_path"),
    ("write_size", write(".analyst_run/plan.json", "x" * (MIB4 + 1)), "write_size"),
    ("write_size_utf8", write(".analyst_run/lesson.json", "م" * (MIB4 // 2 + 1)), "write_size"),
    # read_path
    ("read_etc", read_at("/etc/passwd"), "read_path"),
    ("read_relative", read_at("analyst/ROUTINE_PROMPT.md"), "read_path"),
    ("read_dotdot", read("analyst/../analyst/ROUTINE_PROMPT.md"), "read_path"),
    ("read_git", read(".git/config"), "read_path"),
    ("read_link_out", read("analyst/outside/hosts"), "read_path"),
    ("read_newline", read("analyst/ROUTINE_PROMPT.md\n"), "read_path"),
    ("read_empty", read_at(""), "read_path"),
    # tool
    *[(f"tool:{t}", tool(t), "tool") for t in (
        "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep", "TodoWrite", "WebFetch", "WebSearch", "Agent", "Task",
        "Skill", "Monitor", "PowerShell", "RemoteTrigger", "Artifact", "BashOutput", "KillShell",
        "mcp__github__get_file_contents", "mcp__claude_ai_Gmail__send", "bash", "")],
    # input
    ("stdin_empty", raw(b""), "input"),
    ("stdin_not_json", raw(b"not json"), "input"),
    ("stdin_bad_utf8", raw(b'{"a": "\xff"}'), "input"),
    ("stdin_list", raw(b"[]"), "input"),
    ("stdin_repeated_key", raw(b'{"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_name": "Read", '
                               b'"tool_input": {}}'), "input"),
    ("stdin_nan", raw(b'{"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {"x": NaN}}'), "input"),
    ("stdin_over_8mib", raw(b" " * 8388609), "input"),
    ("no_tool_input", lambda d: {k: v for k, v in event("Bash", {}).items() if k != "tool_input"}, "input"),
    ("tool_input_list", lambda d: event("Bash", [LINES[0]], cwd=d), "input"),
    ("command_not_str", lambda d: event("Bash", {"command": ["python3"]}, cwd=d), "input"),
    ("read_path_not_str", lambda d: event("Read", {"file_path": None}), "input"),
    ("write_path_not_str", lambda d: event("Write", {"file_path": 1, "content": ""}), "input"),
    ("write_content_not_str", lambda d: event("Write", {"file_path": f"{d}/.analyst_run/plan.json"}), "input"),
    ("other_event", lambda d: event("Bash", {"command": LINES[0]}, cwd=d, hook_event_name="PostToolUse"), "input"),
    ("tool_name_not_str", lambda d: event(None, {"command": LINES[0]}, cwd=d), "input"),
]


def test_the_matrix_has_unique_names():
    names = [c[0] for c in ALLOWED] + [c[0] for c in REFUSED]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("base", ["D0", "R"])
@pytest.mark.parametrize("name,build", ALLOWED, ids=[c[0] for c in ALLOWED])
def test_allowed(world, name, build, base):
    r, d0, g = world
    assert_allowed(g, d0, build(d0 if base == "D0" else r))


@pytest.mark.parametrize("name,build,code", REFUSED, ids=[c[0] for c in REFUSED])
def test_refused(world, name, build, code):
    _r, d0, g = world
    assert_refused(g, d0, build(d0), code)


@pytest.mark.parametrize("env", ["R", "R/"])
def test_the_real_path_and_a_trailing_slash_as_the_project_dir(world, env):
    r, _d0, g = world
    root = r if env == "R" else r + "/"
    for payload in (bash(LINES[2])(r), read("analyst/ROUTINE_PROMPT.md")(r), write(".analyst_run/plan.json")(r)):
        assert_allowed(g, root, payload)


# ---------------------------------------------------------------- the file-system rules
def fresh(tmp_path):
    r, d0 = make_root(tmp_path)
    return Path(r), d0, load(Path(r) / ".claude" / "hooks" / "guard.py")


def test_writes_are_allowed_with_the_directory_absent(tmp_path):
    r, d0, g = fresh(tmp_path)
    shutil.rmtree(r / ".analyst_run")
    for f in DRAFTS:
        assert_allowed(g, d0, write(f".analyst_run/{f}")(d0))
        assert_allowed(g, d0, write(f".analyst_run/{f}")(str(r)))


def test_a_draft_dir_linked_to_analyst_is_refused(tmp_path):
    r, d0, g = fresh(tmp_path)
    shutil.rmtree(r / ".analyst_run")
    (r / ".analyst_run").symlink_to(r / "analyst")
    assert_refused(g, d0, write(".analyst_run/plan.json")(d0), "write_path")


def test_a_draft_dir_that_is_a_file_is_refused(tmp_path):
    r, d0, g = fresh(tmp_path)
    shutil.rmtree(r / ".analyst_run")
    (r / ".analyst_run").write_text("x")
    assert_refused(g, d0, write(".analyst_run/plan.json")(d0), "write_path")


@pytest.mark.parametrize("kind", ["symlink", "dangling_symlink", "directory"])
def test_a_draft_file_that_is_not_a_regular_file_is_refused(tmp_path, kind):
    r, d0, g = fresh(tmp_path)
    p = r / ".analyst_run" / "plan.json"
    if kind == "symlink":
        p.symlink_to(r / "analyst" / "ga" / "session.py")
    elif kind == "dangling_symlink":
        p.symlink_to(r / "analyst" / "ga" / "new.py")
    else:
        p.mkdir()
    assert_refused(g, d0, write(".analyst_run/plan.json")(d0), "write_path")


def test_an_existing_regular_draft_file_may_be_written_again(tmp_path):
    r, d0, g = fresh(tmp_path)
    (r / ".analyst_run" / "plan.json").write_text("{}\n")
    assert_allowed(g, d0, write(".analyst_run/plan.json", "{ }\n")(d0))


def test_a_read_through_a_link_to_git_is_refused(tmp_path):
    r, d0, g = fresh(tmp_path)
    (r / "analyst" / "cfg").symlink_to(r / ".git" / "config")
    assert_refused(g, d0, read("analyst/cfg")(d0), "read_path")


# ---------------------------------------------------------------- error: the guard cannot check
def test_project_dir_unset_relative_or_not_a_directory(tmp_path):
    r, _d0, g = fresh(tmp_path)
    script = r / ".claude" / "hooks" / "guard.py"
    payload = bash(LINES[0])(str(r))
    out, err = refusal_out("error")
    for env, cwd in ((None, str(r)), ("repo", str(r.parent)), (str(r / "analyst" / "run_window.py"), str(r)),
                     ("", str(r)), ("/", str(r))):
        assert g.check(data(payload), env) == "error", env
        assert run_hook(env, payload, script=script, cwd=cwd) == (2, out, err), env


def test_a_guard_run_from_a_copy_outside_the_root(tmp_path):
    r, _d0, _g = fresh(tmp_path)
    other = tmp_path / "elsewhere" / "guard.py"
    other.parent.mkdir()
    shutil.copyfile(r / ".claude" / "hooks" / "guard.py", other)
    payload = bash(LINES[0])(str(r))
    assert load(other).check(data(payload), str(r)) == "error"
    out, err = refusal_out("error")
    assert run_hook(str(r), payload, script=other) == (2, out, err)


def test_an_exception_inside_the_decision_refuses(world, monkeypatch):
    r, d0, g = world

    def boom(*a):
        raise RuntimeError("forced")
    monkeypatch.setattr(g, "_inside", boom)
    assert g.check(data(read("analyst/ROUTINE_PROMPT.md")(d0)), d0) == "error"
    monkeypatch.undo()
    lone = json.dumps(write(".analyst_run/plan.json", "\ud800")(d0)).encode()   # an escaped lone surrogate:
    # valid JSON, but the content cannot be encoded as UTF-8, so the size check raises
    assert g.check(data(lone), d0) == "error"
    out, err = refusal_out("error")
    assert run_hook(d0, lone) == (2, out, err)


# ---------------------------------------------------------------- the module itself
def test_the_guard_imports_only_json_os_stat_and_sys():
    tree = ast.parse(GUARD.read_text("utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mods.add("." * node.level + (node.module or ""))
    assert mods == {"json", "os", "stat", "sys"}


def test_the_guard_ignores_pythonpath_and_its_own_directory(tmp_path):
    """`-I`: no PYTHON* variable, no user site, no script directory on the path. A planted json.py beside the guard
    and on PYTHONPATH is never imported."""
    r, d0, _g = fresh(tmp_path)
    poison = "raise SystemExit(0)\n"
    (r / ".claude" / "hooks" / "json.py").write_text(poison)
    (tmp_path / "pp").mkdir()
    (tmp_path / "pp" / "json.py").write_text(poison)
    (tmp_path / "pp" / "os.py").write_text(poison)
    env = {"PYTHONPATH": str(tmp_path / "pp"), "PYTHONSTARTUP": str(tmp_path / "pp" / "os.py")}
    assert run_hook(d0, bash(LINES[0])(d0), extra_env=env) == (0, "", "")
    out, err = refusal_out("bash_form")
    assert run_hook(d0, bash("git status")(d0), extra_env=env) == (2, out, err)
    assert run_hook(d0, bash("git status")(d0), extra_env={"PYTHONPATH": ""}) == (2, out, err)


def test_the_messages_are_the_registers_and_ascii():
    g = load()
    assert g.MESSAGES == MSG
    for code in MSG:
        assert g.message(code).isascii() and g.message(code) == f"guard: {code}: {MSG[code]}"
    assert sorted(g.LINES) == sorted(LINES)


# ---------------------------------------------------------------- the string rules alone (the run-log scan, R-AN-86)
def test_the_string_rules_need_no_file_system():
    g = load()
    p = "/home/user/gold-analyst-inbox"
    assert g.decide(event("Bash", {"command": LINES[0]}), p, fs=False) is None              # no cwd needed
    assert g.decide(event("Bash", {"command": LINES[0]}), None, fs=False) is None
    assert g.decide(event("Bash", {"command": "git status"}), None, fs=False) == "bash_form"
    assert g.decide(event("Bash", {"command": LINES[1], "timeout": 1000}), None, fs=False) == "bash_option"
    assert g.decide(event("Bash", {"command": LINES[1], "timeout": 3600000}), None, fs=False) is None
    assert g.decide(event("Read", {"file_path": p + "/analyst/x.md"}), p, fs=False) is None
    assert g.decide(event("Read", {"file_path": p + "/.git/config"}), p, fs=False) == "read_path"
    assert g.decide(event("Read", {"file_path": p + "/analyst/x.md"}), None, fs=False) == "read_path"
    w = p + "/.analyst_run/plan.json"
    big = "sha256:" + "a" * 64 + f" bytes:{MIB4 + 1}"
    assert g.decide(event("Write", {"file_path": w, "content": "sha256:" + "a" * 64 + " bytes:12"}), p,
                    fs=False) is None
    assert g.decide(event("Write", {"file_path": w, "content": big}), p, fs=False) == "write_size"
    assert g.decide(event("Write", {"file_path": w, "content": big}), p) == "error"   # fs: p is not here
    assert g.decide(event("Write", {"file_path": p + "/analyst/ga/x.py", "content": ""}), p, fs=False) == \
        "write_path"
    assert g.decide(event("WebFetch", {"url": "https://example.com"}), p, fs=False) == "tool"
