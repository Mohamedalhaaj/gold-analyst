"""§16.18 A14, continuous watching: the new windows' ids, times and order (R-AN-4, R-AN-8, R-AN-89), the check grid
G(d), and the trigger rule R(T) as one pure function (ga/wake.py), on synthetic 30-minute bars."""
import datetime as dt

import pytest

from ga import clock, records, rec, wake

UTC = rec.UTC


def u(*a):
    return dt.datetime(*a, tzinfo=UTC)


# ---------------------------------------------------------------- windows (R-AN-4, R-AN-8 table)
@pytest.mark.parametrize("day,w1b,w2b,last,slots", [
    (dt.date(2026, 10, 22), (9, 0), (15, 30), (16, 30), (11, 17)),     # BST, EDT
    (dt.date(2026, 10, 28), (10, 0), (15, 30), (16, 30), (12, 17)),    # GMT, EDT (mismatch week)
    (dt.date(2026, 11, 10), (10, 0), (16, 30), (17, 30), (11, 17)),    # GMT, EST
    (dt.date(2027, 3, 17), (10, 0), (15, 30), (16, 30), (12, 17)),     # GMT, EDT
    (dt.date(2027, 4, 7), (9, 0), (15, 30), (16, 30), (11, 17)),       # BST, EDT
])
def test_second_looks_and_last_check_follow_the_dst_table(day, w1b, w2b, last, slots):
    a, b = clock.Window(day, "W1b"), clock.Window(day, "W2b")
    assert a.entry_utc == u(day.year, day.month, day.day, *w1b) == clock.Window(day, "W1").entry_utc + \
        dt.timedelta(minutes=120)
    assert b.entry_utc == u(day.year, day.month, day.day, *w2b)
    for w in (a, b):
        assert w.exit_utc - w.entry_utc == dt.timedelta(minutes=240)
        assert w.deadline_utc == w.entry_utc - dt.timedelta(minutes=15)
        assert w.span_start_utc == w.check_utc == w.entry_utc - dt.timedelta(minutes=30)
        assert w.start_interval(5) == (w.check_utc, w.check_utc + dt.timedelta(minutes=10))
    assert (a.slot, b.slot) == slots and (a.kind, b.kind, a.exchange, b.exchange) == \
        ("second_look", "second_look", "LSE", "NYSE")
    assert clock.last_check(day) == u(day.year, day.month, day.day, *last)
    w1 = clock.Window(day, "W1")
    assert w1.span_start_utc == w1.entry_utc - dt.timedelta(minutes=45) and w1.kind == "fixed"   # unchanged


def test_check_grid_and_trigger_ids():
    d = dt.date(2026, 11, 10)                          # EST: 39 checks, 22:30 (d - 1) .. 17:30
    g = clock.check_grid(d)
    assert len(g) == 39 and g[0] == u(2026, 11, 9, 22, 30) and g[-1] == u(2026, 11, 10, 17, 30)
    assert len(clock.check_grid(dt.date(2026, 10, 22))) == 37 and clock.check_grid(dt.date(2026, 11, 14)) == []
    assert set(clock.scheduled_checks(d)) == {u(2026, 11, 10, 7, 30), u(2026, 11, 10, 9, 30),
                                              u(2026, 11, 10, 14, 0), u(2026, 11, 10, 16, 0)}
    t = clock.window_of("2026-11-10_T2230")             # a 22:30 check belongs to the next date's model day
    assert t.check_utc == u(2026, 11, 9, 22, 30) and t.entry_utc == u(2026, 11, 9, 23, 0)
    assert (t.name, t.kind, t.exchange, t.slot) == ("T", "trigger", None, 0)
    assert clock.tday(u(2026, 11, 9, 22, 30)) == d and clock.tday(u(2026, 11, 9, 21, 59)) == dt.date(2026, 11, 9)
    assert clock.valid_trigger(d, "T1100") and not clock.valid_trigger(d, "T0930")   # W1b's scheduled check
    assert not clock.valid_trigger(d, "T1800") and not clock.valid_trigger(d, "T2200")
    assert clock.trigger_tag(u(2026, 11, 10, 11, 0)) == "T1100" and clock.window_type("2026-11-10_T1100") == "T"
    with pytest.raises(ValueError):
        clock.parse_call_id("2026-11-10_T1115")
    # window order is (model_day, entry_utc): the 22:30 trigger comes first, W1b between W1 and W2
    ids = ["2026-11-10_W2b", "2026-11-10_W2", "2026-11-10_W1b", "2026-11-10_T1100", "2026-11-10_W1",
           "2026-11-10_T2230", "2026-11-09_W2"]
    assert sorted(ids, key=clock.window_key) == ["2026-11-09_W2", "2026-11-10_T2230", "2026-11-10_W1",
                                                 "2026-11-10_W1b", "2026-11-10_T1100", "2026-11-10_W2",
                                                 "2026-11-10_W2b"]
    assert [w.tag for w in clock.windows_on(d, a14=True)] == ["W1", "W1b", "W2", "W2b"]
    assert [w.tag for w in clock.windows_on(d)] == ["W1", "W2"]


