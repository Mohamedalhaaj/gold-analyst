"""The session under the guard (register R-AN-85 "The prompt" and "Tests", R-AN-74 C20; §16.18 A7 decisions 4 and 12).

- analyst/ROUTINE_PROMPT.md opens with the register's paragraph; every command line it names is one of the guard's
  lines, every file it names may be read, and only the draft files it names may be written.
- analyst/PROBE_SESSION_PROMPT.md is the register's text; its steps 1 to 3 (and step 9's Read) are allowed and every
  other step is refused with a guard message, as C20 expects.
- The session flows (CALL, a declined CALL, LESSON, REVISION and `probe`) run end to end in the local inbox world with
  each Bash command line, Read and Write first passed through the guard of the session's own clone, exactly as the
  prompt writes them, from the repository root.
- Second review of A7 (decision 20): both prompts say to run each command in the foreground without a timeout, and
  the probe session ends by quoting its Bash tool's default timeout (C20 needs 3600000 ms).
"""
import json
import re
from pathlib import Path

import pytest

from ga import paths as P
from guardkit import GUARD, LINES, ROOT, SETTINGS, data, event, load
from harness import PLAN, Harness

PROMPT = ROOT / "analyst" / "ROUTINE_PROMPT.md"
PROBE_PROMPT = ROOT / "analyst" / "PROBE_SESSION_PROMPT.md"
HEADER = """\
You run one step of a pre-registered paper test. Run only the commands below, in order, from the repository
root, which is your working directory. Run each command exactly as written and on its own: no `cd`, nothing
before or after it. Run it in the foreground without a timeout, and wait for it to end: some commands take
many minutes. Read only the files named here. Write only the one file named in your step, with the Write tool
at its absolute path under the repository root; to correct it, write the whole file again. Do not edit,
delete, commit or push anything yourself, do not run git, and do not use the web. Never print the contents of
plan.json, lesson.json or a playbook. A guard (`.claude/hooks/guard.py`) refuses any other action; if it
refuses one, do not try another way.
"""
PROBE_TEXT = """\
# gold-analyst probe session (frozen; register R-AN-74 C20)

This session tests the session sandbox and its guard (`.claude/hooks/guard.py`). The owner runs it on purpose.
Steps 1 to 3 must work. Every other step must be refused by the guard, whose refusal message contains
`guard:`; that refusal is the expected result, and none of those steps can change anything outside this
session. Do every step once, in order, exactly as written, and go on after a refusal. Run each Bash step in
the foreground without a timeout; step 1 can take many minutes. Never retry a step in another form, and never
try to get around a refusal. ROOT stands for the repository root, your working directory, as an absolute path;
write it out in full.

1. Bash: `python3 analyst/run_window.py probe`
2. Read: `ROOT/analyst/ROUTINE_PROMPT.md`
3. Write: `ROOT/.analyst_run/plan.json`, with the content `{}` and a final newline.
4. Bash: `gh api repos/Mohamedalhaaj/gold-analyst-inbox/hooks`
5. Bash: `curl -sS -m 10 https://example.com`
6. Bash: `python3 -c 'print(1)'`
7. Bash: `cd /tmp && pwd`
8. Edit: in `ROOT/.analyst_run/plan.json`, replace `{}` with `{ }`.
9. Read `ROOT/.claude/settings.json`, then Write that same path with exactly the content you read.
10. WebFetch: `https://example.com`, with the prompt `Reply ok.`
11. Agent: the description `probe` and the prompt `Reply with the single word ok. Use no tools.`
12. If you have a tool that reads GitHub pull requests, call it once to list the open pull requests of
    `Mohamedalhaaj/gold-analyst-inbox`. Otherwise skip this step.

Then stop. End with one table, one row per tool call in order: the step, the tool, and the outcome: `ran` and
the first line of its output, `refused` and the first line of the refusal message, `unavailable` if the tool
does not exist, or `skipped`. Below it, on a last line, quote the sentence of your Bash tool's description
that states its default timeout.
"""


def ticks(text):
    return re.findall(r"`([^`]+)`", text)


