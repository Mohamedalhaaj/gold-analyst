"""analyst-settle end to end (closer, settlement, twins, STATE, ledger) and the state rules (prereg §6; register
R-AN-40, R-AN-46, R-AN-47, R-AN-51..R-AN-58)."""
import csv
import datetime as dt
import io
import json

import pytest

from ga import clock, merge, paths, rec, settle_run, state, store
from ga.repo import DictView
from harness import PLAN, Harness
from synth import store_files

UTC = rec.UTC


def test_settle_end_to_end_live(tmp_path):
    h = Harness(tmp_path, mode="live", canary_from=None, live_from="2026-11-10")
    h.run("start")
    h.push_and_merge()
    h.at(2026, 11, 10, 7, 21)
    h.run("context")
    h.at(2026, 11, 10, 7, 25)
    h.run("lock", str(h.write_plan(PLAN)))
    h.push_and_merge()
    # the export mirrors 11-10 (and 11-11 so W2's day is final); the settle run follows the push
    h.at(2026, 11, 11, 9, 0)
    mirror = store_files(clock.days_range(dt.date(2026, 9, 1), dt.date(2026, 11, 10)))
    h.world.owner_commit(mirror, "analyst mirror")
    ws = merge.Workspace(h.world.clone("settle"))
    st, done = settle_run.run_settle(ws, h.api, h.now, {"workflow": "analyst-settle", "id": 9, "attempt": 1,
                                                        "event": "push"}, verdict=None)
    s = h.main_json("analyst/settled/2026-11-10_W1.json")
    assert s["class"] == "shakedown" and s["action"] == "buy" and s["side"] == 1
    assert s["outcome"]["status"] == "final" and s["opposite"]["status"] == "final"
    assert s["call"]["reason_ar"] == PLAN["reason_ar"]
    assert s["terminal_sha256"] == rec.sha256_bytes(h.world.main_file("analyst/terminal/2026-11-10_W1.json"))
    assert len(s["path_summary"]["buckets"]) == 16
    assert set(s) >= {"schema", "call_id", "mode", "window", "model_day", "class", "action", "side", "stop_mult",
                      "target_mult", "D_ref", "S", "T", "S_dimes", "T_dimes", "entry_utc", "exit_utc", "outcome",
                      "opposite", "path_summary", "call", "context", "inputs", "call_sha256", "terminal_sha256",
                      "context_sha256", "settled_utc", "run_id", "head_sha"}
    tw = h.main_json("analyst/twins/2026-11-10_W1.json")
    assert tw["status"] == "final" and tw["terminal_status"] == "called"
    tw2 = h.main_json("analyst/twins/2026-11-10_W2.json")                     # missed window: twin too
    assert tw2["terminal_status"] == "missed:trigger" and tw2["status"] == "final"
    stj = h.main_json("analyst/STATE.json")
    assert stj["state"] == "SHAKEDOWN" and stj["n"] == 0 and stj["calls_enabled"] is True
    assert stj["last_processed_day"] == "2026-11-10"
    led = list(csv.DictReader(io.StringIO(h.world.main_file("analyst/ledger.csv").decode())))
    assert [r["call_id"] for r in led] == ["2026-11-10_W1", "2026-11-10_W2", "2026-11-11_W1"]
    assert led[2]["status"] == "missed:trigger" and led[2]["twin_status"] == ""   # 11-11 not in the store yet
    assert led[0]["class"] == "shakedown" and led[0]["trade_no"] == "" and led[0]["R"] != ""
    assert list(led[0]) == state.LEDGER_COLS
    # a second run is a no-op for settled records (create-only) and keeps the state
    ws = merge.Workspace(h.world.clone("settle2"))
    settle_run.run_settle(ws, h.api, h.now, {"workflow": "analyst-settle", "id": 10, "attempt": 1, "event": "x"})
    assert h.main_json("analyst/settled/2026-11-10_W1.json") == s


def test_settle_waits_for_the_store(tmp_path):
    h = Harness(tmp_path, mode="live", canary_from=None, live_from="2026-11-10")
    h.at(2026, 11, 10, 18, 0)
    ws = merge.Workspace(h.world.clone("settle"))
    st, done = settle_run.run_settle(ws, h.api, h.now, {"workflow": "s", "id": 1, "attempt": 1, "event": "x"})
    assert h.world.main_list("analyst/settled/") == [] and h.world.main_list("analyst/twins/") == []
    assert len(h.world.main_list("analyst/terminal/")) == 2                     # the closer still closed both


# ---------------------------------------------------------------- state rules on synthetic records
def day_list(start, n):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def records_view(ys, first_day=dt.date(2026, 11, 16), start=dt.date(2026, 11, 16), side=1, extra=None,
                 d_w=0.0):
    files = {}
    days = day_list(start, len(ys))
    for d, y in zip(days, ys):
        cid = f"{d.isoformat()}_W1"
        S = 20.0
        R = y + 0.25 / S                    # R_stress = y (unclipped range)
        net = R * S
        files[f"analyst/terminal/{cid}.json"] = json.dumps({"call_id": cid, "status": "called", "entry_utc": "x",
                                                             "call": {"sha256": "c", "lock_utc": "t"},
                                                             "attempts": [{}]})
        files[f"analyst/calls/{cid}.json"] = json.dumps({"plan": {"action": "buy" if side > 0 else "sell",
                                                                  "confidence": "low"}, "side": side, "S": S,
                                                         "T": 40.0, "d_ref": {"D_ref": 20.0},
                                                         "self_reported": {"session_id": f"s-{cid}"}})
        files[f"analyst/settled/{cid}.json"] = json.dumps({
            "side": side, "outcome": {"y": max(-2, min(2, y)), "R": R, "R_stress": y, "net_usd": net,
                                      "stress_net_usd": net - 0.25, "cost_usd": 0.3}})
        files[f"analyst/twins/{cid}.json"] = json.dumps({"status": "final", "d_W": d_w})
    files[paths.START_PATH] = json.dumps({"first_day": first_day.isoformat()})
    files.update(extra or {})
    return DictView(files), days