# ---------------------------------------------------------------- the rule (R-AN-89)
D = dt.date(2026, 11, 10)


def bars(special=None, days=22, wide=10.0):
    """Flat 30-minute bars (o = c = 4000, h = 4001, l = 3999) on the model days before D and on D up to 17:00; the
    first bar of each day is wide (h = 4000 + wide, l = 4000 − wide), so R20 = 2·wide, y_h and asia_h = 4000 + wide."""
    special = special or {}
    out = []
    t = u(2026, 10, 1, 22, 0)
    end = u(2026, 11, 10, 17, 0)
    seen = set()
    while t <= end:
        m = clock.to_server(t)
        if m.weekday() < 5 and m.hour >= 1:
            day = clock.tday(t)
            if t in special:
                out.append((t,) + special[t])
            elif day not in seen:
                out.append((t, 4000.0, 4000.0 + wide, 4000.0 - wide, 4000.0))
            else:
                out.append((t, 4000.0, 4001.0, 3999.0, 4000.0))
            seen.add(day)
        t += dt.timedelta(minutes=30)
    return out


def at(T, b, wakes=(), trades=(), holiday=False):
    return wake.rule(D, T, b, list(wakes), list(trades), holiday)


def test_values_and_the_five_conditions():
    T = u(2026, 11, 10, 11, 0)
    v = wake.values(D, T, bars())
    assert v["r20"] == 20.0 and v["y_day"] == dt.date(2026, 11, 9) and (v["y_h"], v["y_l"]) == (4010.0, 3990.0)
    assert (v["asia_h"], v["asia_l"]) == (4010.0, 3990.0) and v["c_t"] == 4000.0
    assert v["c_prev_utc"] == u(2026, 11, 10, 9, 30) and len(v["r20_days"]) == 20
    assert at(T, bars())["result"] == "none"
    up = {u(2026, 11, 10, 10, 30): (4000.0, 4012.0, 3999.0, 4011.0)}
    r = at(T, bars(up))
    assert (r["result"], r["tag"], r["kind"], r["reason"]) == ("window", "T1100", "trigger", "asia_up")
    # before 07:00 UTC the Asia conditions are off; y_high takes over (h >= y_h − 0.1·R20, inclusive)
    early = {u(2026, 11, 10, 5, 30): (4000.0, 4008.0, 3999.0, 4000.0)}
    assert at(u(2026, 11, 10, 6, 0), bars(early))["reason"] == "y_high"
    just = {u(2026, 11, 10, 5, 30): (4000.0, 4007.999, 3999.0, 4000.0)}
    assert at(u(2026, 11, 10, 6, 0), bars(just))["result"] == "none"
    low = {u(2026, 11, 10, 10, 30): (4000.0, 4001.0, 3988.0, 3989.0)}
    assert at(T, bars(low))["reason"] == "asia_dn"
    mv = {u(2026, 11, 10, 10, 30): (4000.0, 4006.0, 3999.0, 4006.0)}      # |4006 − 4000| >= 0.3·20
    assert at(T, bars(mv))["reason"] == "move"
    assert at(T, bars(mv), wakes=[{"check_utc": u(2026, 11, 10, 9, 30), "kind": "second_look",
                                   "reason": None}])["result"] == "none"      # move needs g >= 120
    # a bar after T − 30 min is never used; R20 needs 20 prior days
    assert at(u(2026, 11, 10, 10, 30), bars(up))["result"] == "none"
    assert wake.values(D, T, [b for b in bars() if b[0] >= u(2026, 10, 20)])["r20"] is None
    assert at(T, [b for b in bars(up) if b[0] >= u(2026, 10, 20)])["result"] == "none"


def test_order_gap_consumed_cap_open_trade_holiday_and_scheduled():
    T = u(2026, 11, 10, 11, 0)
    up = bars({u(2026, 11, 10, 10, 30): (4000.0, 4012.0, 3999.0, 4011.0)})
    trig = {"check_utc": u(2026, 11, 10, 8, 0), "kind": "trigger", "reason": "asia_up"}
    r = at(T, up, wakes=[trig])                                     # asia_up consumed: y_high is next
    assert (r["result"], r["reason"]) == ("window", "y_high")
    assert at(T, up, wakes=[{"check_utc": u(2026, 11, 10, 10, 30), "kind": "trigger",
                             "reason": "move"}])["result"] == "none"   # g = 30 < 60
    six = [{"check_utc": u(2026, 11, 10, h, 0), "kind": "trigger", "reason": None} for h in range(1, 7)]
    assert at(T, up, wakes=six)["result"] == "day_cap"
    trade = {"call_id": "2026-11-10_W1", "check_utc": u(2026, 11, 10, 7, 30), "exit_utc": u(2026, 11, 10, 12, 0)}
    assert at(T, up, trades=[trade])["result"] == "trade_open"
    assert at(u(2026, 11, 10, 11, 30), up, trades=[trade])["result"] != "trade_open"   # entry == exit is allowed
    assert at(T, up, holiday=True)["result"] == "none"
    s = at(u(2026, 11, 10, 9, 30), None, wakes=[trig] * 1, holiday=True)   # scheduled: no bar, no gap, holiday
    assert (s["result"], s["tag"], s["kind"], s["reason"]) == ("window", "W1b", "second_look", None)
    assert at(u(2026, 11, 10, 9, 30), None, trades=[trade])["result"] == "trade_open"


