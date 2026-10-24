"""Time budgets of analyst-merge and analyst-settle (register R-AN-32, R-AN-39, R-AN-40; §16.18 A6 fourth review, N1).

- Each round of fetch tries has 240 s from its first fetch command, the 20 s waits included; a fetch command's timeout
  is the smaller of 180 s and the round's time left, and no command starts once it is spent (`fetch_round=budget`).
  The check of inbox main after a round is bounded on its own (3 tries of 30 s, 20 s apart).
- The local git work on one fetched commit (merge-base, diff, ls-tree, cat-file -s, show) shares 60 s; beyond it the
  push is `agent_scope` with path null.
- The complete read of the inbox's activity has 600 s from its first request; beyond it the read has failed.
- Run budget: once 15 minutes have passed since the run's start, no new push and no completion of a comment starts;
  the run logs `run_budget <count left>`, skips steps 2 and 3 of the closer, commits what it wrote and exits 0. The
  next run goes on from there. In analyst-settle, settlement and the state (the pause rule) still run.
So one push costs a run at most 2 x 240 + 2 x 130 + 60 = 800 s before its witness.

The clock is a fake: the git commands, the API and the waits advance it by the seconds a test gives them; a command
that would take as long as its timeout advances it by the timeout and raises TimeoutExpired, as subprocess.run does.
"""
import datetime as dt
import subprocess

import pytest

from ga import ghapi, merge, rec, settle_run
from harness import Harness
from synth import INBOX_REPO, FakeClock

UTC = rec.UTC
W1 = "2026-11-10_W1"
CANARY_ATT = f"analyst/canary/attempts/{W1}_1.json"
RUN = {"workflow": "analyst-merge", "id": 1, "attempt": 1, "event": "schedule"}
INTEG = "A1|call_bytes|analyst/canary/calls/2026-11-10_W1.json|" + "a" * 40
LOCAL = ("merge-base", "diff", "ls-tree", "cat-file", "show")


def kind(argv, inbox):
    if "fetch" in argv and inbox in argv:
        return "main" if argv[-1] == "refs/heads/main" else "commit"
    for k in LOCAL:
        if k in argv:
            if k == "cat-file" and "-s" not in argv:
                return "has_commit"
            if k == "diff" and "--no-renames" not in argv:
                return "diff_cached"
            return k
    return "other"


class SlowGit:
    """subprocess.run on the fake clock: `cost(kind, argv)` -> (seconds, outcome), outcome 'ok' (run the command)
    or 'fail' (exit 1). `seen` lists (kind, timeout) of every command that costs time."""

    def __init__(self, clk, inbox, cost):
        self.clk, self.inbox, self.cost = clk, str(inbox), cost
        self.seen = []

    def __call__(self, argv, **kw):
        k = kind(argv, self.inbox)
        secs, outcome = self.cost(k, argv)
        to = kw.get("timeout")
        if secs:
            self.seen.append((k, to))
            if to is not None and secs >= to:
                self.clk.t += to
                raise subprocess.TimeoutExpired(argv, to)
            self.clk.t += secs
        if outcome == "fail":
            return subprocess.CompletedProcess(argv, 1, b"", b"fatal: x")
        return subprocess.run(argv, **kw)


def pushed_attempt(h):
    assert h.run("start") == 0
    h.register_pushes()


def merge_on(h, clk, runner=None, log=None):
    ws = merge.Workspace(h.world.clone("m"), inbox_url=str(h.world.inbox), sleep=clk.sleep, mono=clk.mono,
                         **({"runner": runner} if runner else {}))
    try:
        return merge.run_merge(ws, h.api, h.now, RUN, log=(log if log is not None else []).append)
    finally:
        h.world.mirror()


