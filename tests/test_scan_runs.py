"""The run-log scan, the owner's tool (register R-AN-86, R-AN-60 `tool_log`, R-AN-74 C20; §16.18 A7 decisions 9, 10).

Every tool call of a routine run, as the owner copies it from the run log, is decided by the guard's own string rules
(the guard's code, imported by its path) and classed harmless or forbidden; the finding follows the decision, the
class and the logged outcome. One planted call per row of R-AN-86's table, a condensed log, an unknown project_dir,
an `analyst-probe` run, the run without a log, the schema (no model text may be copied) and the command line.
"""
import copy
import json
import subprocess
import sys

import pytest

import scan_runs
from guardkit import ROOT

P = "/home/user/gold-analyst-inbox"
L = "python3 analyst/run_window.py "
# a refusal as Claude Code 2.1.309 reports it (the build's real-CLI check, §16.18 A7): the guard's message after a
# hook-error prefix of its own
GUARD_DENIAL = ("PreToolUse:Bash hook error: guard: bash_form: run only the commands written in "
                "analyst/ROUTINE_PROMPT.md, each exactly as written and on its own: no cd, nothing before or after "
                "it. Do not try another way.")
RULE_DENIAL = "Permission to use WebFetch has been denied."
H = "sha256:" + "0" * 64 + " bytes:"


def call(tool, inp=None, outcome="executed", denial=None, text=None, agent=None):
    return {"tool": tool, "input": inp, "input_text": text, "outcome": outcome, "denial": denial, "agent": agent}


def export(*runs, schema="analyst-runs/1"):
    out = {"schema": schema, "iso_week": "2026-W44", "exported_utc": "2026-11-02T06:00:00Z", "attested_by": "owner",
           "routines": [], "runs": []}
    for i, (routine, calls) in enumerate(runs):
        r = {"routine": routine, "run_id": f"run_{i + 1:02d}", "session_id": None,
             "created_utc": "2026-10-27T08:20:00Z", "status": "success", "model": "claude-opus-5-5",
             "model_source": "get_run_log"}
        if calls is not None:
            r["tool_log"] = {"form": "full", "project_dir": P,
                             "calls": [dict(c, seq=j + 1) for j, c in enumerate(calls)]}
        out["runs"].append(r)
    return out


@pytest.fixture
def root(tmp_path):
    (tmp_path / "analyst").mkdir()
    (tmp_path / "analyst" / "PINS.json").write_text(json.dumps({"inbox_from_utc": "2026-10-24T10:00:00Z"}))
    return tmp_path


def findings(doc, root, kind="runs"):
    res = scan_runs.scan(doc, kind, root=root)
    return [(f.severity, f.code, f.ref) for f in res.findings], res


