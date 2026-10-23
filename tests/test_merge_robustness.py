"""A malformed push to claude/analyst-* never halts analyst-merge or analyst-settle (review blocker B2; register
R-AN-33, R-AN-39 and the dated note of 2026-10-23 in §16).

Every failure caused by what a push contains (an orphan or unfetchable commit, a path whose call_id names no window,
a file that breaks a content check in an unexpected way, a witness comment whose agent file can no longer be fetched)
becomes a rejected witness: `agent_scope` when the push's diff cannot be shown to add exactly one allowed path,
`invalid_content` when the path is allowed but its content cannot be validated or recovered. The run goes on, so the
other windows still get their witnesses, terminal records and settlement. Adapted from the review's probes
(scratchpad review/probe_tests/test_review_probes.py)."""
import json

import pytest

from ga import merge, rec, records, settle_run
from ga.repo import DirView
from harness import Harness
from synth import git

W1 = "2026-11-10_W1"
CANARY_ATT = "analyst/canary/attempts/2026-11-10_W1_1.json"


def attempt_bytes(h, draw, n=1):
    return rec.record_bytes(records.attempt("call", "canary", draw, n, draw, "x" * 40,
                                            records.frozen_block(DirView(h.world.seed)), {"note": "x"}))


def legit_attempt_still_lands(h):
    """After the malformed push, a legitimate start in the same span is witnessed and copied to main."""
    assert h.run("start") == 0
    h.push_and_merge()
    assert h.world.main_file(CANARY_ATT) is not None
    w = h.last_witness()
    assert (w["kind"], w["id"], w["status"]) == ("attempt", W1, "accepted")


def settle_runs(h, run_id=77):
    ws = h.workspace(f"settle{run_id}")
    return settle_run.run_settle(ws, h.api, h.now, {"workflow": "analyst-settle", "id": run_id, "attempt": 1,
                                                    "event": "schedule"}, verdict=None)


def rejections(h):
    return [w for w in h.witnesses() if w["status"] == "rejected"]


def test_orphan_push_is_agent_scope_and_merge_goes_on(tmp_path):
    h = Harness(tmp_path)
    d = h.world.clone_inbox("orphan")
    git(d, "checkout", "-q", "--orphan", "o")
    git(d, "rm", "-rq", "--cached", ".")
    p = d / CANARY_ATT
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(attempt_bytes(h, W1))
    git(d, "add", CANARY_ATT)
    git(d, "commit", "-q", "-m", "orphan")
    git(d, "push", "-q", "-f", "origin", "HEAD:refs/heads/claude/analyst-orphan")
    [push] = h.register_pushes()
    h.merge()                                                                      # no exception
    w = h.last_witness()
    assert (w["kind"], w["id"], w["status"], w["reason"]) == ("push", None, "rejected", "agent_scope")
    assert w["push"]["after"] == push[1] and w["writes"] == []
    assert h.world.main_list("analyst/canary/attempts/") == []
    legit_attempt_still_lands(h)                                                   # the closer skips the orphan now
    assert len(rejections(h)) == 1


@pytest.mark.parametrize("path", [
    "analyst/canary/attempts/2026-02-30_W1_1.json",       # no such date
    "analyst/canary/attempts/2026-11-14_W1_1.json",       # a Saturday: no window
    "analyst/canary/calls/2026-02-30_W2.json",
    "analyst/canary/lessons/2026-13-01_W1.json",
    "analyst/canary/attempts/lesson-2026-02-29_W1_1.json",
])
def test_a_path_naming_no_window_is_agent_scope(tmp_path, path):
    h = Harness(tmp_path)
    cid = path.rsplit("/", 1)[1].split(".")[0]
    cid = cid[len("lesson-"):] if cid.startswith("lesson-") else cid
    cid = cid[:13]
    h.agent_branch("claude/analyst-x", {path: attempt_bytes(h, cid)})
    h.push_and_merge()                                                             # no exception
    w = h.last_witness()
    assert (w["kind"], w["status"], w["reason"], w["path"]) == ("push", "rejected", "agent_scope", path)
    assert h.world.main_file(path) is None
    legit_attempt_still_lands(h)


def test_unfetchable_commit_of_a_deleted_branch_is_agent_scope(tmp_path):
    """The agent pushes, deletes the branch before any merge run, and the commit is gone from the remote: the closer
    of every later run used to raise on it."""
    h = Harness(tmp_path)
    sha = h.agent_branch("claude/analyst-gone", {CANARY_ATT: attempt_bytes(h, W1)})
    h.register_pushes()                                                            # the merge run never happened
    git(h.world.inbox, "update-ref", "-d", "refs/heads/claude/analyst-gone")      # (the inbox's rules refuse this;
    git(h.world.inbox, "reflog", "expire", "--expire=now", "--all")                 # the test removes it by hand)
    git(h.world.inbox, "gc", "-q", "--prune=now")
    assert git(h.world.inbox, "cat-file", "-t", sha, check=False) != "commit"
    h.at(2026, 11, 10, 7, 22)
    h.merge()                                                                      # closer step 1: no exception
    w = h.last_witness()
    assert (w["kind"], w["status"], w["reason"], w["push"]["after"]) == ("push", "rejected", "agent_scope", sha)
    legit_attempt_still_lands(h)
    st, _done = settle_runs(h)                                                     # settle's closer: no exception
    assert st["calls_enabled"] is True


