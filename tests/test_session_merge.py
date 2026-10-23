"""The session scripts and analyst-merge on a local bare repo: draws, attempts, the allowlist, lock evidence, the
witness, the chain, the closer and completeness (prereg R1, R3, §1, §3; register R-AN-24..R-AN-39)."""
import datetime as dt
import json
import re

import pytest

from ga import paths, plan, rec, records
from harness import PLAN, Harness

W1 = "2026-11-10_W1"
CANARY_ATT = "analyst/canary/attempts/2026-11-10_W1_1.json"
CANARY_CALL = "analyst/canary/calls/2026-11-10_W1.json"


def full_call(h, plan_obj=PLAN, lock_t=(2026, 11, 10, 7, 25)):
    assert h.run("start") == 0
    h.push_and_merge()
    h.at(2026, 11, 10, 7, 21)
    assert h.run("context") == 0
    h.at(*lock_t)
    rc = h.run("lock", str(h.write_plan(plan_obj)))
    return rc


def test_canary_end_to_end(tmp_path):
    h = Harness(tmp_path)
    assert h.run("start") == 0
    assert h.log[-1] == "attempt_pushed 2026-11-10_W1 n=1 mode=canary"
    pushes = h.register_pushes()
    assert pushes and pushes[0][0] == "refs/heads/claude/analyst-canary-attempt-2026-11-10_W1-1"
    h.merge(pushes[0])
    att = h.main_json(CANARY_ATT)
    assert records.valid_attempt(att) and att["self_reported"]["note"] == "self-reported, unverified"
    w = h.last_witness()
    assert (w["kind"], w["status"], w["window"]["void"], w["window"]["time_source"]) == \
        ("attempt", "accepted", False, "activity")
    assert w["window"]["time_utc"] == "2026-11-10T07:20:00Z"

    h.at(2026, 11, 10, 7, 21)
    assert h.run("context") == 0 and h.log[-1].startswith("context_ok 2026-11-10_W1")
    st = json.loads((h.sess_dir / ".analyst_run" / "state.json").read_text())
    probe = st["canary_probe"]
    assert probe["push_main"] == "refused" and probe["force_main"] == "refused"
    assert probe["network"]["google"] == 0 and probe["network"]["ls_remote_gfw"] is False
    h.at(2026, 11, 10, 7, 25)
    assert h.run("lock", str(h.write_plan(PLAN))) == 0 and h.log[-1] == "locked 2026-11-10_W1 status=called"
    [p] = h.register_pushes()
    h.merge(p)
    call = h.main_json(CANARY_CALL)
    branch_bytes = __import__("subprocess").run(["git", "-C", str(h.world.origin), "show", f"{p[1]}:{CANARY_CALL}"],
                                                capture_output=True).stdout
    assert h.world.main_file(CANARY_CALL) == branch_bytes                      # copied byte for byte
    assert records.valid_call(call) and plan.derivable(call)
    assert call["status"] == "called" and call["side"] == 1 and call["canary_probe"] == probe
    assert call["request"] == {"runner": "claude-code-routine", "model_set": "claude-opus-5-5", "effort_set": None}
    assert call["decision_utc"] == "2026-11-10T07:21:00Z" and call["attempt_n"] == 1
    t = h.main_json("analyst/canary/terminal/2026-11-10_W1.json")
    assert t["status"] == "called" and t["writer"] == "merge" and t["prev_chain_sha256"] == "0" * 64
    assert t["call"]["lock_utc"] == "2026-11-10T07:25:00Z" and t["call"]["push_sha"] == p[1]
    assert [a["n"] for a in t["attempts"]] == [1]
    assert h.world.main_file("analyst/START.json") is None                     # canary never writes START
    assert h.api.dispatched == []                                              # canary calls are never tracked
    kinds = [x["kind"] for x in h.witnesses()]
    assert kinds == ["attempt", "call", "terminal"]
    # comments: two lines, header, compact payload equal to the file without `comment`
    for c, wdoc in zip(h.api.issue_comments, h.witnesses()):
        head, payload = c["body"].split("\n")
        assert re.match(r"^analyst-witness/1 (attempt|call|terminal) \S+ seq \d+$", head)
        assert json.loads(payload) == {k: v for k, v in wdoc.items() if k != "comment"}
        assert wdoc["comment"]["body_sha256"] == rec.sha256_bytes(c["body"].encode())
    # idempotent: the same push again changes nothing
    n = len(h.api.issue_comments)
    h.merge(p)
    assert len(h.api.issue_comments) == n
    # no second draw after a terminal
    h.at(2026, 11, 10, 7, 30)
    assert h.run("start") == 0 and h.log[-1] == "refused closed"