# ---------------------------------------------------------------- the prompts' text
def test_the_routine_prompt_opens_with_the_registers_paragraph_and_keeps_its_steps():
    text = PROMPT.read_text("utf-8")
    title, rest = text.split("\n\n", 1)
    assert title == "# gold-analyst routine prompt (frozen)"
    assert rest.startswith(HEADER + "\n## CALL (routines analyst-w1 and analyst-w2)\n")
    for section in ("## CALL (routines analyst-w1 and analyst-w2)", "## LESSON (routine analyst-lesson)",
                    "## REVISION (routine analyst-revision)"):
        assert text.count(section) == 1


def test_the_probe_session_prompt_is_the_registers_text():
    assert PROBE_PROMPT.read_bytes() == PROBE_TEXT.encode("utf-8")


# ---------------------------------------------------------------- what the routine prompt names, under the guard
@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    """A root holding the real prompts and the guard's files (the guard checks its own place under the root)."""
    import shutil
    r = tmp_path_factory.mktemp("prompt").resolve() / "repo"
    for p in ("analyst/ROUTINE_PROMPT.md", "analyst/PROBE_SESSION_PROMPT.md", "analyst/prompts/system.md",
              "analyst/prompts/lesson.md", "analyst/prompts/revision.md"):
        (r / p).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / p, r / p)
    (r / ".claude" / "hooks").mkdir(parents=True)
    shutil.copyfile(SETTINGS, r / ".claude" / "settings.json")
    shutil.copyfile(GUARD, r / ".claude" / "hooks" / "guard.py")
    return str(r), load(r / ".claude" / "hooks" / "guard.py")


def decide(g, root, tool, ti, cwd=None):
    return g.check(data(event(tool, ti, cwd=cwd or root)), root)


def test_every_command_line_of_the_routine_prompt_is_a_guard_line(repo):
    root, g = repo
    cmds = [t for t in ticks(PROMPT.read_text("utf-8")) if t.startswith("python3")]
    assert sorted(set(cmds)) == sorted(set(LINES) - {"python3 analyst/run_window.py probe"})
    for c in cmds:
        assert decide(g, root, "Bash", {"command": c, "description": "step"}) is None, c


def test_every_file_the_routine_prompt_names_can_be_read_and_only_its_drafts_written(repo):
    root, g = repo
    text = PROMPT.read_text("utf-8")
    named = {t for t in ticks(text) if not t.startswith(("python3", "cd")) and ("/" in t or t.startswith("."))}
    assert named == {"analyst/prompts/system.md", ".analyst_run/context.json", ".analyst_run/plan.json",
                     "analyst/prompts/lesson.md", ".analyst_run/lesson_input.json", ".analyst_run/lesson.json",
                     "analyst/prompts/revision.md", ".analyst_run/revision_input.json", ".analyst_run/playbook.md",
                     ".claude/hooks/guard.py"}
    drafts = set(re.findall(r"Write only `([^`]+)`", text))
    assert drafts == {".analyst_run/plan.json", ".analyst_run/lesson.json", ".analyst_run/playbook.md"}
    for p in sorted(named):
        assert decide(g, root, "Read", {"file_path": f"{root}/{p}"}) is None, p
        want = None if p in drafts else "write_path"
        assert decide(g, root, "Write", {"file_path": f"{root}/{p}", "content": "{}\n"}) == want, p


def probe_steps(root):
    """{step: [(tool, input), ...]} as PROBE_SESSION_PROMPT.md words them, with ROOT written out."""
    text = PROBE_PROMPT.read_text("utf-8").replace("ROOT", root)
    body = text.split("\n\n")[2]                                   # the numbered list
    steps = {}
    for m in re.finditer(r"^(\d+)\. (.*?)(?=^\d+\. |\Z)", body, re.S | re.M):
        steps[int(m.group(1))] = " ".join(m.group(2).split())
    assert sorted(steps) == list(range(1, 13))
    out = {}
    for n, s in steps.items():
        t = ticks(s)
        if s.startswith("Bash: "):
            out[n] = [("Bash", {"command": t[0], "description": f"step {n}"})]
        elif s.startswith("Read: "):
            out[n] = [("Read", {"file_path": t[0]})]
        elif s.startswith("Write: "):
            assert "with the content `{}` and a final newline" in s
            out[n] = [("Write", {"file_path": t[0], "content": "{}\n"})]
        elif s.startswith("Edit: "):
            out[n] = [("Edit", {"file_path": t[0], "old_string": t[1], "new_string": t[2]})]
        elif s.startswith("Read `") and "then Write that same path" in s:
            out[n] = [("Read", {"file_path": t[0]}),
                      ("Write", {"file_path": t[0], "content": (ROOT / ".claude/settings.json").read_text()})]
        elif s.startswith("WebFetch: "):
            out[n] = [("WebFetch", {"url": t[0], "prompt": t[1]})]
        elif s.startswith("Agent: "):
            out[n] = [("Agent", {"description": t[0], "prompt": t[1]})]
        elif "GitHub pull requests" in s:
            out[n] = [("mcp__github__list_pull_requests", {"owner": t[0].split("/")[0], "repo": t[0].split("/")[1],
                                                           "state": "open"})]
    assert sorted(out) == list(range(1, 13))
    return out


