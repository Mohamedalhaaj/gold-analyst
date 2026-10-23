"""Unit tests of the judge's independent arithmetic, each pinned to a register item."""
import datetime as dt
import importlib.util
import lzma
import os
import random
import struct

import pytest

from jlib import derive, settle, stats, ticks
from jlib.clock import Events, Holidays, Window, busday_count, model_to_utc, register_holiday_rows, to_server
from jlib.common import UTC, price_eq, rel_eq
from jlib.m1 import DaysTable, Store, decode_day

HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- R-AN-8 windows (the register's zoneinfo table)
@pytest.mark.parametrize("day,w1,w1slot,w2,w2slot", [
    ("2026-10-23", "07:00-11:00", 9, "13:30-17:30", 15),
    ("2026-10-26", "08:00-12:00", 10, "13:30-17:30", 15),
    ("2026-11-02", "08:00-12:00", 9, "14:30-18:30", 15),
    ("2027-03-12", "08:00-12:00", 9, "14:30-18:30", 15),
    ("2027-03-15", "08:00-12:00", 10, "13:30-17:30", 15),
    ("2027-03-29", "07:00-11:00", 9, "13:30-17:30", 15),
    ("2027-11-01", "08:00-12:00", 10, "13:30-17:30", 15),
    ("2027-11-08", "08:00-12:00", 9, "14:30-18:30", 15),
])
def test_windows_match_register_table(day, w1, w1slot, w2, w2slot):
    d = dt.date.fromisoformat(day)
    a, b = Window(d, "W1"), Window(d, "W2")
    assert f"{a.entry:%H:%M}-{a.exit:%H:%M}" == w1 and a.slot == w1slot
    assert f"{b.entry:%H:%M}-{b.exit:%H:%M}" == w2 and b.slot == w2slot
    assert a.deadline == a.entry - dt.timedelta(minutes=15)
    assert to_server(a.entry).date() == d and to_server(b.entry).date() == d  # R-AN-4 model day = local date


def test_model_clock_inverse():
    for d in ("2026-11-02", "2027-03-15", "2027-07-01"):
        m = dt.datetime.fromisoformat(d + " 00:00")
        assert to_server(model_to_utc(m)) == m


def test_holidays_own_exchange_only():
    h = Holidays(None)
    assert len(register_holiday_rows()) == 12 + 3 + 10 + 4
    assert not h.window_holiday(Window(dt.date(2026, 11, 26), "W1"))   # NYSE-only holiday: W1 runs
    assert h.window_holiday(Window(dt.date(2026, 11, 26), "W2"))
    assert not h.window_holiday(Window(dt.date(2026, 12, 28), "W2"))   # LSE-only: W2 runs
    assert h.window_holiday(Window(dt.date(2026, 12, 28), "W1"))
    assert h.window_holiday(Window(dt.date(2026, 11, 27), "W2"))       # early close counts
    assert not h.covers(dt.date(2028, 1, 3))


def test_busday_and_dref_stale_rule():
    assert busday_count(dt.date(2026, 11, 6), dt.date(2026, 11, 9)) == 1   # Friday -> Monday
    assert busday_count(dt.date(2026, 11, 2), dt.date(2026, 11, 9)) == 5   # Monday -> Monday
    assert busday_count(dt.date(2026, 11, 9), dt.date(2026, 11, 2)) == -5


def test_events_flags_and_horizon():
    e = Events.judge_copies()
    b = e.context_block(dt.date(2026, 11, 5))
    assert b["next"]["nfp"] == 1 and b["next_day"] == "2026-11-06" and b["today"]["nfp"] == 0
    late = e.flags(dt.date(2027, 12, 13))
    assert late == {"nfp": None, "cpi": None, "fomc": None}


# ---------------------------------------------------------------- R-AN-26/27 plan, S and T
def test_plan_validation():
    ok = {"action": "buy", "stop_mult": 1, "target_mult": 3.0, "confidence": "low", "reason_ar": "س",
          "key_risks_ar": ""}
    assert derive.plan_errors(ok) == []
    assert "keys" in derive.plan_errors(dict(ok, action="stay_out"))
    assert "stop_mult" in derive.plan_errors(dict(ok, stop_mult=1.25))
    assert "stop_mult" in derive.plan_errors(dict(ok, stop_mult=True))
    assert "reason_ar" in derive.plan_errors(dict(ok, reason_ar="س" * 401))
    assert "reason_ar" in derive.plan_errors(dict(ok, reason_ar=""))
    assert "key_risks_ar" in derive.plan_errors(dict(ok, key_risks_ar="a\x07"))
    assert derive.plan_errors(dict(ok, reason_ar="سطر\nسطر")) == []
    assert "keys" in derive.plan_errors(dict(ok, extra=1))


@pytest.mark.parametrize("cents,mult,dimes", [(3215, 1.5, 482), (3203, 1.5, 480), (3210, 1.0, 321), (3215, 3.0, 965),
                                             (3205, 1.0, 321), (3201, 2.0, 640)])
def test_st_half_up(cents, mult, dimes):
    s, _t = derive.st_dimes(cents, mult, 1.0)
    assert s == dimes


def test_dref_rule():
    rows = []
    d0 = dt.date(2026, 10, 1)
    for i in range(30):
        day = d0 + dt.timedelta(days=i)
        if day.weekday() < 5:
            rows.append({"day": day.isoformat(), "slot": "9", "F_S": str(70 + i), "nfp": "0", "cpi": "0",
                         "fomc": "0", "hol": "0", "computed_utc": f"{day.isoformat()}T23:30:00Z"})
    rows.append(dict(rows[-1], slot="15"))
    rows.append(dict(rows[-2], nfp="1"))
    rows.append(dict(rows[-3], F_S=""))
    d = dt.date(2026, 11, 2)
    sel = derive.dref_rows(rows, 9, d, "2026-11-02T07:20:00Z")
    assert len(sel) == 20 and sel[-1]["day"] == "2026-10-30"
    assert derive.dref_status(sel, d) is None
    v = derive.dref_values(sel, 4000.0)
    fs = sorted(r["F_S"] for r in sel)
    assert v["F_ref"] == (fs[9] + fs[10]) / 2
    assert v["D_ref"] == round(4000.0 * (1 - __import__("math").exp(-derive.K_DREF * v["F_ref"] / 1e4)), 2)
    assert derive.dref_status(sel[:9], d) == "rows"
    assert derive.dref_status(sel, dt.date(2026, 11, 9)) == "stale"


# ---------------------------------------------------------------- R-AN-41..44 settlement path rule
def _store(minutes, day=dt.date(2026, 11, 30)):
    """minutes: {i: (bid(o,h,l,c,vol)|None, ask(...)|None)} for minute i of the 08:00 UTC window."""
    st = Store(DaysTable(b"day,side,status,bytes,sha256,records,vol_minutes,fetched_utc\n"), lambda p: None)
    t0 = int(dt.datetime(day.year, day.month, day.day, 8, 0, tzinfo=UTC).timestamp())
    for i, (b, a) in minutes.items():
        if b is not None:
            st.side_rows["BID"][t0 + 60 * i] = b
        if a is not None:
            st.side_rows["ASK"][t0 + 60 * i] = a
    return st, Window(day, "W1")


def _flat(p, vol=1.0, spread=300):
    return (p, p, p, p, vol), (p + spread, p + spread, p + spread, p + spread, vol)


def test_settle_stop_by_level_and_tie():
    m = {i: _flat(4_000_000) for i in range(240)}
    b, a = m[10]
    m[10] = ((b[0], 4_100_000, 3_960_000, b[3], 1.0), a)   # low through X and high through Tg: stop wins
    st, win = _store(m)
    o = settle.settle_side(settle.Minutes(st, win), 1, 300, 600, False)  # S=$30, T=$60; P_e = ask open 4000.300
    assert o["P_e"] == 4000.3 and price_eq(o["X"], 3970.3) and o["reason"] == "stop" and o["fill"] == "level"
    assert o["exit_minute_utc"] == "2026-11-30T08:10:00Z" and price_eq(o["P_x"], 3970.3)
    assert rel_eq(o["net_usd"], -30.0 - 0.10) and rel_eq(o["R"], (-30.1) / 30.0)
    assert rel_eq(o["y"], max(-2.0, (-30.1 - 0.25) / 30.0))


def test_settle_stop_gap_open_and_target():
    m = {i: _flat(4_000_000) for i in range(240)}
    m[5] = _flat(3_900_000)
    st, win = _store(m)
    o = settle.settle_side(settle.Minutes(st, win), 1, 300, 600, False)
    assert o["reason"] == "stop" and o["fill"] == "open" and o["P_x"] == 3900.0
    m = {i: _flat(4_000_000) for i in range(240)}
    b, a = m[7]
    m[7] = ((b[0], 4_200_000, b[2], b[3], 1.0), a)
    st, win = _store(m)
    o = settle.settle_side(settle.Minutes(st, win), 1, 300, 600, False)
    assert o["reason"] == "target" and o["fill"] == "level" and price_eq(o["P_x"], 4060.3)


def test_settle_short_mirrors_on_ask():
    m = {i: _flat(4_000_000) for i in range(240)}
    b, a = m[9]
    m[9] = (b, (a[0], 4_040_000, a[2], a[3], 1.0))
    st, win = _store(m)
    o = settle.settle_side(settle.Minutes(st, win), -1, 300, 600, False)  # P_e = bid open 4000.000, X = 4030
    assert o["P_e"] == 4000.0 and price_eq(o["X"], 4030.0) and o["reason"] == "stop" and price_eq(o["P_x"], 4030.0)


def test_settle_no_quote_run_and_quoted_flats():
    m = {i: _flat(4_000_000) for i in range(240)}
    for i in range(20, 40):
        m[i] = _flat(4_000_000, vol=0.0)          # quoted but zero volume: tested for the stop, counts for the run
    st, win = _store(m)
    o = settle.settle_side(settle.Minutes(st, win), 1, 300, 600, False)
    assert o["reason"] == "gap" and o["exit_minute_utc"] == "2026-11-30T08:34:00Z"
    m = {i: _flat(4_000_000) for i in range(240)}
    for i in range(30, 38):
        m[i] = (None, None)
    m = {k: v for k, v in m.items() if v != (None, None)}
    st, win = _store(m)
    o = settle.settle_side(settle.Minutes(st, win), 1, 300, 600, False)
    assert o["reason"] == "time" and o["exit_minute_utc"] == "2026-11-30T11:59:00Z"


def test_settle_fallback_entry_no_quote_and_void():
    m = {i: _flat(4_000_000) for i in range(1, 240)}
    m[-3] = _flat(4_010_000)
    st, win = _store(m)
    o = settle.settle_side(settle.Minutes(st, win), 1, 300, 600, False)
    assert o["P_e_src"] == "fallback_close" and o["P_e"] == 4010.3 and o["P_e_minute_utc"] == "2026-11-30T07:57:00Z"
    st, win = _store({i: _flat(4_000_000) for i in range(1, 240)})
    o = settle.settle_side(settle.Minutes(st, win), 1, 300, 600, False)
    assert o["status"] == "no_quote" and o["y"] == -1.0 and o["net_usd"] == -30.0
    o = settle.settle_side(settle.Minutes(st, win), 1, 300, 600, True)
    assert o["status"] == "void" and o["R"] == -1.0 and o["cost_usd"] == 0.0


def test_twin_dw_from_point_sums():
    m = {i: _flat(4_000_000) for i in range(240)}
    m[239] = _flat(4_012_345)
    st, win = _store(m)
    me, mes, mx, mxs, dW, status = settle.twin(settle.Minutes(st, win), False)
    assert status == "final" and mes == "open" and mxs == "close" and dW == 12.345


# ---------------------------------------------------------------- R-AN-52 E and K against the lab's frozen eproc.py
def test_e_and_k_match_frozen_eproc():
    np = pytest.importorskip("numpy")
    spec = importlib.util.spec_from_file_location("frozen_eproc", os.path.join(HERE, "fixtures", "eproc.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rng = random.Random(3)
    for n in (0, 1, 5, 60, 300):
        ys = [max(-2.0, min(2.0, rng.gauss(0.05, 1.0))) for _ in range(n)]
        assert rel_eq(stats.e_value(ys), mod.e_value(ys))
        assert rel_eq(stats.k_value(ys), mod.k_value(ys))
        p = stats.e_path(ys)
        q = mod.e_path(ys)
        assert all(rel_eq(a, float(b)) for a, b in zip(p, q))
    assert np is not None


def test_promotion_rules():
    first = dt.date(2026, 11, 30)
    trades = [{"model_day": first + dt.timedelta(days=i), "w": "W1", "side": 1, "y": 2.0, "R": 2.2, "net": 70.0,
               "stress": 69.75, "cost": 0.5} for i in range(60)]
    twins = [(first + dt.timedelta(days=i), "W1", 0.0) for i in range(60)]
    p = stats.promotion(trades, twins, first, first + dt.timedelta(days=59))
    assert p["P1"] and p["P2"] and p["P3"] and p["P4"] and p["P5"] and p["all"]
    p = stats.promotion(trades[:59], twins, first, first + dt.timedelta(days=59))
    assert not p["P2"]
    assert stats.demotion([{"y": -2.0, "R": -1.0}] * 30)[0] in ("D1", "D2")


# ---------------------------------------------------------------- R-AN-16 hour files, R-V1-2 day files
def _tick_body(records):
    return lzma.compress(b"".join(struct.pack(">IIIff", *r) for r in records), format=lzma.FORMAT_ALONE)


def test_hour_file_status_rules():
    good = _tick_body([(0, 4_000_300, 4_000_000, 1.0, 1.0), (10, 4_000_500, 4_000_200, 1.0, 1.0)])
    st, s = ticks.classify(200, good)
    assert st == "present" and s["ticks"] == 2 and s["max_sum"] == 8_000_700
    assert ticks.classify(404, b"")[0] == "empty"
    assert ticks.classify(200, b"")[0] == "empty"
    assert ticks.classify(503, b"x")[0] == "error"
    assert ticks.classify(200, b"not lzma")[0] == "invalid"
    assert ticks.classify(200, _tick_body([(3_600_000, 1, 1, 0.0, 0.0)]))[0] == "invalid"
    assert ticks.classify(200, _tick_body([(10, 1, 1, 0.0, 0.0), (5, 1, 1, 0.0, 0.0)]))[0] == "invalid"
    assert ticks.classify(200, _tick_body([(10, 0, 1, 0.0, 0.0)]))[0] == "invalid"
    assert ticks.hour_url("XAUUSD", dt.datetime(2026, 11, 2, 7, tzinfo=UTC)).endswith("XAUUSD/2026/10/02/07h_ticks.bi5")


def test_day_file_decode_rules():
    rec = struct.Struct(">iiiiif")
    good = lzma.compress(rec.pack(0, 1, 2, 0, 3, 1.0) + rec.pack(60, 2, 2, 2, 2, 0.0), format=lzma.FORMAT_ALONE)
    rows = decode_day(good, dt.date(2026, 11, 2))
    assert len(rows) == 2
    bad = lzma.compress(rec.pack(60, 1, 2, 0, 3, 1.0) + rec.pack(60, 2, 2, 2, 2, 0.0), format=lzma.FORMAT_ALONE)
    with pytest.raises(ValueError):
        decode_day(bad, dt.date(2026, 11, 2))
    assert decode_day(b"", dt.date(2026, 11, 2)) == {}


def test_complete_utc_rule():
    lines = ["day,side,status,bytes,sha256,records,vol_minutes,fetched_utc"]
    for d in ("2026-09-01", "2026-09-02"):
        for s in ("BID", "ASK"):
            lines.append(f"{d},{s},ok,1,x,1440,1,{d}T23:00:00Z")
    lines.append("2026-09-03,BID,missing,,,,,")
    t = DaysTable(("\n".join(lines) + "\n").encode())
    assert t.complete_utc("2026-09-03T01:00:00Z") == "2026-09-02T00:00:00Z"   # D + 26 h not reached for 09-02
    assert t.complete_utc("2026-09-05T00:00:00Z") == "2026-09-03T00:00:00Z"   # 09-03 ASK has no row


def test_real_store_decodes_if_present():
    root = "/Users/mohamedalhaj/Documents/gold-forward-watch/data/system/"
    if not os.path.exists(root + "days.csv"):
        pytest.skip("gold-forward-watch not on this machine")
    days = DaysTable(open(root + "days.csv", "rb").read())
    st = Store(days, lambda p: open(root + p[len("mirror/system/"):], "rb").read())
    st.load_day(dt.date(2026, 10, 20))
    assert not st.mismatch and len(st.side_rows["BID"]) == 1440


def test_tolerances():
    assert price_eq(4000.0005, (4_000_000 / 1000.0 + 4_000_001 / 1000.0) / 2)
    assert not price_eq(4000.0005, 4000.001)
    assert rel_eq(1.0, 1.0 + 1e-12) and not rel_eq(1.0, 1.0001) and rel_eq(None, None) and not rel_eq(None, 0.0)