def test_start_refusals_in_order(tmp_path):
    h = Harness(tmp_path, canary_from="2026-11-10")
    for t in ((2026, 11, 10, 7, 14), (2026, 11, 10, 7, 45, 1), (2026, 11, 7, 7, 20)):
        h.at(*t)
        h.run("start")
        assert h.log[-1] == "refused outside_span", t
    h.at(2026, 11, 9, 7, 20)
    h.run("start")
    assert h.log[-1] == "refused not_expected"                                   # before canary_from
    h.world.owner_commit({"analyst/MODE.json": json.dumps({"mode": "off"})})
    h.at(2026, 11, 10, 7, 20)
    h.run("start")
    assert h.log[-1] == "refused mode_off"
    h.world.owner_commit({"analyst/MODE.json": json.dumps({"mode": "canary", "canary_from": "2026-11-10"})})
    h.at(2026, 12, 28, 7, 20)                                                     # LSE full (GMT: entry 08:00)
    h.run("start")
    assert h.log[-1] == "refused skipped:holiday"
    h.at(2028, 1, 4, 7, 20)
    h.run("start")
    assert h.log[-1] == "refused no_table"
    h.world.owner_commit({"analyst/STATE.json": json.dumps({"calls_enabled": False, "pause_reasons": ["x"]})})
    h.at(2026, 11, 10, 7, 20)
    h.run("start")
    assert h.log[-1] == "refused calls_disabled"
    h.world.owner_commit({"analyst/STATE.json": json.dumps({"calls_enabled": True})})
    h.run("start")
    assert h.log[-1].startswith("attempt_pushed")
    h.push_and_merge()
    h.at(2026, 11, 10, 7, 30)                                                     # within concurrency_min (20)
    h.run("start")
    assert h.log[-1] == "refused concurrent"
    h.at(2026, 11, 10, 7, 41)                                                     # a manual run-now later: draw 2
    h.run("start")
    assert h.log[-1] == "attempt_pushed 2026-11-10_W1 n=2 mode=canary"
    h.push_and_merge()
    h.at(2026, 11, 10, 7, 44)
    h.run("start")
    assert h.log[-1] == "refused attempt_cap"


def test_bst_branch_fire_2027_03_29_and_30(tmp_path):
    """C1's manual test as a unit test: the 06:20 UTC fire is W1's in BST."""
    h = Harness(tmp_path, canary_from="2027-03-29", slots_until=dt.date(2027, 4, 2))
    h.at(2027, 3, 29, 6, 20)
    h.run("start")
    assert h.log[-1] == "refused skipped:holiday"                                 # Easter Monday (LSE), not outside
    h.at(2027, 3, 30, 6, 20)
    h.run("start")
    assert h.log[-1] == "attempt_pushed 2027-03-30_W1 n=1 mode=canary"
    h.at(2027, 3, 30, 7, 20)                                                      # the GMT fire exits
    h.run("start")
    assert h.log[-1] == "refused outside_span"