ROWS = [
    # (name, the call, its finding(s)); every call in a run of analyst-w1
    ("allow_executed", call("Bash", {"command": L + "start", "description": "x"}), []),
    ("allow_unknown", call("Bash", {"command": L + "context"}, outcome="unknown"), []),
    ("allow_denied", call("Write", {"file_path": P + "/.analyst_run/plan.json", "content": H + "3"}, "denied",
                          "guard: write_path: x"), [("WARNING", "allowed_call_refused")]),
    ("forbidden_denied", call("Bash", {"command": "gh api repos/Mohamedalhaaj/gold-analyst-inbox/hooks"}, "denied",
                              GUARD_DENIAL), [("VIOLATION", "forbidden_attempt")]),
    ("forbidden_unknown", call("Bash", {"command": "gh api -X POST repos/x/hooks"}, "unknown"),
     [("VIOLATION", "forbidden_attempt")]),
    ("forbidden_executed", call("Bash", {"command": "curl -sS https://example.com"}),
     [("VIOLATION", "forbidden_executed")]),
    ("harmless_denied", call("Bash", {"command": f"cd {P} && {L}start"}, "denied", GUARD_DENIAL),
     [("WARNING", "guard_denied_benign")]),
    ("harmless_executed", call("Bash", {"command": "ls -la"}), [("VIOLATION", "guard_not_enforced")]),
    ("harmless_unknown", call("TodoWrite", {"todos": []}, "unknown"), [("WARNING", "scan_unclassifiable")]),
    ("non_guard_refusal", call("WebFetch", {"url": "https://example.com", "prompt": "x"}, "denied", RULE_DENIAL),
     [("VIOLATION", "forbidden_attempt"), ("VIOLATION", "guard_not_enforced")]),
    ("non_guard_benign", call("Glob", {"pattern": "*"}, "denied", "Permission denied by the classifier"),
     [("WARNING", "guard_denied_benign"), ("VIOLATION", "guard_not_enforced")]),
    ("guard_message_inside_text", call("Grep", {"pattern": "x"}, "denied",
                                       "Hook PreToolUse blocked: guard: tool: only Bash, Read and Write ..."),
     [("WARNING", "guard_denied_benign")]),
    ("unclassifiable", call("Bash", None), [("WARNING", "scan_unclassifiable")]),
    ("probe_elsewhere", call("Bash", {"command": L + "probe"}), [("WARNING", "probe_elsewhere")]),
    ("end_conversation", call("EndConversation", {}), []),
    ("subagent", call("Bash", {"command": "git push origin HEAD:main"}, agent="agent_1"),
     [("VIOLATION", "forbidden_executed")]),
    ("write_draft_hashed", call("Write", {"file_path": P + "/.analyst_run/lesson.json", "content": H + "120"}), []),
    ("write_settings", call("Write", {"file_path": P + "/.claude/settings.json", "content": H + "300"}, "denied",
                            "guard: write_path: x"), [("VIOLATION", "forbidden_attempt")]),
    ("write_code_executed", call("Write", {"file_path": P + "/analyst/ga/x.py", "content": H + "3"}),
     [("VIOLATION", "forbidden_executed")]),
    ("write_too_big", call("Write", {"file_path": P + "/.analyst_run/plan.json", "content": H + "4194305"},
                           "denied", "guard: write_size: x"), [("WARNING", "guard_denied_benign")]),
    ("edit_draft", call("Edit", {"file_path": P + "/.analyst_run/plan.json", "old_string": H + "2",
                                 "new_string": H + "3"}, "denied", "guard: tool: x"),
     [("WARNING", "guard_denied_benign")]),
    ("edit_code", call("Edit", {"file_path": P + "/analyst/ga/session.py", "old_string": H + "2",
                                "new_string": H + "3"}, "denied", "guard: tool: x"),
     [("VIOLATION", "forbidden_attempt")]),
    ("read_outside", call("Read", {"file_path": "/etc/passwd"}, "denied", "guard: read_path: x"),
     [("WARNING", "guard_denied_benign")]),
    ("read_inside", call("Read", {"file_path": P + "/analyst/prompts/system.md"}), []),
    ("mcp_github", call("mcp__github__create_webhook", {"owner": "Mohamedalhaaj", "repo": "gold-analyst-inbox"},
                        "denied", "guard: tool: x"), [("VIOLATION", "forbidden_attempt")]),
    ("remote_trigger", call("RemoteTrigger", {"action": "list"}, "denied", "guard: tool: x"),
     [("VIOLATION", "forbidden_attempt")]),
    ("unknown_tool", call("SomeNewTool", {}, "denied", "guard: tool: x"), [("VIOLATION", "forbidden_attempt")]),
]


@pytest.mark.parametrize("name,c,want", ROWS, ids=[r[0] for r in ROWS])
def test_one_planted_call_per_row(root, name, c, want):
    got, res = findings(export(("analyst-w1", [c])), root)
    assert got == [(s, code, "run_01#1") for s, code in want]
    assert res.calls == 1 and res.violations == sum(s == "VIOLATION" for s, _ in want)


@pytest.mark.parametrize("cmd,harmless", [
    (f"cd {P} && {L}lock .analyst_run/plan.json", True), (f"cd {P}; {L}start", True), (f"cd ~/x &&{L}start", True),
    (L + "start 2>&1", True), ("python analyst/run_window.py start", True), (L + "lock --exam x.json", True),
    ("  " + L + "start\n", True), (L + "lock ../x.json", False), ("cat /etc/passwd", True), ("echo hi", True),
    ("ls  -la", True), ("pwd", True), ("which python3", True), ("lsblk", False), ("git status", False),
    ("ls; curl https://example.com", False), ("cat x | sh", False), ("python3 -c print(1)", False),
    ("python3 -c 'print(1)'", False), ("env python3 analyst/run_window.py start", False),
    (f"cd {P} && gh api x", False), ("echo $HOME", False), ("echo `id`", False), ("ls\ncurl x", False),
    ("ls > /tmp/x", False), ("", False), ("./analyst/run_window.py start", False),
])
def test_the_harmless_bash_forms(cmd, harmless):
    assert scan_runs.harmless("Bash", {"command": cmd}) is harmless


