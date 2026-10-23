"""The guard's configuration (register R-AN-85 "Files", R-AN-2, R-AN-7; §16.18 A7 decisions 2, 6 and 8).

`.claude/settings.json` is exactly the register's block: one PreToolUse hook for every tool (matcher "*"), the shell
form `python3 -I "${CLAUDE_PROJECT_DIR}/.claude/hooks/guard.py"` with timeout 30 and onFailure "block", and the
deny rules WebFetch, WebSearch, Agent and Skill as a second layer. The guard's files and the probe session's prompt
are frozen (FROZEN_ANALYST.sha256), so a change is frozen drift for the judge (A11) as well as A13 `guard_config`.

Second review of A7 (decision 20, R-AN-85 "Command time"): the `env` block sets BASH_DEFAULT_TIMEOUT_MS and
BASH_MAX_TIMEOUT_MS to 3600000, so no allowed command reaches its timeout and is moved to the background, where the
session could not read its output (the guard refuses the task file) and could run a second command beside it. The
timeout must lie above the longest each window command can run by its own timeouts: the probe set is simulated with
every network command taking its full timeout, and `lock` retries its push until the window's deadline.
"""
import datetime as dt
import json
import shlex

from ga import clock, frozen, hours, probes
from guardkit import GUARD, ROOT, SETTINGS, load
from synth import FakeClock

EXPECTED = (
    '{\n'
    '  "hooks": {\n'
    '    "PreToolUse": [\n'
    '      {\n'
    '        "matcher": "*",\n'
    '        "hooks": [\n'
    '          {\n'
    '            "type": "command",\n'
    '            "command": "python3 -I \\"${CLAUDE_PROJECT_DIR}/.claude/hooks/guard.py\\"",\n'
    '            "timeout": 30,\n'
    '            "onFailure": "block"\n'
    '          }\n'
    '        ]\n'
    '      }\n'
    '    ]\n'
    '  },\n'
    '  "permissions": {\n'
    '    "deny": ["WebFetch", "WebSearch", "Agent", "Skill"]\n'
    '  },\n'
    '  "env": {\n'
    '    "BASH_DEFAULT_TIMEOUT_MS": "3600000",\n'
    '    "BASH_MAX_TIMEOUT_MS": "3600000"\n'
    '  }\n'
    '}\n'
)
BASH_TIMEOUT_MS = 3600000


def test_the_settings_file_is_the_registers_block_byte_for_byte():
    assert SETTINGS.read_bytes() == EXPECTED.encode("ascii")


def test_the_settings_object():
    s = json.loads(SETTINGS.read_text("utf-8"))
    assert set(s) == {"hooks", "permissions", "env"}
    assert set(s["hooks"]) == {"PreToolUse"}
    [entry] = s["hooks"]["PreToolUse"]
    assert entry["matcher"] == "*" and set(entry) == {"matcher", "hooks"}
    [hook] = entry["hooks"]
    assert hook == {"type": "command", "command": 'python3 -I "${CLAUDE_PROJECT_DIR}/.claude/hooks/guard.py"',
                    "timeout": 30, "onFailure": "block"}
    assert s["permissions"] == {"deny": ["WebFetch", "WebSearch", "Agent", "Skill"]}
    assert s["env"] == {"BASH_DEFAULT_TIMEOUT_MS": str(BASH_TIMEOUT_MS), "BASH_MAX_TIMEOUT_MS": str(BASH_TIMEOUT_MS)}