def test_context_block_and_wake_shape():
    T = u(2026, 11, 10, 11, 0)
    up = bars({u(2026, 11, 10, 10, 30): (4000.0, 4012.0, 3999.0, 4011.0)})
    used = wake.used_bars(up, T)
    prov = {"src": "td", "req": "XAUUSD_M30", "url": "u", "http": 200, "td_code": 200, "status": "ok",
            "reason": None, "bytes": 1, "sha256": "x", "values_n": len(up), "fetched_utc": None, "tries": 1,
            "bars": [{"t_utc": rec.iso(b[0]), "o": b[1], "h": b[2], "l": b[3], "c": b[4]} for b in used]}
    w = clock.window_of("2026-11-10_T1100")
    wk = records.wake_of(w, "asia_up", prov)
    assert records.valid_wake(wk, w.call_id) and wk["check_utc"] == "2026-11-10T11:00:00Z"
    assert not records.valid_wake(dict(wk, reason="calm"), w.call_id)
    assert not records.valid_wake(dict(wk, td=dict(prov, req="XAUUSD")), w.call_id)
    assert not records.valid_wake(dict(wk, check_utc="2026-11-10T11:30:00Z"), w.call_id)
    assert records.valid_wake(records.wake_of(clock.window_of("2026-11-10_W1b")), "2026-11-10_W1b")
    assert not records.valid_wake(dict(records.wake_of(clock.window_of("2026-11-10_W1")), reason="move"),
                                  "2026-11-10_W1")
    blk = wake.context_block(wk)
    assert list(blk) == ["kind", "reason", "check_utc", "values", "r20_days", "bars_m30"]
    assert blk["values"]["c_t"] == 4011.0 and blk["values"]["r20"] == 20.0 and blk["values"]["y_day"] == "2026-11-09"
    assert blk["values"]["c_prev_utc"] == "2026-11-10T09:30:00Z" and len(blk["r20_days"]) == 20
    assert blk["bars_m30"][0]["t_utc"] == "2026-11-09T23:00:00Z" and blk["bars_m30"][-1]["t_utc"] == \
        "2026-11-10T10:30:00Z"
    fixed = wake.context_block(records.wake_of(clock.window_of("2026-11-10_W2")))
    assert fixed == {"kind": "fixed", "reason": None, "check_utc": "2026-11-10T14:00:00Z", "values": None,
                     "r20_days": None, "bars_m30": None}


# ---------------------------------------------------------------- expected windows, settlement days, run matching
def test_expected_windows_include_second_looks_and_attempted_triggers():
    import json as _json

    from ga import merge, settle_run, state
    from ga.repo import DictView
    from synth import mode_json
    wit = {"seq": 1, "kind": "attempt", "status": "accepted", "id": "2026-11-10_T1100",
           "path": "analyst/attempts/2026-11-10_T1100_1.json"}
    rej = dict(wit, seq=2, status="rejected", id="2026-11-10_T1200")
    files = {"analyst/MODE.json": mode_json("live", canary_from=None, live_from="2026-11-09", a14_from="2026-11-10"),
             "analyst/witness/000001.json": _json.dumps(wit), "analyst/witness/000002.json": _json.dumps(rej)}
    view = DictView(files)
    doc = _json.loads(files["analyst/MODE.json"])
    ids = [w.call_id for w in merge.expected_windows(doc, "live", D, view)]
    assert ids == ["2026-11-09_W1", "2026-11-09_W2", "2026-11-10_W1", "2026-11-10_W1b", "2026-11-10_T1100",
                   "2026-11-10_W2", "2026-11-10_W2b"]
    assert [w.call_id for w in settle_run.day_expected(view, D)][-3:] == ["2026-11-10_T1100", "2026-11-10_W2",
                                                                         "2026-11-10_W2b"]
    assert settle_run.day_end(D) == u(2026, 11, 10, 22, 0)
    # R-AN-57: a W1b window is matched to a run of analyst-wake (or analyst-w1/w2) with the call's session_id
    call = {"self_reported": {"session_id": "s-9"}}
    term = {"status": "called"}
    runs = {"routine": "analyst-wake", "session_id": "s-9", "model": "other-model", "run_id": "r1",
            "created_utc": "2026-11-10T09:31:00Z"}
    v2 = DictView({"analyst/terminal/2026-11-10_W1b.json": _json.dumps(term),
                   "analyst/calls/2026-11-10_W1b.json": _json.dumps(call),
                   "analyst/runs/2026-W46.json": _json.dumps({"runs": [runs]})})
    over, unmatched = state.model_overlays(v2)
    assert over == {"2026-11-10_W1b": "r1"} and not unmatched
