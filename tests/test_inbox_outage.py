"""GitHub outages and the closer (register §16.18 A6 after its second review: GA-B1, SEC-3; R-AN-32, R-AN-36,
R-AN-39, R-AN-40).

- A pushed commit that cannot be fetched while the inbox itself does not answer (`git fetch <inbox> refs/heads/main`
  fails too) is a GitHub outage, not an agent fault: the run stops with nothing posted for that push, logs
  `inbox_fetch=unreachable` and exits 1; the next run retries. Only a commit that cannot be fetched while the inbox
  answers is `agent_scope`. The same holds when a comment without its file is completed: no second witness.
- analyst-settle stops, with a logged enum, at a `called` terminal whose call file is not on main.
- The closer closes only windows the run's push range covers: span start (entry − 45 min) at or after
  max(PINS.inbox_from_utc, now − 28 days). An uncovered open window stays open (`stale_window <call_id>`) and so do
  the later windows of its mode (the chain); a push for an uncovered open window is not processed. A Saturday older
  than the range is not marked `skipped` (`stale_week <iso_week>`).
- A window with an accepted call witness and no terminal (a run that failed between the call witness and the terminal
  witness, or a call whose chain waited) gets the terminal R-AN-36 gives that call, writer `merge`; never the
  closer's `no_call:run`.
"""
import datetime as dt
import subprocess

import pytest

from ga import clock, ghapi, merge, settle_run
from harness import PLAN, Harness, pins_with
from synth import repo_files, store_files

W1 = "2026-11-10_W1"
W1_TERM = f"analyst/canary/terminal/{W1}.json"
CANARY_ATT = f"analyst/canary/attempts/{W1}_1.json"
LIVE_TERM = f"analyst/terminal/{W1}.json"
LIVE_CALL = f"analyst/calls/{W1}.json"


class InboxOutage:
    """subprocess.run for the merge workspace, except that while `down` every git command that names the inbox (the
    fetch of a pushed commit, or of inbox main) fails, as during a GitHub git incident."""

    def __init__(self, inbox):
        self.inbox = str(inbox)
        self.down = False
        self.cmds = []

    def __call__(self, argv, **kw):
        self.cmds.append(list(argv))
        if self.down and self.inbox in argv:
            return subprocess.CompletedProcess(argv, 128, b"", b"fatal: unable to access: 503")
        return subprocess.run(argv, **kw)


class LostCommits:
    """The inbox answers (a fetch of refs/heads/main works), but no pushed commit can be fetched."""

    def __call__(self, argv, **kw):
        if "fetch" in argv and "refs/heads/main" not in argv:
            return subprocess.CompletedProcess(argv, 128, b"", b"fatal: remote error: upload-pack: not our ref")
        return subprocess.run(argv, **kw)


def call_flow(h):
    """W1 2026-11-10 (GMT: entry 08:00, deadline 07:45): the attempt pushed at 07:20 and witnessed at 07:25; the call
    locked and pushed at 07:44:30, not yet witnessed."""
    assert h.run("start") == 0
    h.register_pushes()
    h.at(2026, 11, 10, 7, 25)
    h.merge()
    h.at(2026, 11, 10, 7, 26)
    assert h.run("context") == 0
    h.at(2026, 11, 10, 7, 44, 30)
    assert h.run("lock", str(h.write_plan(PLAN))) == 0
    h.register_pushes()


def settle(h, run_id, log=None):
    return settle_run.run_settle(h.workspace(f"settle{run_id}"), h.api, h.now,
                                 {"workflow": "analyst-settle", "id": run_id, "attempt": 1, "event": "schedule"},
                                 log=(log.append if log is not None else (lambda s: None)))


# ---------------------------------------------------------------- GA-B1: an outage is not an agent fault
def test_an_inbox_outage_while_a_call_is_fetched_delays_it_and_keeps_its_lock(tmp_path):
    h = Harness(tmp_path)
    h.runner = InboxOutage(h.world.inbox)
    call_flow(h)
    comments, dispatched, before = len(h.api.issue_comments), list(h.api.dispatched), h.world.main_sha()
    h.at(2026, 11, 10, 7, 50)
    h.runner.down = True
    with pytest.raises(merge.InboxUnavailable):
        h.merge()
    assert len(h.api.issue_comments) == comments and h.api.dispatched == dispatched
    assert h.world.main_sha() == before and "inbox_fetch=unreachable" in h.merge_log
    h.runner.down = False
    h.at(2026, 11, 10, 8, 10)                                   # after deadline + 20 min
    h.merge()
    t = h.main_json(W1_TERM)
    assert (t["status"], t["writer"], t["call"]["lock_utc"]) == ("called", "merge", "2026-11-10T07:44:30Z")
    assert [(w["kind"], w["status"], w["reason"]) for w in h.witnesses()] == [
        ("attempt", "accepted", None), ("call", "accepted", None), ("terminal", "accepted", None)]