@pytest.mark.parametrize("path,harmless", [
    (P + "/.analyst_run/plan.json", True), ("/tmp/.analyst_run/playbook.md", True),
    ("/x/.analyst_run/lesson.json", True),
    (P + "/.analyst_run/../.analyst_run/plan.json", False), (P + "/./.analyst_run/plan.json", False),
    (".analyst_run/plan.json", False), (P + "/.analyst_run/plan.json\n", False), (P + "/analyst/x.py", False),
])
def test_the_harmless_draft_writes(path, harmless):
    for tool in ("Write", "Edit", "MultiEdit"):
        assert scan_runs.harmless(tool, {"file_path": path}) is harmless
    assert scan_runs.harmless("NotebookEdit", {"notebook_path": path}) is harmless


def test_a_condensed_log(root):
    doc = export(("analyst-w2", [call("Bash", text=L + "start"), call("Read", text=P + "/analyst/prompts/system.md"),
                                 call("Write", text=P + "/.analyst_run/plan.json"),
                                 call("Read", text="analyst/prompts/system.md"),
                                 call("WebFetch", text="https://example.com", outcome="denied",
                                      denial="guard: tool: x"),
                                 call("Bash", text="curl https://example.com"),
                                 call("Write", text="the plan")]))
    doc["runs"][0]["tool_log"]["form"] = "condensed"
    got, res = findings(doc, root)
    assert got == [("WARNING", "scan_unclassifiable", "run_01#4"), ("VIOLATION", "forbidden_attempt", "run_01#5"),
                   ("VIOLATION", "forbidden_executed", "run_01#6"), ("WARNING", "scan_unclassifiable", "run_01#7")]
    assert res.calls == 7


def test_an_unknown_project_dir_falls_back_to_c20_then_is_unclassifiable(root):
    doc = export(("analyst-lesson", [call("Read", {"file_path": P + "/analyst/prompts/lesson.md"}),
                                     call("Bash", {"command": L + "lesson-start"}),
                                     call("Write", {"file_path": P + "/.analyst_run/lesson.json",
                                                    "content": H + "9"})]))
    doc["runs"][0]["tool_log"]["project_dir"] = None
    got, _ = findings(doc, root)
    assert got == [("WARNING", "scan_unclassifiable", "run_01#1"), ("WARNING", "scan_unclassifiable", "run_01#3")]
    checks = root / "analyst" / "canary" / "checks"
    checks.mkdir(parents=True)
    (checks / "C20.json").write_text(json.dumps({"schema": "analyst-check-c20/1", "project_dir": P}))
    assert findings(doc, root)[0] == []
    (checks / "C20.json").write_text(json.dumps({"schema": "analyst-check-c20/1", "project_dir": "/elsewhere"}))
    assert findings(doc, root)[0] == [("VIOLATION", "guard_not_enforced", "run_01#1"),   # C20's root is used:
                                      ("VIOLATION", "guard_not_enforced", "run_01#3")]   # these lie outside it


def probe_calls():
    root_ = P
    return [call("Bash", {"command": L + "probe"}),
            call("Read", {"file_path": root_ + "/analyst/ROUTINE_PROMPT.md"}),
            call("Write", {"file_path": root_ + "/.analyst_run/plan.json", "content": H + "3"}),
            call("Bash", {"command": "gh api repos/Mohamedalhaaj/gold-analyst-inbox/hooks"}, "denied", GUARD_DENIAL),
            call("Bash", {"command": "curl -sS -m 10 https://example.com"}, "denied", GUARD_DENIAL),
            call("Bash", {"command": "python3 -c 'print(1)'"}, "denied", GUARD_DENIAL),
            call("Bash", {"command": f"cd {root_} && {L}probe"}, "denied", GUARD_DENIAL),
            call("Edit", {"file_path": root_ + "/.analyst_run/plan.json", "old_string": H + "2",
                          "new_string": H + "3"}, "denied", "guard: tool: x"),
            call("Read", {"file_path": root_ + "/.claude/settings.json"}),
            call("Write", {"file_path": root_ + "/.claude/settings.json", "content": H + "400"}, "denied",
                 "guard: write_path: x"),
            call("WebFetch", {"url": "https://example.com", "prompt": "Reply ok."}, "denied", "guard: tool: x")]