def test_invalid_plan_three_times_and_decline(tmp_path):
    h = Harness(tmp_path)
    assert h.run("start") == 0
    h.push_and_merge()
    h.at(2026, 11, 10, 7, 21)
    h.run("context")
    bad = dict(PLAN, stop_mult=9)
    assert h.run("lock", str(h.write_plan(bad))) == 2 and h.log[-1] == "invalid stop_mult failures=1"
    assert h.run("lock", str(h.sess_dir / "missing.json")) == 2 and "missing_plan" in h.log[-1]
    assert h.run("lock", str(h.write_plan(bad))) == 0 and h.log[-1] == "locked 2026-11-10_W1 status=no_call:invalid"
    h.push_and_merge()
    t = h.main_json("analyst/canary/terminal/2026-11-10_W1.json")
    assert (t["status"], t["detail"]) == ("no_call:invalid", None)
    assert h.main_json(CANARY_CALL)["lock_validation_failures"] == 3
    assert h.run("lock", str(h.write_plan(PLAN))) == 0 and h.log[-1] == "refused already_locked"
    # decline on the next window
    h.at(2026, 11, 10, 13, 50)                                                    # W2 (EST entry 14:30)
    assert h.run("start") == 0
    h.push_and_merge()
    h.at(2026, 11, 10, 13, 52)
    h.run("context")
    assert h.run("lock", decline=True) == 0
    h.push_and_merge()
    assert h.main_json("analyst/canary/terminal/2026-11-10_W2.json")["status"] == "no_call:refusal"


def test_context_no_call_is_locked_at_once(tmp_path):
    h = Harness(tmp_path, store_last=dt.date(2026, 11, 4))                        # 75 listed hours -> store_stale
    h.run("start")
    h.push_and_merge()
    h.at(2026, 11, 10, 7, 21)
    assert h.run("context") == 0 and h.log[-1] == "locked 2026-11-10_W1 status=no_call:data"
    h.push_and_merge()
    t = h.main_json("analyst/canary/terminal/2026-11-10_W1.json")
    assert (t["status"], t["detail"]) == ("no_call:data", "store_stale")
    call = h.main_json(CANARY_CALL)
    assert call["plan"] is None and call["S"] is None and call["context"]["detail"] == "store_stale"


def test_late_lock_is_no_call_late(tmp_path):
    h = Harness(tmp_path)
    h.run("start")
    h.push_and_merge()
    h.at(2026, 11, 10, 7, 21)
    h.run("context")
    h.at(2026, 11, 10, 7, 44)
    h.run("lock", str(h.write_plan(PLAN)))
    h.push_and_merge(t=dt.datetime(2026, 11, 10, 7, 45, 1, tzinfo=dt.timezone.utc))   # server time after deadline
    t = h.main_json("analyst/canary/terminal/2026-11-10_W1.json")
    assert t["status"] == "no_call:late" and t["call"]["lock_utc"] == "2026-11-10T07:45:01Z"


def test_void_attempt_and_no_attempt_and_underivable(tmp_path):
    h = Harness(tmp_path)
    h.run("start")
    h.push_and_merge(t=dt.datetime(2026, 11, 10, 7, 14, 59, tzinfo=dt.timezone.utc))  # before entry − 45: void
    assert h.last_witness()["window"]["void"] is True
    h.at(2026, 11, 10, 7, 21)
    h.run("context")
    h.at(2026, 11, 10, 7, 25)
    h.run("lock", str(h.write_plan(PLAN)))
    h.push_and_merge()
    t = h.main_json("analyst/canary/terminal/2026-11-10_W1.json")
    assert (t["status"], t["detail"]) == ("no_call:invalid", "void_attempt")
    # W2: a call without any attempt, and then (W1 of 11-11) an underivable call
    good = h.main_json(CANARY_CALL)
    h.at(2026, 11, 10, 14, 0)
    fake = dict(good, call_id="2026-11-10_W2", window="W2")
    fake["context"] = dict(good["context"], call_id="2026-11-10_W2")
    fake["context_sha256"] = rec.compact_sha256(fake["context"])
    h.agent_branch("claude/analyst-canary-call-2026-11-10_W2",
                   {"analyst/canary/calls/2026-11-10_W2.json": rec.record_bytes(fake)})
    h.push_and_merge()
    t = h.main_json("analyst/canary/terminal/2026-11-10_W2.json")
    assert (t["status"], t["detail"]) == ("no_call:invalid", "no_attempt")