def test_a_completion_during_an_inbox_outage_posts_nothing_and_completes_later(tmp_path):
    h = Harness(tmp_path)
    h.runner = InboxOutage(h.world.inbox)
    assert h.run("start") == 0
    h.register_pushes()
    h.merge_fail = True
    with pytest.raises(RuntimeError):
        h.merge()                                               # the witness comment is out; the push to main failed
    h.merge_fail = False
    h.runner.down = True
    h.at(2026, 11, 10, 7, 30)
    with pytest.raises(merge.InboxUnavailable):
        h.merge()
    assert len(h.api.issue_comments) == 1 and h.world.main_file(CANARY_ATT) is None
    h.runner.down = False
    h.at(2026, 11, 10, 7, 35)
    h.merge()
    assert [(w["status"], w["reason"]) for w in h.witnesses()] == [("accepted", None)]
    assert h.main_json(CANARY_ATT) is not None and len(h.api.issue_comments) == 1


def test_a_live_lock_completed_after_an_outage_is_settled(tmp_path):
    h = Harness(tmp_path, mode="live", canary_from=None, live_from="2026-11-10")
    h.runner = InboxOutage(h.world.inbox)
    assert h.run("start") == 0
    h.push_and_merge()
    h.at(2026, 11, 10, 7, 21)
    assert h.run("context") == 0
    h.at(2026, 11, 10, 7, 25)
    assert h.run("lock", str(h.write_plan(PLAN))) == 0
    h.register_pushes()
    h.merge_fail = True
    with pytest.raises(RuntimeError):
        h.merge()                                               # call and terminal comments out, nothing on main
    h.merge_fail = False
    h.runner.down = True
    h.at(2026, 11, 10, 7, 35)
    with pytest.raises(merge.InboxUnavailable):
        h.merge()
    assert h.world.main_file(LIVE_TERM) is None
    h.runner.down = False
    h.at(2026, 11, 10, 7, 40)
    h.merge()
    assert h.main_json(LIVE_TERM)["status"] == "called" and h.main_json(LIVE_CALL) is not None
    assert [w["reason"] for w in h.witnesses("live")] == [None, None, None]
    h.at(2026, 11, 11, 9, 0)
    h.world.owner_commit(store_files(clock.days_range(dt.date(2026, 9, 1), dt.date(2026, 11, 10))), "mirror")
    settle(h, 90)
    assert h.main_json(f"analyst/settled/{W1}.json") is not None


def test_a_commit_lost_while_the_inbox_answers_is_still_agent_scope(tmp_path):
    h = Harness(tmp_path)
    h.runner = LostCommits()
    assert h.run("start") == 0
    h.push_and_merge()
    w = h.last_witness()
    assert (w["kind"], w["status"], w["reason"], w["path"]) == ("push", "rejected", "agent_scope", None)


def test_the_reachability_check_is_a_fetch_of_inbox_main_without_credentials(tmp_path):
    h = Harness(tmp_path)
    assert h.run("start") == 0
    h.register_pushes()
    out = InboxOutage(h.world.inbox)
    out.down = True
    sleeps, timeouts = [], []

    def runner(argv, **kw):
        if "fetch" in argv:
            timeouts.append(kw.get("timeout"))
        return out(argv, **kw)
    ws = merge.Workspace(h.world.clone("m"), runner=runner, inbox_url=str(h.world.inbox), sleep=sleeps.append)
    log = []
    with pytest.raises(merge.InboxUnavailable):
        merge.run_merge(ws, h.api, h.now, {"workflow": "analyst-merge", "id": 1, "attempt": 1, "event": "schedule"},
                        log=log.append)
    checks = [c for c in out.cmds if "fetch" in c and c[-1] == "refs/heads/main"]
    assert len(checks) == 3
    i = checks[0].index("fetch")
    assert checks[0][i - 4:i] == ["-c", "http.https://github.com/.extraheader=", "-c", "protocol.version=2"]
    assert checks[0][i + 1:] == ["--quiet", "--no-tags", str(h.world.inbox), "refs/heads/main"]
    assert timeouts == [180] * 6 + [30] * 3 and sleeps == [20, 20, 20, 20]
    assert "inbox_fetch=unreachable" in log and h.api.issue_comments == []


