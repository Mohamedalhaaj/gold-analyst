"""§16.18 A14 on the local inbox world: `start`'s window selection, gates and wake record (R-AN-90), the trigger
session's XAUUSD_M30 request, `analyst-context/4` (R-AN-23) with D_ref from mirror/system/forecasts.csv (R-AN-21),
and analyst-merge's ids, `window_order`, wake check and the closer's `skipped:trade_open` (R-AN-11, R-AN-33,
R-AN-39). Canary mode; a14_from = canary_from = 2026-11-10 (GMT/EST: W1 07:30, W1b 09:30, W2 14:00, W2b 16:00
checks)."""
import datetime as dt
import json

from ga import clock, context, rec, records, td
from harness import PLAN, Harness
from synth import td_error

UTC = rec.UTC
P = "analyst/canary/"
T1100 = "2026-11-10_T1100"


def u(*a):
    return dt.datetime(*a, tzinfo=UTC)


def m30(special=None):
    """Flat XAU/USD 30-minute bars; each model day's first bar is wide (R20 = 20, y_h = asia_h = 4010)."""
    special = special or {}

    def f(t):
        if t in special:
            return special[t]
        m = clock.to_server(t)
        return (4000.0, 4010.0, 3990.0, 4000.0) if (m.hour, m.minute) == (1, 0) else (4000.0, 4001.0, 3999.0, 4000.0)
    return f


UP = m30({u(2026, 11, 10, 10, 30): (4000.0, 4012.0, 3999.0, 4011.0)})       # asia_up at the 11:00 check


def state(h):
    return json.loads((h.sess_dir / ".analyst_run" / "state.json").read_text())


def trigger_call(h, plan_obj=PLAN):
    h.at(2026, 11, 10, 11, 1, 10)
    assert h.run("start") == 0 and h.log[-1] == f"attempt_pushed {T1100} n=1 mode=canary"
    h.push_and_merge()
    h.at(2026, 11, 10, 11, 3)
    assert h.run("context") == 0, h.log[-3:]
    h.at(2026, 11, 10, 11, 10)
    assert h.run("lock", str(h.write_plan(plan_obj))) == 0
    h.push_and_merge()


def test_a_trigger_session_end_to_end(tmp_path):
    h = Harness(tmp_path, a14_from="2026-11-10", m30=UP)
    h.at(2026, 11, 10, 11, 1, 10)
    assert h.run("start") == 0 and h.log[-1] == f"attempt_pushed {T1100} n=1 mode=canary"
    m30_req = [x for x in h.feed.requests if "interval=30min" in x]
    assert m30_req == [td.m30_url(u(2026, 10, 8, 22), u(2026, 11, 10, 11))]          # A = 22:00 of d − 33 days
    assert h.clk.t >= 60.0                                                     # waited until T + 130 s
    wk = state(h)["wake"]
    assert (wk["kind"], wk["reason"], wk["check_utc"]) == ("trigger", "asia_up", "2026-11-10T11:00:00Z")
    assert wk["td"]["req"] == "XAUUSD_M30" and wk["td"]["status"] == "ok"
    assert wk["td"]["bars"][-1]["t_utc"] == "2026-11-10T10:30:00Z"                # t_utc <= T − 30 min only
    h.push_and_merge()
    att = h.main_json(f"{P}attempts/{T1100}_1.json")
    assert att["schema"] == "analyst-attempt/2" and records.valid_attempt(att)
    w = [x for x in h.witnesses() if x["kind"] == "attempt"][-1]
    assert (w["kind"], w["status"], w["window"]["void"]) == ("attempt", "accepted", False)
    assert w["window"]["entry_utc"] == "2026-11-10T11:30:00Z" and w["window"]["deadline_utc"] == \
        "2026-11-10T11:15:00Z"
    h.at(2026, 11, 10, 11, 3)
    assert h.run("context") == 0 and h.log[-1].startswith(f"context_ok {T1100}")
    ctx = json.loads((h.sess_dir / ".analyst_run" / "context.json").read_text())
    assert ctx["schema"] == "analyst-context/5" and ctx["window"] == "T" and ctx["status"] == "ok"   # A15: /5
    assert ctx["wake"]["reason"] == "asia_up" and ctx["wake"]["values"]["c_t"] == 4011.0
    assert ctx["wake"]["values"]["r20"] == 20.0 and len(ctx["wake"]["r20_days"]) == 20
    assert ctx["sources"]["note"].endswith(context.WAKE_NOTE + context.REVIEW_NOTE)
    assert ctx["d_ref"]["slot"] == 12                                           # entry 11:30 UTC = model 13:30
    assert ctx["store"]["slots_sha256"] == rec.sha256_bytes(h.world.main_file(context.FORECASTS_PATH))