# ---------------------------------------------------------------- the round budget
@pytest.mark.parametrize("secs,per_round", [
    (170, [180, 70]),                  # a slow failure; then a fetch cut short by the round's 70 s left times out
    (60, [180, 180, 100, 40]),         # the 20 s wait counts: 60 + 60 + 20 + 60 = 200, so 40 s are left
])
def test_slow_failing_fetches_end_a_round_at_240_s(tmp_path, secs, per_round):
    h = Harness(tmp_path)
    pushed_attempt(h)
    clk = FakeClock()
    run = SlowGit(clk, h.world.inbox, lambda k, a: (secs, "fail") if k == "commit" else (0, "ok"))
    log = []
    merge_on(h, clk, run, log)
    assert [t for k, t in run.seen if k == "commit"] == per_round * 2
    assert clk.t == 480                                                # two rounds of 240 s; the checks answer at once
    assert "fetch_round=budget" in log
    w = h.last_witness()
    assert (w["kind"], w["status"], w["reason"], w["path"]) == ("push", "rejected", "agent_scope", None)


def test_a_push_whose_fetches_all_fail_slowly_is_witnessed_within_800_s(tmp_path):
    """Both rounds spend their 240 s, and each check of inbox main hangs twice (30 s timeouts, 20 s apart) before it
    answers after 29 s: 2 x 240 + 2 x 129 = 738 s, inside the 800 s bound."""
    h = Harness(tmp_path)
    pushed_attempt(h)
    clk = FakeClock()
    checks = iter([30, 30, 29] * 2)

    def cost(k, a):
        if k == "commit":
            return 170, "fail"
        if k == "main":
            return next(checks), "ok"
        return 0, "ok"
    run = SlowGit(clk, h.world.inbox, cost)
    merge_on(h, clk, run)
    assert [t for k, t in run.seen if k == "main"] == [30, 30, 30] * 2
    assert clk.t == 738 <= 800
    w = h.last_witness()
    assert (w["kind"], w["reason"], w["path"]) == ("push", "agent_scope", None)


# ---------------------------------------------------------------- the local budget
def test_the_local_git_work_on_a_push_shares_one_60_s_budget(tmp_path):
    h = Harness(tmp_path)
    pushed_attempt(h)
    clk = FakeClock()
    run = SlowGit(clk, h.world.inbox, lambda k, a: (25, "ok") if k in LOCAL else (0, "ok"))
    merge_on(h, clk, run)
    assert [(k, t) for k, t in run.seen if k in LOCAL] == [("merge-base", 60), ("diff", 35), ("cat-file", 10)]
    assert clk.t == 60
    w = h.last_witness()
    assert (w["kind"], w["status"], w["reason"], w["path"]) == ("push", "rejected", "agent_scope", None)
    assert h.world.main_file(CANARY_ATT) is None


def test_a_fast_push_is_untouched_by_the_budgets(tmp_path):
    h = Harness(tmp_path)
    pushed_attempt(h)
    clk = FakeClock()
    run = SlowGit(clk, h.world.inbox, lambda k, a: (1, "ok") if k in LOCAL + ("commit",) else (0, "ok"))
    merge_on(h, clk, run)
    assert [t for k, t in run.seen if k == "commit"] == [180]
    assert [t for k, t in run.seen if k in LOCAL] == [60, 59, 58, 57, 56]
    w = h.last_witness()
    assert (w["kind"], w["status"]) == ("attempt", "accepted") and h.main_json(CANARY_ATT) is not None


# ---------------------------------------------------------------- the read budget
def test_a_read_slower_than_600_s_fails():
    clk = FakeClock()
    calls = []

    def http(method, url, token, timeout):
        calls.append(timeout)
        if 25 >= timeout:                                              # it would time out
            clk.t += timeout
            return 0, "", b""
        clk.t += 25
        return 200, f'<{ghapi.API}/repositories/1/activity?after={len(calls)}>; rel="next"', b"[]"
    r = ghapi.InboxActivity(INBOX_REPO, token="tok", http=http, sleep=clk.sleep, mono=clk.mono)
    with pytest.raises(ghapi.ActivityReadFailed):
        r.read_activity()
    assert calls == [30] * 23 + [25] and clk.t == 600


def test_the_read_budget_is_far_above_a_normal_read():
    clk = FakeClock()

    def http(method, url, token, timeout):
        clk.t += 2
        return 200, "", b"[]"
    r = ghapi.InboxActivity(INBOX_REPO, token="tok", http=http, sleep=clk.sleep, mono=clk.mono)
    assert r.read_activity() == ([], "token")