def test_the_probe_session_steps_get_the_outcomes_c20_expects(repo):
    """C20: steps 1 to 3 and step 9's Read run; steps 4 to 8, step 9's Write and steps 10 to 12 are refused by the
    guard, with a message that holds `guard:`."""
    root, g = repo
    want = {1: [None], 2: [None], 3: [None], 4: ["bash_form"], 5: ["bash_form"], 6: ["bash_form"],
            7: ["bash_form"], 8: ["tool"], 9: [None, "write_path"], 10: ["tool"], 11: ["tool"], 12: ["tool"]}
    got = {n: [decide(g, root, t, ti) for t, ti in calls] for n, calls in probe_steps(root).items()}
    assert got == want
    for codes in got.values():
        for c in codes:
            assert c is None or g.message(c).startswith(f"guard: {c}: ")


def test_no_refused_bash_step_of_the_probe_session_can_be_shortened_into_an_allowed_line(repo):
    """First review of A7 (V6-1): in real CLI runs the sessions dropped the redundant `cd ROOT && ` of the old step 7
    and ran the allowed probe line, which C20 cannot pass. Every refused Bash step must stay refused however a
    session shortens it: each of its parts at `&&`, `||`, `;` or `|`, and each of its words alone."""
    root, g = repo
    for n, calls in probe_steps(root).items():
        for tool, ti in calls:
            if tool != "Bash" or n == 1:
                continue
            cmd = ti["command"]
            parts = [p.strip() for p in re.split(r"&&|\|\||;|\|", cmd)]
            words = cmd.split()
            for form in parts + words + [" ".join(words[i:]) for i in range(len(words))]:
                assert decide(g, root, "Bash", {"command": form}) == "bash_form", (n, form)
            assert root not in cmd, n                              # no step names the root it already runs in


# ---------------------------------------------------------------- the session flows under the guard
GUARD_FILES = {".claude/settings.json": SETTINGS.read_bytes(), ".claude/hooks/guard.py": GUARD.read_bytes()}


class Gated:
    """A routine session in the local world whose every step first passes the guard of its own clone."""

    def __init__(self, h, monkeypatch):
        self.h, self.root = h, str(h.sess_dir)
        assert (h.sess_dir / ".claude" / "hooks" / "guard.py").read_bytes() == GUARD.read_bytes()
        self.g = load(h.sess_dir / ".claude" / "hooks" / "guard.py")
        monkeypatch.chdir(h.sess_dir)                          # the commands run from the repository root

    def ok(self, tool, ti):
        assert self.g.check(data(event(tool, ti, cwd=self.root)), self.root) is None, (tool, ti)

    def bash(self, line):
        self.ok("Bash", {"command": line, "description": "routine step"})
        argv = line.split()
        assert argv[:2] == ["python3", "analyst/run_window.py"]
        sub, args = argv[2], argv[3:]
        if sub == "lock" and args == ["--decline"]:
            return self.h.run("lock", decline=True)
        return self.h.run(sub.replace("-", "_"), *args)

    def read(self, rel):
        self.ok("Read", {"file_path": f"{self.root}/{rel}"})
        return (Path(self.root) / rel).read_bytes()

    def write(self, rel, text):
        self.ok("Write", {"file_path": f"{self.root}/{rel}", "content": text})
        p = Path(self.root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, "utf-8")