def test_settle_stops_at_a_called_terminal_without_its_call_file(tmp_path):
    h = Harness(tmp_path, mode="live", canary_from=None, live_from="2026-11-10")
    assert h.run("start") == 0
    h.push_and_merge()
    h.at(2026, 11, 10, 7, 21)
    assert h.run("context") == 0
    h.at(2026, 11, 10, 7, 25)
    assert h.run("lock", str(h.write_plan(PLAN))) == 0
    h.register_pushes()
    h.merge_fail = True
    with pytest.raises(RuntimeError):
        h.merge()
    h.merge_fail = False
    h.runner = LostCommits()                                    # the call's bytes can never be reproduced
    h.at(2026, 11, 10, 7, 35)
    h.merge()
    assert h.main_json(LIVE_TERM)["status"] == "called" and h.main_json(LIVE_CALL) is None
    h.runner = None
    h.at(2026, 11, 11, 9, 0)
    h.world.owner_commit(store_files(clock.days_range(dt.date(2026, 9, 1), dt.date(2026, 11, 10))), "mirror")
    for i in range(2):
        log = []
        st, done = settle(h, 90 + i, log)                        # no exception, on every run
        assert f"call_file_missing {W1}" in log and done == []
    assert h.main_json(f"analyst/settled/{W1}.json") is None


# ---------------------------------------------------------------- SEC-3: the push range bounds the closer
def test_a_timely_lock_survives_a_15_day_read_outage(tmp_path):
    h = Harness(tmp_path, slots_until=dt.date(2026, 12, 4))
    h.at(2026, 11, 10, 7, 20)
    call_flow_unmerged(h)
    h.at(2026, 11, 25, 8, 0)                                    # every run since 07:20 on 11-10 failed its read
    h.merge()
    t = h.main_json(W1_TERM)
    assert (t["status"], t["writer"], t["call"]["lock_utc"]) == ("called", "merge", "2026-11-10T07:44:30Z")


def call_flow_unmerged(h):
    """As call_flow, with no merge run at all: the attempt at 07:20, the call at 07:44:30."""
    assert h.run("start") == 0
    h.register_pushes()
    h.at(2026, 11, 10, 7, 21)
    assert h.run("context") == 0
    h.at(2026, 11, 10, 7, 44, 30)
    assert h.run("lock", str(h.write_plan(PLAN))) == 0
    h.register_pushes()


def test_after_an_outage_longer_than_28_days_the_window_stays_open(tmp_path):
    h = Harness(tmp_path, slots_until=dt.date(2026, 12, 31))
    call_flow_unmerged(h)
    h.at(2026, 12, 9, 8, 0)                                     # 29 days later: the pushes are out of range
    h.merge()
    assert h.world.main_list("analyst/canary/terminal/") == []
    assert f"stale_window {W1}" in h.merge_log
    assert h.witnesses() == [] and h.api.issue_comments == []


def test_with_inbox_from_utc_unset_no_window_is_closed_and_no_week_skipped(tmp_path):
    h = Harness(tmp_path, inbox_from_utc=None)
    assert h.run("start") == 0
    h.register_pushes()
    h.at(2026, 11, 15, 12, 0)                                   # past every deadline of the week, and its Saturday
    h.merge()
    assert h.world.main_list("analyst/canary/terminal/") == []
    assert h.world.main_list("analyst/canary/revisions/") == []
    assert f"stale_window {W1}" in h.merge_log and "stale_week 2026-W46" in h.merge_log


def test_a_push_for_an_uncovered_window_is_not_processed(tmp_path):
    """inbox_from_utc between the attempt and the call: the window straddles the range start. Its call is not judged
    (it would have been `no_call:invalid` for want of an attempt the merge never read): no witness, no terminal."""
    h = Harness(tmp_path, inbox_from_utc="2026-11-10T07:30:00Z")
    call_flow_unmerged(h)
    h.at(2026, 11, 10, 8, 10)
    h.merge()
    assert h.api.issue_comments == [] and h.world.main_list("analyst/canary/terminal/") == []
    assert f"stale_window {W1}" in h.merge_log