def test_underivable_call(tmp_path):
    h = Harness(tmp_path)
    h.run("start")
    h.push_and_merge()
    h.at(2026, 11, 10, 7, 21)
    h.run("context")
    h.at(2026, 11, 10, 7, 25)
    h.run("lock", str(h.write_plan(PLAN)))
    branch = "refs/heads/claude/analyst-canary-call-2026-11-10_W1"
    import subprocess
    sha = subprocess.run(["git", "-C", str(h.world.origin), "rev-parse", branch], capture_output=True,
                         text=True).stdout.strip()
    doc = json.loads(subprocess.run(["git", "-C", str(h.world.origin), "show", f"{sha}:{CANARY_CALL}"],
                                    capture_output=True).stdout)
    doc["S"] = doc["S"] + 1.0                                                     # tampered stop distance
    h.agent_branch("claude/analyst-canary-call-2026-11-10_W1", {CANARY_CALL: rec.record_bytes(doc)})
    h.push_and_merge()
    t = h.main_json("analyst/canary/terminal/2026-11-10_W1.json")
    assert (t["status"], t["detail"]) == ("no_call:invalid", "underivable")


@pytest.mark.parametrize("case,expect", [
    ("two_files", "agent_scope"), ("modify", "agent_scope"), ("delete", "agent_scope"),
    ("workflow", "agent_scope"), ("outside", "agent_scope"), ("other_mode", "mode"),
    ("bad_json", "invalid_content"), ("n2_first", "attempt_n"),
])
def test_allowlist_rejections(tmp_path, case, expect):
    h = Harness(tmp_path)
    att = rec.record_bytes(records.attempt("call", "canary", W1, 1, W1, "x" * 40, records.frozen_block(
        __import__("ga.repo", fromlist=["DirView"]).DirView(h.world.seed)), {"note": "x"}))
    files, delete = {}, ()
    if case == "two_files":
        files = {CANARY_ATT: att, "analyst/canary/attempts/2026-11-10_W1_2.json": att}
    elif case == "modify":
        files = {"analyst/PARAMS.json": b'{"T_run_min": 99}\n'}
    elif case == "delete":
        delete = ("analyst/PARAMS.json",)
    elif case == "workflow":
        files = {".github/workflows/x.yml": b"on: workflow_dispatch\n"}
    elif case == "outside":
        files = {"analyst/canary/notes/x.json": b"{}"}
    elif case == "other_mode":
        files = {"analyst/attempts/2026-11-10_W1_1.json": att}
    elif case == "bad_json":
        files = {CANARY_ATT: b"{not json"}
    elif case == "n2_first":
        doc = json.loads(att)
        doc["n"] = 2
        files = {"analyst/canary/attempts/2026-11-10_W1_2.json": rec.record_bytes(doc)}
    h.agent_branch("claude/analyst-x", files, delete)
    h.push_and_merge()
    w = h.last_witness()
    assert (w["status"], w["reason"]) == ("rejected", expect)
    assert h.world.main_list("analyst/canary/attempts/") == []


def test_duplicate_and_rewrite_and_window_closed(tmp_path):
    h = Harness(tmp_path)
    h.run("start")
    [(ref, sha)] = h.register_pushes()
    h.merge((ref, sha))
    att = h.world.main_file(CANARY_ATT)
    from synth import git
    n = len(h.witnesses())
    git(h.world.origin, "update-ref", "refs/heads/claude/analyst-again", sha)    # the same commit again: a no-op
    h.push_and_merge()
    assert len(h.witnesses()) == n
    base = git(h.world.origin, "rev-parse", f"{sha}^")
    h.agent_branch("claude/analyst-dup", {CANARY_ATT: att}, base=base)          # a new commit, identical bytes
    h.push_and_merge()
    assert (h.last_witness()["status"], h.last_witness()["reason"]) == ("duplicate", "duplicate")
    h.agent_branch("claude/analyst-rw", {CANARY_ATT: att.replace(b'"n": 1', b'"n": 1 ')}, base=base)
    h.push_and_merge()
    assert h.last_witness()["reason"] == "rewrite"
    h.at(2026, 11, 10, 8, 10)                                                     # closer: no_call:run
    h.merge()
    assert h.main_json("analyst/canary/terminal/2026-11-10_W1.json")["status"] == "no_call:run"
    doc = json.loads(att)
    doc["n"] = 2
    h.agent_branch("claude/analyst-late", {"analyst/canary/attempts/2026-11-10_W1_2.json": rec.record_bytes(doc)})
    h.push_and_merge()
    assert h.last_witness()["reason"] == "window_closed"