def test_the_command_names_the_guard_under_the_project_dir_with_isolated_python():
    argv = shlex.split(json.loads(SETTINGS.read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"])
    assert argv == ["python3", "-I", "${CLAUDE_PROJECT_DIR}/.claude/hooks/guard.py"]
    assert (ROOT / argv[2].replace("${CLAUDE_PROJECT_DIR}/", "")).resolve() == GUARD.resolve()


def test_the_guard_and_the_probe_prompt_are_frozen():
    lines = (ROOT / "analyst" / "FROZEN_ANALYST.sha256").read_text().splitlines()
    paths = {l.split("  ", 1)[1] for l in lines}
    assert {".claude/settings.json", ".claude/hooks/guard.py", "analyst/PROBE_SESSION_PROMPT.md"} <= paths
    assert {".claude/settings.json", ".claude/hooks/guard.py", "analyst/PROBE_SESSION_PROMPT.md"} <= \
        set(frozen.frozen_paths(ROOT))
    assert frozen.verify(ROOT) == []


# ---------------------------------------------------------------- command time (second review of A7, decision 20)
class SlowWorld:
    """Every network command of the probe set takes its full timeout on a fake clock: an HTTP probe 8 s, a REST call
    20 s, a git command 30 s. The pushes that must land to unlock later probes (the c3 push, branch_delete's
    creation) land just as their timeout ends; every other command times out. Local git work is free."""

    def __init__(self):
        self.clk = FakeClock()
        self.steps = []

    # the git object
    def try_cmd(self, *args, timeout):
        self.clk.sleep(timeout)
        lands = args[0] == "push" and any("probe-c3-" in a or "probe-del-" in a for a in args) and \
            "--delete" not in args
        return (0, b"") if lands else (None, b"")

    def remote_url(self):
        return "https://proxy.invalid/git/Mohamedalhaaj/gold-analyst-inbox"

    def tree_of(self, rev):
        return "t" * 40

    def commit_time(self, rev):
        return 1761300000

    def first_parent(self, rev):
        return "p" * 40

    def commit_tree_at(self, *a, **k):
        return "c" * 40

    def commit_one_file(self, *a, **k):
        return "w" * 40

    # gh and HTTP
    def gh(self, args, timeout):
        self.clk.sleep(timeout)
        return None, b"", b""

    def http_get(self, url, timeout):
        self.clk.sleep(timeout)
        return 0, None


def probe_set_seconds(kind, budget_s=None, only=None):
    w = SlowWorld()
    pr = probes.Prober(w, w.gh, w.http_get, w.clk.mono, w.clk.sleep,
                       lambda: dt.datetime(2026, 10, 26, 6, 0, tzinfo=dt.timezone.utc),
                       "Mohamedalhaaj/gold-analyst-inbox", "h" * 40, kind, "p20261026T060000Z", budget_s=budget_s)
    if only == "branch_delete":
        pr.t0 = w.clk.mono()
        pr._branch_delete()
    else:
        pr.run()
    return w.clk.t


def test_the_guard_refuses_a_timeout_below_the_settings_value():
    env = json.loads(SETTINGS.read_text("utf-8"))["env"]
    assert load().BASH_TIMEOUT_MIN_MS == int(env["BASH_DEFAULT_TIMEOUT_MS"]) == int(env["BASH_MAX_TIMEOUT_MS"])


def test_the_probe_set_at_its_full_timeouts_takes_1262_seconds():
    """R-AN-85 "Command time": 4 HTTP probes of 8 s, 8 REST calls of 20 s, 15 git commands of 30 s, and the activity
    polling, which ends at its first poll from 600 s on (620 s)."""
    assert (len(probes.REST), probes.REST_TIMEOUT_S) == (8, 20)
    assert probes.HTTP_TIMEOUT_S == 8 and probes.GIT_TIMEOUT_S == 30
    assert probes.SEEN_BOUND_S["probe"] == 600 and probes.SEEN_POLL_S == 5
    assert probe_set_seconds("probe") == 4 * 8 + 8 * 20 + 15 * 30 + 620 == 1262


def test_the_bash_timeout_covers_every_window_command():
    t_s = BASH_TIMEOUT_MS / 1000
    # probe: the set, then the result's push retried for 3 minutes (10 s apart), each try at least one push
    probe_s = probe_set_seconds("probe") + 3 * 60 + 10
    # a canary context: the hour fetches (no request starts once the 120 s budget is spent; one request of 20 s and
    # the 1.5 s pacing past it), then the canary probe set (no probe starts after 120 s; the longest, branch_delete
    # with its 20 s polling bound, takes 80 s)
    fetch_s = hours.BUDGET_S + hours.TIMEOUT_S + hours.PACE_S
    assert probe_set_seconds("canary", only="branch_delete") == 80
    context_s = fetch_s + probes.CANARY_BUDGET_S + 80
    # lock: the call's push retried until the deadline, at most 30 minutes after the start interval opens
    w = clock.windows_on(dt.date(2026, 10, 26))[0]
    lock_s = (w.deadline_utc - w.start_interval(0)[0]).total_seconds() + 10
    assert lock_s == 30 * 60 + 10
    assert (round(probe_s), round(context_s), round(lock_s)) == (1452, 342, 1810)
    # every bound, with at least 10 minutes left for the git commands that have no timeout of their own
    assert max(probe_s, context_s, lock_s) + 600 <= t_s