# ---------------------------------------------------------------- the run budget
def four_pushes(h):
    """W1's attempt (07:20), then three foreign pushes (07:21-07:23); the time is then past W1's deadline + 20 min."""
    pushed_attempt(h)
    for i in range(3):
        h.at(2026, 11, 10, 7, 21 + i)
        h.agent_branch(f"claude/analyst-junk{i}", {f"junk{i}.txt": b"x"})
        h.register_pushes()
    h.at(2026, 11, 10, 8, 10)


def slow_api(h, clk, secs=500):
    real = h.api.post_comment

    def post(issue, body):
        clk.t += secs
        return real(issue, body)
    h.api.post_comment = post
    return real


def test_a_run_past_its_budget_closes_no_window_commits_its_witnesses_and_the_next_run_goes_on(tmp_path):
    h = Harness(tmp_path)
    four_pushes(h)
    clk = FakeClock()
    real = slow_api(h, clk)
    log = []
    merge_on(h, clk, log=log)                                          # no exception: the job exits 0
    assert "run_budget 2" in log                                       # 0 s and 500 s started; 1000 s did not
    assert [(w["kind"], w["status"], w["reason"]) for w in h.witnesses()] == [
        ("attempt", "accepted", None), ("push", "rejected", "agent_scope")]
    assert h.main_json(CANARY_ATT) is not None
    assert h.world.main_list("analyst/canary/terminal/") == []         # closer steps 2 and 3 skipped
    h.api.post_comment = real
    h.at(2026, 11, 10, 8, 15)
    h.merge()
    assert [(w["kind"], w["reason"]) for w in h.witnesses()] == [
        ("attempt", None), ("push", "agent_scope"), ("push", "agent_scope"), ("push", "agent_scope"),
        ("terminal", None)]
    assert h.main_json(f"analyst/canary/terminal/{W1}.json")["status"] == "no_call:run"


def test_no_completion_starts_past_the_run_budget(tmp_path):
    h = Harness(tmp_path)
    pushed_attempt(h)
    h.merge_fail = True
    with pytest.raises(RuntimeError):
        h.merge()                                                      # the comment is out; nothing on main
    h.merge_fail = False
    clk = FakeClock()
    real = h.api.read_activity

    def slow_read():
        clk.t += 901                                                   # a read that ends after 15 min
        return real()
    h.api.read_activity = slow_read
    log = []
    merge_on(h, clk, log=log)
    assert "run_budget 1" in log and h.world.main_file(CANARY_ATT) is None
    h.api.read_activity = real
    h.merge()
    assert h.main_json(CANARY_ATT) is not None and len(h.api.issue_comments) == 1


def test_settle_past_its_run_budget_still_runs_the_pause_rule(tmp_path):
    h = Harness(tmp_path)
    four_pushes(h)
    clk = FakeClock()
    slow_api(h, clk)
    log = []
    ws = merge.Workspace(h.world.clone("s"), inbox_url=str(h.world.inbox), sleep=clk.sleep, mono=clk.mono)
    v = {"schema": "analyst-verdict/1", "asof_utc": rec.iso(h.now() - dt.timedelta(minutes=5)),
         "analyst_verdict": "VIOLATION", "violation_keys": [INTEG], "cleared_keys": []}
    settle_run.run_settle(ws, h.api, h.now, {"workflow": "analyst-settle", "id": 2, "attempt": 1, "event": "push"},
                          verdict=v, log=log.append, dispatch=False)
    assert "run_budget 2" in log
    st = h.main_json("analyst/STATE.json")
    assert st["pause_reasons"] == [f"judge_violation:{INTEG}"] and st["calls_enabled"] is False
    assert h.world.main_list("analyst/state/") == ["analyst/state/000001.json"]
    assert h.world.main_list("analyst/canary/terminal/") == []


def test_the_budget_constants():
    assert (merge.ROUND_BUDGET_S, merge.LOCAL_BUDGET_S, merge.RUN_BUDGET_S, ghapi.InboxActivity.BUDGET_S) == \
        (240, 60, 900, 600)
    assert 2 * merge.ROUND_BUDGET_S + 2 * (merge.REACH_TRIES * merge.REACH_TIMEOUT_S + 2 * merge.FETCH_RETRY_S) \
        + merge.LOCAL_BUDGET_S == 800