def test_closer_completeness_and_chain(tmp_path):
    h = Harness(tmp_path, canary_from="2026-11-09")
    h.at(2026, 11, 9, 8, 4)                                                       # W1 deadline 07:45 + 19 min
    h.merge()
    assert h.world.main_list("analyst/canary/terminal/") == []
    h.at(2026, 11, 9, 8, 5)                                                       # + 20 min: closed
    h.merge()
    t1 = h.main_json("analyst/canary/terminal/2026-11-09_W1.json")
    assert (t1["status"], t1["writer"], t1["detail"], t1["prev_chain_sha256"]) == \
        ("missed:trigger", "closer", None, "0" * 64)
    h.at(2026, 11, 10, 7, 20)
    h.run("start")
    h.push_and_merge()
    h.at(2026, 11, 10, 8, 6)                                                      # attempt without lock
    h.merge()
    t_w2 = h.world.main_file("analyst/canary/terminal/2026-11-09_W2.json")
    t_w1b = h.main_json("analyst/canary/terminal/2026-11-10_W1.json")
    assert t_w1b["status"] == "no_call:run"
    assert t_w1b["prev_chain_sha256"] == rec.sha256_bytes(t_w2)
    assert json.loads(t_w2)["prev_chain_sha256"] == rec.sha256_bytes(
        h.world.main_file("analyst/canary/terminal/2026-11-09_W1.json"))
    # every expected window has exactly one terminal witness
    tw = [w["id"] for w in h.witnesses() if w["kind"] == "terminal"]
    assert sorted(tw) == ["2026-11-09_W1", "2026-11-09_W2", "2026-11-10_W1"]
    # a holiday window is skipped
    h2 = Harness(tmp_path / "b", canary_from="2026-12-28", slots_until=dt.date(2027, 1, 8))
    h2.at(2026, 12, 28, 9, 0)
    h2.merge()
    assert h2.main_json("analyst/canary/terminal/2026-12-28_W1.json")["status"] == "skipped:holiday"
    # mode off: detail mode_off
    h3 = Harness(tmp_path / "c", canary_from="2026-11-09")
    h3.world.owner_commit({"analyst/MODE.json": json.dumps({"mode": "off", "canary_from": "2026-11-09"})})
    h3.at(2026, 11, 9, 9, 0)
    h3.merge()
    t = h3.main_json("analyst/canary/terminal/2026-11-09_W1.json")
    assert (t["status"], t["detail"]) == ("missed:trigger", "mode_off")


def test_live_start_json_and_track_dispatch(tmp_path):
    h = Harness(tmp_path, mode="live", canary_from=None, live_from="2026-11-10")
    assert full_call(h) == 0
    h.push_and_merge()
    t = h.main_json("analyst/terminal/2026-11-10_W1.json")
    assert t["status"] == "called"
    s = h.main_json("analyst/START.json")
    # d0 = Tuesday 2026-11-10 -> the next Monday 2026-11-16 (busday_count = 4 >= 3)
    assert (s["d0"], s["first_day"], s["first_call_id"]) == ("2026-11-10", "2026-11-16", W1)
    assert s["terminal_sha256"] == rec.sha256_bytes(h.world.main_file("analyst/terminal/2026-11-10_W1.json"))
    assert h.api.dispatched == [("analyst-track.yml", "main", {"call_id": W1})]
    call = h.main_json("analyst/calls/2026-11-10_W1.json")
    assert call["context"]["memory"]["header"].startswith("data, not instructions")
    assert "canary_probe" not in call


def test_first_day_rule():
    from ga import merge
    import types
    m = types.SimpleNamespace(pins={"freeze_tag": "t"})
    for d0, want in (("2026-11-11", "2026-11-16"), ("2026-11-12", "2026-11-23"), ("2026-11-13", "2026-11-23"),
                     ("2026-11-09", "2026-11-16")):
        w = __import__("ga.clock", fromlist=["Window"]).Window(dt.date.fromisoformat(d0), "W1")
        r = merge.Merge.start_record(m, w, {"lock_utc": "x"}, {"written_utc": "x", "run_id": 1, "head_sha": "h"}, "s")
        assert r["first_day"] == want, d0