def test_an_unexpected_content_failure_is_invalid_content(tmp_path):
    """A call whose context holds NaN: the content check itself raised (compact form refuses NaN)."""
    h = Harness(tmp_path)
    assert h.run("start") == 0
    h.push_and_merge()
    doc = {k: None for k in ("schema", "call_id", "mode", "window", "model_day", "entry_utc", "exit_utc",
                             "deadline_utc", "status", "detail", "attempt_n", "decision_utc", "context_sha256",
                             "context", "playbook", "plan", "plan_sha256", "d_ref", "side", "stop_mult",
                             "target_mult", "S", "T", "S_dimes", "T_dimes", "lock_validation_failures", "request",
                             "frozen", "repo_head_sha", "self_reported")}
    doc.update({"schema": records.CALL_SCHEMA, "call_id": W1, "mode": "canary", "status": "called", "attempt_n": 1,
                "context": {"call_id": W1, "x": float("nan")}, "context_sha256": "0" * 64})
    body = json.dumps(doc, sort_keys=True, indent=1).encode() + b"\n"             # NaN written as the literal NaN
    assert b"NaN" in body
    h.at(2026, 11, 10, 7, 25)
    h.agent_branch("claude/analyst-nan", {"analyst/canary/calls/2026-11-10_W1.json": body})
    h.push_and_merge()                                                             # no exception
    w = h.last_witness()
    assert (w["kind"], w["id"], w["status"], w["reason"]) == ("call", W1, "rejected", "invalid_content")
    assert h.world.main_file("analyst/canary/calls/2026-11-10_W1.json") is None
    assert h.main_json("analyst/canary/terminal/2026-11-10_W1.json") is None


def test_any_unexpected_handler_failure_before_a_witness_is_invalid_content(tmp_path, monkeypatch):
    h = Harness(tmp_path)

    def boom(doc, *a, **k):
        raise TypeError("unexpected")

    assert h.run("start") == 0
    monkeypatch.setattr(records, "valid_attempt", boom)                            # the merge side only
    h.push_and_merge()                                                             # no exception
    w = h.last_witness()
    assert (w["kind"], w["id"], w["status"], w["reason"]) == ("attempt", W1, "rejected", "invalid_content")
    assert h.world.main_file(CANARY_ATT) is None


def test_a_failure_after_the_witness_was_posted_is_not_rewritten_as_a_rejection(tmp_path, monkeypatch):
    """Once a witness comment is out, the run must fail loudly (the next run completes it from the comment); a second,
    contradictory witness for the same push is never posted."""
    h = Harness(tmp_path)
    real = merge.Merge.post_witness
    calls = {"n": 0}

    def post_then_fail(self, doc, mode):
        out = real(self, doc, mode)
        calls["n"] += 1
        raise RuntimeError("after the comment")

    monkeypatch.setattr(merge.Merge, "post_witness", post_then_fail)
    assert h.run("start") == 0
    with pytest.raises(RuntimeError):
        h.push_and_merge()
    assert calls["n"] == 1 and len(h.api.issue_comments) == 1
    monkeypatch.setattr(merge.Merge, "post_witness", real)
    h.merge()                                                                      # completed from the comment
    assert h.world.main_file(CANARY_ATT) is not None
    assert [w["status"] for w in h.witnesses()] == ["accepted"]


def test_a_comment_whose_agent_file_cannot_be_fetched_does_not_halt(tmp_path):
    """The merge run posted the witness comment, then failed to push; the agent deleted its branch and the commit is
    gone. Later runs used to raise `cannot complete witness`. Now the witness file is completed from the comment, the
    missing file is logged as an `invalid_content` witness for the same push, and the runs go on."""
    h = Harness(tmp_path)
    assert h.run("start") == 0
    h.merge_fail = True
    [(ref, sha)] = h.register_pushes()
    with pytest.raises(RuntimeError):
        h.merge()
    h.merge_fail = False
    git(h.world.inbox, "update-ref", "-d", ref)
    git(h.world.inbox, "reflog", "expire", "--expire=now", "--all")
    git(h.world.inbox, "gc", "-q", "--prune=now")
    h.at(2026, 11, 10, 8, 10)                                                      # after W1's deadline + 20 min
    h.merge()                                                                      # no exception
    ws = h.witnesses()
    assert [(w["seq"], w["kind"], w["status"], w["reason"]) for w in ws[:2]] == [
        (1, "attempt", "accepted", None), (2, "attempt", "rejected", "invalid_content")]
    assert ws[0]["comment"]["id"] == h.api.issue_comments[0]["id"]
    assert ws[1]["push"]["after"] == sha and ws[1]["path"] == CANARY_ATT and ws[1]["id"] == W1
    assert h.world.main_file(CANARY_ATT) is None
    assert h.main_json("analyst/canary/terminal/2026-11-10_W1.json")["status"] == "no_call:run"
    n = len(h.api.issue_comments)
    h.merge()                                                                      # idempotent: nothing re-posted
    assert len(h.api.issue_comments) == n
    settle_runs(h)                                                                 # settle's own completion pass