def test_selection_and_refusals(tmp_path):
    h = Harness(tmp_path, a14_from="2026-11-10", m30=m30())
    h.at(2026, 11, 10, 11, 1, 10)
    assert h.run("start") == 0 and h.log[-1] == "refused not_triggered"
    h.feed.responses = {"XAU/USD": [(429, td_error(429))]}
    assert h.run("start") == 0 and h.log[-1] == "refused wake_data td_credits"
    h.feed.responses = {}
    h.at(2026, 11, 10, 11, 16)                                                 # after T + 15 min − T_run
    assert h.run("start") == 0 and h.log[-1] == "refused outside_span"
    n_req = len(h.feed.requests)
    h.at(2026, 11, 10, 9, 35)                                                  # W1b's start interval
    assert h.run("start") == 0 and h.log[-1] == "attempt_pushed 2026-11-10_W1b n=1 mode=canary"
    assert state(h)["wake"] == {"kind": "second_look", "reason": None, "check_utc": "2026-11-10T09:30:00Z",
                                "td": None}
    assert len(h.feed.requests) == n_req                                       # no request for a second look
    h.at(2026, 11, 26, 11, 1, 10)                                              # NYSE holiday: no trigger
    assert h.run("start") == 0 and h.log[-1] == "refused skipped:holiday"
    h2 = Harness(tmp_path / "pre", a14_from=None, m30=UP)                       # a14_from null: W1 and W2 only
    h2.at(2026, 11, 10, 9, 35)
    assert h2.run("start") == 0 and h2.log[-1] == "refused outside_span"
    h2.at(2026, 11, 10, 7, 20)
    assert h2.run("start") == 0 and state(h2)["wake"]["kind"] == "fixed"


def test_trade_open_gate_and_the_closers_skip(tmp_path):
    h = Harness(tmp_path, a14_from="2026-11-10", m30=UP)
    trigger_call(h)
    assert h.main_json(f"{P}terminal/{T1100}.json")["status"] == "called"
    h.at(2026, 11, 10, 14, 1)                                                  # W2: the trigger's trade is open
    assert h.run("start") == 0 and h.log[-1] == "refused trade_open"
    h.at(2026, 11, 10, 14, 40)
    h.merge()
    t = h.main_json(f"{P}terminal/2026-11-10_W2.json")
    assert (t["status"], t["detail"], t["writer"]) == ("skipped:trade_open", T1100, "closer")
    assert h.main_json(f"{P}terminal/2026-11-10_W1b.json")["status"] == "missed:trigger"
    # the chain runs in window order: W1, W1b, T1100, W2
    tb = h.world.main_file(f"{P}terminal/{T1100}.json")
    assert t["prev_chain_sha256"] == rec.sha256_bytes(tb)
    w1b = h.world.main_file(f"{P}terminal/2026-11-10_W1b.json")
    assert json.loads(tb)["prev_chain_sha256"] == rec.sha256_bytes(w1b)


def _attempt_bytes(cid, wake, schema="analyst-attempt/2"):
    doc = {"schema": schema, "kind": "call", "mode": "canary", "draw_id": cid, "n": 1, "call_id": cid, "wake": wake,
           "repo_head_sha": "a" * 40, "manifest_sha256": "b" * 64, "code_sha8": "bbbbbbbb",
           "routine_prompt_sha256": "c" * 64, "self_reported": {}}
    if schema != "analyst-attempt/2":
        del doc["wake"]
    return rec.record_bytes(doc)


def _prov():
    return {"src": "td", "req": "XAUUSD_M30", "url": "u", "http": 200, "td_code": 200, "status": "ok",
            "reason": None, "bytes": 1, "sha256": "x", "values_n": 0, "bars": [], "fetched_utc": None, "tries": 1}


def _push(h, cid, data):
    h.agent_branch(f"claude/analyst-canary-attempt-{cid}-1", {f"{P}attempts/{cid}_1.json": data})
    h.push_and_merge()
    return [x for x in h.witnesses() if x["kind"] != "terminal"][-1]


def test_merge_ids_wake_check_and_window_order(tmp_path):
    h = Harness(tmp_path, a14_from="2026-11-10")
    h.at(2026, 11, 10, 9, 0, 30)
    w = _push(h, "2026-11-10_T0900", _attempt_bytes("2026-11-10_T0900", records.wake_of(
        clock.window_of("2026-11-10_T0900"), "move", _prov())))
    assert (w["status"], w["window"]["void"]) == ("accepted", False)
    bad_reason = records.wake_of(clock.window_of("2026-11-10_T1000"), "calm", _prov())
    h.at(2026, 11, 10, 10, 0, 30)
    assert _push(h, "2026-11-10_T1000", _attempt_bytes("2026-11-10_T1000", bad_reason))["reason"] == \
        "invalid_content"
    sched = records.wake_of(clock.Window(dt.date(2026, 11, 10), "W1b"))
    assert _push(h, "2026-11-10_T0930", _attempt_bytes("2026-11-10_T0930", dict(
        sched, kind="trigger", reason="move", td=_prov())))["reason"] == "invalid_content"   # a scheduled check
    assert _push(h, "2026-11-10_W1b", _attempt_bytes("2026-11-10_W1b", None, "analyst-attempt/1"))["reason"] == \
        "invalid_content"                                                      # /1 only for W1 and W2
    # a trigger attempt listed after a later window of its mode was closed: window_order (W1b closed at 10:06)
    h.at(2026, 11, 10, 10, 6)
    h.merge()
    assert h.main_json(f"{P}terminal/2026-11-10_W1b.json")["status"] == "missed:trigger"
    late = records.wake_of(clock.window_of("2026-11-10_T0800"), "y_high", _prov())
    assert _push(h, "2026-11-10_T0800", _attempt_bytes("2026-11-10_T0800", late))["reason"] == "window_order"
    # the accepted trigger is expected: closed no_call:run after its deadline + 20 min, in window order
    assert h.main_json(f"{P}terminal/2026-11-10_T0900.json")["status"] == "no_call:run"
    h2 = Harness(tmp_path / "pre", a14_from="2026-11-11")                      # before a14_from
    h2.at(2026, 11, 10, 9, 31)
    assert _push(h2, "2026-11-10_W1b", _attempt_bytes("2026-11-10_W1b", records.wake_of(
        clock.Window(dt.date(2026, 11, 10), "W1b"))))["reason"] == "invalid_content"