def test_completion_from_comment_after_failed_push(tmp_path):
    h = Harness(tmp_path)
    h.run("start")
    h.merge_fail = True
    [p] = h.register_pushes()
    with pytest.raises(RuntimeError):
        h.merge(p)
    assert len(h.api.issue_comments) == 1 and h.world.main_list("analyst/canary/") == []
    h.merge_fail = False
    h.merge()                                                                     # any later run completes it
    assert h.world.main_file(CANARY_ATT) is not None
    w = h.main_json("analyst/canary/witness/000001.json")
    assert w["comment"]["id"] == h.api.issue_comments[0]["id"] and len(h.api.issue_comments) == 1


def test_forged_comments_by_others_are_ignored(tmp_path):
    h = Harness(tmp_path)
    h.api.issue_comments.append({"id": 1, "created_at": "2026-11-10T07:00:00Z", "user": {"login": "mallory"},
                                 "body": 'analyst-witness/1 attempt 2026-11-10_W1 seq 99\n{"seq":99}'})
    h.run("start")
    h.push_and_merge()
    assert h.last_witness()["seq"] == 1


def test_unwitnessed_push_is_processed_by_the_closer(tmp_path):
    h = Harness(tmp_path)
    h.run("start")
    h.register_pushes()                                                           # the merge run never happened
    h.at(2026, 11, 10, 7, 30)
    h.merge()                                                                     # a later run (closer step 1)
    assert h.world.main_file(CANARY_ATT) is not None
    assert h.last_witness()["window"]["time_utc"] == "2026-11-10T07:20:00Z"


def test_canary_lesson_and_revision(tmp_path):
    from ga import paths as P
    fx = (h_root := __import__("pathlib").Path(__file__).resolve().parents[1]) / P.CANARY_SETTLED_FIXTURE
    h = Harness(tmp_path, canary_from="2026-11-09", extra={P.CANARY_SETTLED_FIXTURE: fx.read_bytes()})
    h.at(2026, 11, 10, 16, 40)
    assert h.run("lesson_start") == 0 and h.log[-1].startswith("lesson_attempt_pushed 2026-10-16_W1 n=1")
    h.push_and_merge()
    assert h.last_witness()["kind"] == "attempt" and h.last_witness()["status"] == "accepted"
    lp = h.sess_dir / ".analyst_run" / "lesson.json"
    lp.write_text(json.dumps({"text_ar": "درس قصير من سجل اصطناعي"}, ensure_ascii=False))
    h.at(2026, 11, 10, 16, 42)
    assert h.run("lesson_lock", str(lp)) == 0
    h.push_and_merge()
    les = h.main_json("analyst/canary/lessons/2026-10-16_W1.json")
    assert les["status"] == "ok" and les["attempt_n"] == 1
    assert h.run("lesson_start") == 0 and h.log[-1] == "none"
    # Saturday revision: the canary week of 2026-11-09 is complete once the closer has closed every window
    h.at(2026, 11, 14, 18, 0)
    h.merge()
    assert h.run("revision_start") == 0 and h.log[-1].startswith("revision_attempt_pushed v1 n=1")
    h.push_and_merge()
    pb = h.sess_dir / ".analyst_run" / "playbook.md"
    pb.write_text("# playbook v2\nwrong first line\n")
    h.at(2026, 11, 14, 18, 5)
    assert h.run("revision_lock", str(pb)) == 2 and "first_line" in h.log[-1]
    pb.write_text("# playbook v1\nفرضية من عينة صغيرة.\n")
    assert h.run("revision_lock", str(pb)) == 0
    h.push_and_merge()
    assert h.world.main_file("analyst/canary/playbooks/v1.md") == pb.read_bytes()
    r = h.main_json("analyst/canary/revisions/2026-W46.json")
    assert (r["status"], r["version"], r["effective_from"]) == ("written", 1, "2026-11-16")
    # the closer records a skipped week when no revision was written
    h.at(2026, 11, 22, 0, 1)
    h.merge()
    assert h.main_json("analyst/canary/revisions/2026-W47.json")["status"] == "skipped"