def test_a_call_whose_chain_is_blocked_is_witnessed_and_its_terminal_waits(tmp_path):
    """canary_from 11-09 but inbox_from_utc 11-10 00:00: the 11-09 windows are open and uncovered. The 11-10 W1 call is
    witnessed with its lock time; its terminal, which chains to 11-09 W2, waits. Once the 11-09 windows are covered
    (here by an owner commit moving inbox_from_utc, standing in for the owner's remedy), the next run closes them
    and writes W1's terminal from its call witness."""
    h = Harness(tmp_path, canary_from="2026-11-09", inbox_from_utc="2026-11-10T00:00:00Z")
    call_flow(h)
    h.at(2026, 11, 10, 7, 50)
    h.merge()
    assert [(w["kind"], w["status"]) for w in h.witnesses()] == [("attempt", "accepted"), ("call", "accepted")]
    assert h.world.main_list("analyst/canary/terminal/") == []
    assert "stale_window 2026-11-09_W1" in h.merge_log
    files = repo_files()
    h.world.owner_commit({"analyst/PINS.json": pins_with(files, inbox_from_utc="2026-11-01T00:00:00Z")})
    h.at(2026, 11, 10, 8, 10)
    h.merge()
    t = h.main_json(W1_TERM)
    assert (t["status"], t["writer"], t["call"]["lock_utc"]) == ("called", "merge", "2026-11-10T07:44:30Z")
    assert h.main_json("analyst/canary/terminal/2026-11-09_W2.json")["status"] == "missed:trigger"


def test_a_saturday_older_than_the_range_is_not_marked_skipped(tmp_path):
    h = Harness(tmp_path, slots_until=dt.date(2026, 12, 31))
    h.at(2026, 12, 16, 9, 0)                                    # the range starts 11-18 09:00
    h.merge()
    assert "stale_week 2026-W46" in h.merge_log                # Saturday 11-14
    assert h.world.main_file("analyst/canary/revisions/2026-W46.json") is None
    assert h.main_json("analyst/canary/revisions/2026-W47.json")["status"] == "skipped"


# ---------------------------------------------------------------- an accepted call always gets its own terminal
def test_a_run_that_failed_between_the_call_and_terminal_witnesses_is_completed_with_the_calls_terminal(tmp_path):
    h = Harness(tmp_path)
    call_flow(h)
    real = h.api.post_comment

    def no_terminal(issue, body):
        if body.startswith("analyst-witness/1 terminal "):
            raise ghapi.GitHubError("POST comments -> 502")
        return real(issue, body)
    h.api.post_comment = no_terminal
    h.at(2026, 11, 10, 7, 45, 30)
    with pytest.raises(ghapi.GitHubError):
        h.merge()
    h.api.post_comment = real
    h.at(2026, 11, 10, 7, 50)                                   # before deadline + 20 min
    h.merge()
    t = h.main_json(W1_TERM)
    assert (t["status"], t["writer"], t["call"]["lock_utc"]) == ("called", "merge", "2026-11-10T07:44:30Z")
    tw = h.last_witness()
    assert tw["kind"] == "terminal" and tw["push"]["after"] == t["call"]["push_sha"]


def test_the_jobs_exit_1_on_an_unreachable_inbox(tmp_path, monkeypatch):
    import merge_job
    import settle_job

    def down(*a, **k):
        raise merge.InboxUnavailable("inbox")
    env = {"GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "schedule", "GITHUB_REPOSITORY": "o/r",
           "GITHUB_RUN_ID": "1"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(merge, "run_merge", down)
    monkeypatch.setattr(settle_run, "run_settle", down)
    monkeypatch.setattr(settle_run, "fetch_verdict", lambda pins: None)
    monkeypatch.setattr(merge, "Workspace", lambda *a, **k: type("W", (), {"view": None})())
    monkeypatch.setattr(merge_job.paths, "read_pins", lambda v: {})
    assert merge_job.main() == 1 and settle_job.main() == 1


def test_the_push_range_is_inside_the_month_read():
    """28 days: never longer than the activity read's `time_period=month`, whatever a month GitHub counts."""
    assert merge.WINDOW_DAYS == 28