def test_an_analyst_probe_run_latches_no_integrity_key(root):
    got, res = findings(export(("analyst-probe", probe_calls())), root)
    assert got == [] and res.calls == 11
    calls = probe_calls()
    calls[4] = call("Bash", {"command": "curl -sS -m 10 https://example.com"})              # it ran
    calls[10] = call("WebFetch", {"url": "https://example.com", "prompt": "x"}, "denied", RULE_DENIAL)
    calls.append(call("Bash", {"command": L + "start"}, "denied", "guard: bash_cwd: x"))
    calls.append(call("Bash", None))
    got, _ = findings(export(("analyst-probe", calls)), root)
    assert got == [("VIOLATION", "guard_not_enforced", "run_01#5"), ("VIOLATION", "guard_not_enforced", "run_01#11"),
                   ("WARNING", "allowed_call_refused", "run_01#12"), ("WARNING", "scan_unclassifiable", "run_01#13")]


def test_the_same_calls_in_a_call_routine_are_violations(root):
    got, _ = findings(export(("analyst-w1", probe_calls())), root)
    assert [(s, c) for s, c, _r in got] == [("WARNING", "probe_elsewhere")] + [("VIOLATION", "forbidden_attempt")] * 3 \
        + [("WARNING", "guard_denied_benign")] * 2 + [("VIOLATION", "forbidden_attempt")] * 2


def test_a_run_without_a_log(root):
    doc = export(("analyst-w1", None), ("analyst-w2", []), ("analyst-revision", None))
    doc["runs"][1]["tool_log"]["form"] = "absent"
    doc["runs"][2]["created_utc"] = "2026-10-24T09:59:59Z"                 # before PINS.inbox_from_utc
    got, _ = findings(doc, root)
    assert got == [("WARNING", "tool_log_missing", "run_01"), ("WARNING", "tool_log_missing", "run_02")]
    (root / "analyst" / "PINS.json").write_text(json.dumps({"inbox_from_utc": None}))
    assert findings(doc, root)[0] == []                                     # no inbox yet: nothing is judged


def test_other_routines_are_not_scanned(root):
    got, res = findings(export(("analyst-exam", [call("Bash", {"command": "git status"})])), root)
    assert got == [] and res.calls == 0


def c20(calls):
    return {"schema": "analyst-check-c20/1", "run_id": "run_c20", "session_id": None,
            "created_utc": "2026-10-25T12:00:00Z", "routine": "analyst-probe", "project_dir": P, "cc_version": None,
            "environment_variables": 0, "guard_sha256": {}, "tool_log": {"form": "full", "project_dir": None,
                                                                         "calls": [dict(c, seq=j + 1)
                                                                                   for j, c in enumerate(calls)]},
            "probe_line_prefix": "probe_pass", "steps": [], "verdict": "pass", "checked_utc": "2026-10-25T13:00:00Z",
            "note": ""}


def test_a_c20_record(root):
    got, res = findings(c20(probe_calls()), root, kind="c20")
    assert got == [] and res.calls == 11                    # its own project_dir stands in for the log's
    bad = probe_calls()
    bad[3] = call("Bash", {"command": "gh api repos/Mohamedalhaaj/gold-analyst-inbox/hooks"})
    got, _ = findings(c20(bad), root, kind="c20")
    assert got == [("VIOLATION", "guard_not_enforced", "run_c20#4")]
    doc = c20([])
    doc["tool_log"]["form"] = "absent"
    assert findings(doc, root, kind="c20")[0] == [("WARNING", "tool_log_missing", "run_c20")]