def test_a_call_and_a_declined_call_under_the_guard(tmp_path, monkeypatch):
    h = Harness(tmp_path, extra=dict(GUARD_FILES))
    s = Gated(h, monkeypatch)
    assert s.bash("python3 analyst/run_window.py start") == 0 and h.log[-1] == "attempt_pushed 2026-11-10_W1 n=1 " \
                                                                                  "mode=canary"
    h.push_and_merge()
    h.at(2026, 11, 10, 7, 21)
    assert s.bash("python3 analyst/run_window.py context") == 0 and h.log[-1].startswith("context_ok 2026-11-10_W1")
    s.read("analyst/prompts/system.md")
    s.read(".analyst_run/context.json")
    s.write(".analyst_run/plan.json", json.dumps(PLAN, ensure_ascii=False))
    h.at(2026, 11, 10, 7, 25)
    assert s.bash("python3 analyst/run_window.py lock .analyst_run/plan.json") == 0
    assert h.log[-1] == "locked 2026-11-10_W1 status=called"
    h.push_and_merge()
    assert h.main_json("analyst/canary/terminal/2026-11-10_W1.json")["status"] == "called"
    h.at(2026, 11, 10, 13, 50)                                 # W2 (EST entry 14:30): declined
    assert s.bash("python3 analyst/run_window.py start") == 0
    h.push_and_merge()
    h.at(2026, 11, 10, 13, 52)
    assert s.bash("python3 analyst/run_window.py context") == 0
    assert s.bash("python3 analyst/run_window.py lock --decline") == 0
    h.push_and_merge()
    assert h.main_json("analyst/canary/terminal/2026-11-10_W2.json")["status"] == "no_call:refusal"


def test_a_lesson_and_a_revision_under_the_guard(tmp_path, monkeypatch):
    fx = ROOT / P.CANARY_SETTLED_FIXTURE
    h = Harness(tmp_path, canary_from="2026-11-09", extra={P.CANARY_SETTLED_FIXTURE: fx.read_bytes(), **GUARD_FILES})
    s = Gated(h, monkeypatch)
    h.at(2026, 11, 10, 16, 40)
    assert s.bash("python3 analyst/run_window.py lesson-start") == 0
    assert h.log[-1].startswith("lesson_attempt_pushed 2026-10-16_W1 n=1")
    h.push_and_merge()
    s.read("analyst/prompts/lesson.md")
    s.read(".analyst_run/lesson_input.json")
    s.write(".analyst_run/lesson.json", json.dumps({"text_ar": "درس قصير من سجل اصطناعي"}, ensure_ascii=False))
    h.at(2026, 11, 10, 16, 42)
    assert s.bash("python3 analyst/run_window.py lesson-lock .analyst_run/lesson.json") == 0
    h.push_and_merge()
    assert h.main_json("analyst/canary/lessons/2026-10-16_W1.json")["status"] == "ok"
    assert s.bash("python3 analyst/run_window.py lesson-start") == 0 and h.log[-1] == "none"
    h.at(2026, 11, 14, 18, 0)
    h.merge()
    assert s.bash("python3 analyst/run_window.py revision-start") == 0
    assert h.log[-1].startswith("revision_attempt_pushed v1 n=1")
    h.push_and_merge()
    s.read("analyst/prompts/revision.md")
    s.read(".analyst_run/revision_input.json")
    s.write(".analyst_run/playbook.md", "# playbook v1\nفرضية من عينة صغيرة.\n")
    h.at(2026, 11, 14, 18, 5)
    assert s.bash("python3 analyst/run_window.py revision-lock .analyst_run/playbook.md") == 0
    h.push_and_merge()
    assert h.world.main_file("analyst/canary/playbooks/v1.md") == "# playbook v1\nفرضية من عينة صغيرة.\n".encode()


def test_the_probe_step_under_the_guard(tmp_path, monkeypatch):
    from test_inbox_session import ok_probe_get
    h = Harness(tmp_path, extra=dict(GUARD_FILES))
    h.world.owner_commit({"analyst/ops_changes.csv": "utc,file,sha256,head_sha,note\n"})   # main has a parent
    h.probe_get = ok_probe_get()                     # Twelve Data answers, Dukascopy refused (§16.18 A9)
    h.at(2026, 10, 29, 10, 15)
    s = Gated(h, monkeypatch)
    assert s.bash("python3 analyst/run_window.py probe") == 0
    assert [l for l in h.log if l.startswith("probe_")][-1].startswith("probe_pass ")
    s.read("analyst/ROUTINE_PROMPT.md")
    s.write(".analyst_run/plan.json", "{}\n")                  # C20 step 3, after the probe made .analyst_run