def evaluate(view, days, verdict, now=None):
    out = []

    def write(p, b):
        view.files[p] = b
        out.append(json.loads(b))

    now = now or dt.datetime.combine(days[-1] + dt.timedelta(days=1), dt.time(12), tzinfo=UTC)
    st = state.run_state(view, days, now, 1, "h", verdict, write)
    return st, out


SOUND = {"analyst_verdict": "SOUND", "asof_utc": "x", "violation_keys": []}


def test_promotion_needs_all_of_p1_to_p7():
    ys = [1.0] * 70                                                          # a huge edge, 70 trades over 14 weeks
    view, days = records_view(ys)
    st, evs = evaluate(view, days, SOUND)
    names = [e["event"] for e in evs]
    assert names[0] == "TESTING" and "PROMOTED" in names
    p = [e for e in evs if e["event"] == "PROMOTED"][0]
    assert p["n"] >= 60 and p["E"] >= 400.0
    first, last = days[0], dt.date.fromisoformat(p["model_day"])
    assert (last - first).days >= 56
    view, days = records_view(ys)
    st, evs = evaluate(view, days, None)                                     # P7: no verdict -> never promoted
    assert "PROMOTED" not in [e["event"] for e in evs] and st["state"] == "TESTING"
    view, days = records_view(ys, d_w=50.0)                                  # P4: all of it was drift
    assert "PROMOTED" not in [e["event"] for e in evaluate(view, days, SOUND)[1]]


def test_demotion_and_retirement():
    ys = [1.0] * 60 + [-1.0] * 40
    view, days = records_view(ys)
    st, evs = evaluate(view, days, SOUND)
    names = [e["event"] for e in evs]
    assert names[:2] == ["TESTING", "PROMOTED"] and "DEMOTED" in names and names[-1] == "RETIRED"
    assert [e for e in evs if e["event"] == "RETIRED"][0]["reason"] == "demoted"
    assert st["state"] == "RETIRED" and st["calls_enabled"] is False
    # age: 12 months after first_day without promotion
    view, days = records_view([0.0] * 300, start=dt.date(2026, 11, 16))
    st, evs = evaluate(view, days, SOUND)
    r = [e for e in evs if e["event"] == "RETIRED"]
    assert r and r[0]["reason"] in ("age", "n150")


def test_shakedown_trades_are_not_counted():
    view, days = records_view([1.0] * 10, first_day=dt.date(2026, 11, 23), start=dt.date(2026, 11, 16))
    st, evs = evaluate(view, days, SOUND)
    assert st["n"] == 5 and evs[0]["event"] == "TESTING" and evs[0]["model_day"] == "2026-11-23"


def test_model_overlay_pauses_and_clear_resumes():
    runs = {"analyst/runs/2026-W47.json": json.dumps({"runs": [
        {"routine": "analyst-w1", "run_id": "r1", "session_id": "s-2026-11-16_W1", "created_utc": "x",
         "model": "claude-sonnet-x"}]})}
    view, days = records_view([0.5] * 5, extra=runs)
    st, evs = evaluate(view, days, SOUND)
    assert any(e["event"] == "PAUSED" and e["reason"] == "model:r1" for e in evs)
    assert st["calls_enabled"] is False and st["n"] == 4                     # the overlaid trade left the set
    view.files["analyst/ADJUDICATIONS.csv"] = b"utc,ref,subject,decision,note\n2026-11-20T00:00:00Z,pause,model:r1,clear,ok\n"
    st, evs = evaluate(view, days, SOUND)
    assert any(e["event"] == "RESUMED" for e in evs) and st["calls_enabled"] is True


def test_judge_violation_pauses_then_retires_on_a_new_key_after_a_clear():
    view, days = records_view([0.1] * 5)
    v1 = {"analyst_verdict": "VIOLATION", "violation_keys": ["A1|x|p|c1"]}
    st, evs = evaluate(view, days, v1)
    assert st["calls_enabled"] is False and "judge_violation:A1|x|p|c1" in st["pause_reasons"]
    view.files["analyst/ADJUDICATIONS.csv"] = (b"utc,ref,subject,decision,note\n"
                                               b"2026-11-20T00:00:00Z,pause,judge_violation:A1|x|p|c1,clear,ok\n")
    st, evs = evaluate(view, days, v1)
    assert st["calls_enabled"] is True
    st, evs = evaluate(view, days, {"analyst_verdict": "VIOLATION", "violation_keys": ["A1|x|p|c2"]})
    assert st["state"] == "RETIRED" and any(e["reason"] == "violation" for e in evs)


def test_paused_display_share_and_side_label():
    view, days = records_view([0.1] * 10, side=-1)
    for d in day_list(days[-1] + dt.timedelta(days=1), 3):
        cid = f"{d.isoformat()}_W1"
        view.files[f"analyst/terminal/{cid}.json"] = json.dumps({"call_id": cid, "status": "missed:trigger",
                                                                 "entry_utc": "x", "call": None,
                                                                 "attempts": []}).encode()
    st, _ = evaluate(view, days, SOUND, now=dt.datetime(2027, 3, 1, tzinfo=UTC))
    assert st["missed_share_40"] == pytest.approx(3 / 13) and st["paused_display"] is True
    assert st["side_share"] == 1.0 and st["side_label"] is True