def test_settle_goes_on_after_malformed_pushes(tmp_path):
    """The review's failure: one malformed push stopped analyst-settle (it runs the closer first) for 14 days."""
    h = Harness(tmp_path, canary_from="2026-11-09")
    bad = "2026-02-30_W1"
    h.agent_branch("claude/analyst-bad", {f"analyst/canary/attempts/{bad}_1.json": attempt_bytes(h, bad)})
    h.register_pushes()
    h.at(2026, 11, 10, 9, 0)
    st, _done = settle_runs(h)                                                     # no exception
    assert st["calls_enabled"] is True
    assert [w["reason"] for w in rejections(h)] == ["agent_scope"]
    for cid in ("2026-11-09_W1", "2026-11-09_W2", "2026-11-10_W1"):               # the closer still closed these
        assert h.main_json(f"analyst/canary/terminal/{cid}.json")["status"] == "missed:trigger", cid
    assert h.main_json("analyst/canary/terminal/2026-11-09_W1.json")["writer"] == "closer"


def test_a_derivability_check_that_raises_ends_as_underivable(tmp_path, monkeypatch):
    """The call witness is already out when S/T/D_ref are re-derived (R-AN-36 step 4), so a raising check must end
    in the terminal record `no_call:invalid` / `underivable`, not in an exception."""
    from ga import plan
    from harness import PLAN
    h = Harness(tmp_path)
    assert h.run("start") == 0
    h.push_and_merge()
    h.at(2026, 11, 10, 7, 21)
    assert h.run("context") == 0
    h.at(2026, 11, 10, 7, 25)
    assert h.run("lock", str(h.write_plan(PLAN))) == 0

    def boom(doc):
        raise ZeroDivisionError("unexpected")

    monkeypatch.setattr(plan, "derivable", boom)                                    # the merge side only
    h.push_and_merge()                                                             # no exception
    t = h.main_json("analyst/canary/terminal/2026-11-10_W1.json")
    assert (t["status"], t["detail"]) == ("no_call:invalid", "underivable")


def test_no_second_lock_with_another_plan(tmp_path):
    """The review's third probe, with assertions: after a lock, a session that resets its scratch state cannot lock
    another plan (`refused closed`), and a hand-made push of another call file for the window is a VIOLATION code."""
    from harness import PLAN
    h = Harness(tmp_path)
    assert h.run("start") == 0
    h.push_and_merge()
    h.at(2026, 11, 10, 7, 21)
    assert h.run("context") == 0
    h.at(2026, 11, 10, 7, 25)
    assert h.run("lock", str(h.write_plan(PLAN))) == 0
    before_lock = h.world.main_sha()
    h.push_and_merge()
    tpath = "analyst/canary/terminal/2026-11-10_W1.json"
    first = h.world.main_file(tpath)
    assert json.loads(first)["status"] == "called"
    st = h.sess_dir / ".analyst_run" / "state.json"
    s = json.loads(st.read_text())
    s.pop("locked", None)
    s.pop("lock_commit", None)
    st.write_text(json.dumps(s))
    h.run("lock", str(h.write_plan(dict(PLAN, action="sell"))))
    assert h.log[-1] == "refused closed"
    h.push_and_merge()
    assert h.world.main_file(tpath) == first
    cpath = "analyst/canary/calls/2026-11-10_W1.json"
    other = json.loads(h.world.main_file(cpath))
    other["plan"]["action"] = "sell"
    h.agent_branch("claude/analyst-rw", {cpath: rec.record_bytes(other)}, base=before_lock)
    h.push_and_merge()
    assert (h.last_witness()["status"], h.last_witness()["reason"]) == ("rejected", "rewrite")
    h.agent_branch("claude/analyst-m", {cpath: rec.record_bytes(other)})              # on top of main: a modification
    h.push_and_merge()
    assert (h.last_witness()["status"], h.last_witness()["reason"]) == ("rejected", "agent_scope")
    assert h.world.main_file(tpath) == first