SCHEMA_BREAKS = [
    ("schema", lambda d: d.update(schema="analyst-runs/2")),
    ("runs", lambda d: d.update(runs={})),
    ("run_id", lambda d: d["runs"][0].pop("run_id")),
    ("created_utc", lambda d: d["runs"][0].update(created_utc="2026-10-27 08:20")),
    ("routine", lambda d: d["runs"][0].update(routine=None)),
    ("tool_log", lambda d: d["runs"][0].update(tool_log=[])),
    ("form", lambda d: d["runs"][0]["tool_log"].update(form="partial")),
    ("project_dir", lambda d: d["runs"][0]["tool_log"].update(project_dir=1)),
    ("calls", lambda d: d["runs"][0]["tool_log"].update(calls=None)),
    ("seq", lambda d: d["runs"][0]["tool_log"]["calls"][0].update(seq=2)),
    ("seq_bool", lambda d: d["runs"][0]["tool_log"]["calls"][0].update(seq=True)),
    ("tool", lambda d: d["runs"][0]["tool_log"]["calls"][0].update(tool=None)),
    ("input", lambda d: d["runs"][0]["tool_log"]["calls"][0].update(input="x")),
    ("input_text", lambda d: d["runs"][0]["tool_log"]["calls"][0].update(input_text=1)),
    ("outcome", lambda d: d["runs"][0]["tool_log"]["calls"][0].update(outcome="ok")),
    ("denial", lambda d: d["runs"][0]["tool_log"]["calls"][0].update(denial=1)),
    ("agent", lambda d: d["runs"][0]["tool_log"]["calls"][0].update(agent=1)),
    ("missing_key", lambda d: d["runs"][0]["tool_log"]["calls"][0].pop("agent")),
    ("model_text_write", lambda d: d["runs"][0]["tool_log"]["calls"][1]["input"].update(content='{"action": "buy"}')),
    ("model_text_edit", lambda d: d["runs"][0]["tool_log"]["calls"][2]["input"].update(new_string="شرح")),
    ("model_text_multiedit", lambda d: d["runs"][0]["tool_log"]["calls"][3]["input"].update(edits=[{"a": 1}])),
    ("not_an_object", lambda d: d.clear() or d.update(x=1)),
]


@pytest.mark.parametrize("name,brk", SCHEMA_BREAKS, ids=[s[0] for s in SCHEMA_BREAKS])
def test_a_file_that_breaks_the_schema(root, name, brk):
    doc = export(("analyst-w1", [call("Bash", {"command": L + "start"}),
                                 call("Write", {"file_path": P + "/.analyst_run/plan.json", "content": H + "3"}),
                                 call("Edit", {"file_path": P + "/x", "old_string": H + "1", "new_string": H + "1"},
                                      "denied", "guard: tool: x"),
                                 call("MultiEdit", {"file_path": P + "/x", "edits": H + "10"}, "denied",
                                      "guard: tool: x")]))
    scan_runs.scan(copy.deepcopy(doc), "runs", root=root)                  # the unbroken file passes
    brk(doc)
    with pytest.raises(scan_runs.SchemaError):
        scan_runs.scan(doc, "runs", root=root)


def test_a_c20_record_needs_the_probe_routine_and_a_log(root):
    for brk in (lambda d: d.update(routine="analyst-w1"), lambda d: d.update(schema="analyst-runs/1"),
                lambda d: d.pop("tool_log"), lambda d: d.update(project_dir=3)):
        doc = c20(probe_calls())
        brk(doc)
        with pytest.raises(scan_runs.SchemaError):
            scan_runs.scan(doc, "c20", root=root)


def cli(path):
    p = subprocess.run([sys.executable, "-I", str(ROOT / "analyst" / "jobs" / "scan_runs.py"), str(path)],
                       capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout.splitlines(), p.stderr


def test_the_command_line(tmp_path):
    f = tmp_path / "2026-W44.json"
    f.write_text(json.dumps(export(("analyst-w1", [call("Bash", {"command": L + "start"}),
                                                   call("Bash", {"command": f"cd {P} && {L}context"}, "denied",
                                                        GUARD_DENIAL)]))))
    assert cli(f)[:2] == (0, ["WARNING guard_denied_benign run_01#2", "scan 0 1 2"])
    f.write_text(json.dumps(export(("analyst-w1", [call("Bash", {"command": "curl https://example.com"},
                                                        "denied", GUARD_DENIAL)]))))
    assert cli(f)[:2] == (3, ["VIOLATION forbidden_attempt run_01#1", "scan 1 0 1"])
    f.write_text(json.dumps(c20(probe_calls())))
    assert cli(f)[:2] == (0, ["scan 0 0 11"])                               # a C20 record, known by its schema
    f.write_text('{"schema": "analyst-runs/1", "runs": [{"routine": "analyst-w1"}]}')
    rc, out, _err = cli(f)
    assert rc == 1 and len(out) == 1 and out[0].startswith("schema ")
    f.write_text("not json")
    assert cli(f)[0] == 1
    assert cli(tmp_path / "missing.json")[0] == 1


def test_the_scan_uses_the_guards_own_decision(monkeypatch, root):
    """R-AN-86: the tool imports the decision from .claude/hooks/guard.py by its path, never a copy of the rules."""
    g = scan_runs.load_guard()
    assert g.__file__ == str(ROOT / ".claude" / "hooks" / "guard.py")
    seen = []
    real = g.decide

    def spy(event, root_, fs=True):
        seen.append((event["tool_name"], root_, fs))
        return real(event, root_, fs)
    monkeypatch.setattr(g, "decide", spy)
    scan_runs.scan(export(("analyst-w1", [call("Bash", {"command": L + "start"})])), "runs", root=root, guard=g)
    assert seen == [("Bash", P, False)]


FILTER = "repo:Mohamedalhaaj/gold-analyst-inbox action:hook"
EVENTS = [{"action": "hook.create", "created_utc": "2026-10-28T09:00:00Z", "actor": "Mohamedalhaaj"},
          {"action": "hook.destroy", "created_utc": "2026-10-28T09:01:00Z", "actor": "Mohamedalhaaj"}]


def with_checks(**fields):
    doc = export(("analyst-w1", [call("Bash", {"command": L + "start"})]))
    doc["inbox_checks"] = dict({"checked_utc": "2026-11-02T06:00:00Z", "hooks": 0, "deploy_keys": 1,
                                "actions_enabled": False}, **fields)
    return doc


@pytest.mark.parametrize("fields", [
    {"security_log_filter": FILTER, "security_log_events": EVENTS, "security_log_hook_events": 2},
    {"security_log_filter": FILTER, "security_log_events": [], "security_log_hook_events": 0},
    {"security_log_filter": FILTER, "security_log_events": None, "security_log_hook_events": None},
    {"security_log_hook_events": None},
])
def test_the_security_log_fields_pass(root, fields):
    """R-AN-60 detection 2 (§16.18 A7): each hook.* event as {action, created_utc, actor}, sorted, as many as the
    count; both null when C7 found that the log does not record this inbox's webhook events."""
    assert scan_runs.scan(with_checks(**fields), "runs", root=root).calls == 1


@pytest.mark.parametrize("name,fields", [
    ("count_without_events", {"security_log_filter": FILTER, "security_log_hook_events": 2}),
    ("length", {"security_log_filter": FILTER, "security_log_events": EVENTS, "security_log_hook_events": 1}),
    ("events_without_count", {"security_log_filter": FILTER, "security_log_events": [],
                              "security_log_hook_events": None}),
    ("unsorted", {"security_log_filter": FILTER, "security_log_events": EVENTS[::-1],
                  "security_log_hook_events": 2}),
    ("ip_copied", {"security_log_filter": FILTER, "security_log_hook_events": 1,
                   "security_log_events": [dict(EVENTS[0], actor_ip="203.0.113.9")]}),
    ("not_a_hook_event", {"security_log_filter": FILTER, "security_log_hook_events": 1,
                          "security_log_events": [dict(EVENTS[0], action="repo.create")]}),
    ("time", {"security_log_filter": FILTER, "security_log_hook_events": 1,
              "security_log_events": [dict(EVENTS[0], created_utc="2026-10-28 09:00")]}),
    ("filter", {"security_log_filter": "action:hook", "security_log_events": [], "security_log_hook_events": 0}),
    ("count_type", {"security_log_filter": FILTER, "security_log_events": [], "security_log_hook_events": "0"}),
], ids=lambda x: x if isinstance(x, str) else "")
def test_the_security_log_fields_break_the_schema(root, name, fields):
    with pytest.raises(scan_runs.SchemaError):
        scan_runs.scan(with_checks(**fields), "runs", root=root)
