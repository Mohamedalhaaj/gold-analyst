"""Settlement (prereg §4; register R-AN-41..R-AN-47): the path cases, money, void, no_quote, the opposite side, the
drift twin, and equality with lab v2's frozen execution (gold-forward-watch v2_exec.py, sha256 a59707d1...)."""
import datetime as dt
import hashlib
import importlib.util
import math
from pathlib import Path

import numpy as np
import pytest

from ga import clock, rec, settle, store

HERE = Path(__file__).resolve().parent
V2_SHA = "a59707d1f3908b7149fb38b9c5abeb6b1c1fc4db0ddb8b35d61e4d68837beb05"
W = clock.Window(dt.date(2026, 11, 10), "W1")           # entry 08:00 UTC, exit 12:00 UTC (EST)
T0 = int(W.entry_utc.timestamp())
DAY0 = int(clock.model_to_utc(dt.datetime(2026, 11, 10)).timestamp())


def minutes(rows):
    """rows: {t_epoch: (bid (o,h,l,c,vol) | None, ask (...) | None)} -> store.Minutes."""
    m = store.Minutes()
    for t, (b, a) in rows.items():
        if b is not None:
            m.side["BID"][t] = b
        if a is not None:
            m.side["ASK"][t] = a
    return m


def flat_path(n=240, mid=4000000, spread=200, vol=1.0, start=T0 - 600):
    rows = {}
    for i in range((T0 - start) // 60 + n):
        t = start + 60 * i
        b = (mid - spread // 2,) * 4
        a = (mid + spread // 2,) * 4
        rows[t] = ((b[0], b[1], b[2], b[3], vol), (a[0], a[1], a[2], a[3], vol))
    return rows


def set_bid(rows, i, o, h, l, c, vol=1.0):
    t = T0 + 60 * i
    a = rows[t][1]
    rows[t] = ((o, h, l, c, vol), a)


S_D, T_D = 200, 400       # S = $20.0, T = $40.0 -> 20000 and 40000 points


def test_time_exit_and_money():
    rows = flat_path()
    out, aux = settle.trade(minutes(rows), W, 1, S_D, T_D)
    assert out["status"] == "final" and out["reason"] == "time" and out["fill"] == "close"
    assert out["P_e"] == 4000.1 and out["P_x"] == 3999.9 and out["P_e_src"] == "open"
    assert out["exit_minute_utc"] == "2026-11-10T11:59:00Z"
    assert out["net_usd"] == pytest.approx(-0.2 - 0.10, abs=1e-12)
    assert out["stress_net_usd"] == pytest.approx(out["net_usd"] - 0.25, abs=1e-12)
    assert out["R"] == pytest.approx(out["net_usd"] / 20.0, abs=1e-15)
    assert out["y"] == out["R_stress"]
    assert out["cost_usd"] == pytest.approx(0.10 + 0.1 + 0.1, abs=1e-12)        # half spreads 0.1 each
    assert out["gross_usd"] == pytest.approx(out["net_usd"] + out["cost_usd"], abs=1e-12)
    assert out["X"] == 3980.1 and out["Tg"] == 4040.1


def test_stop_level_and_open_gap_and_tie_with_target():
    rows = flat_path()
    set_bid(rows, 10, 3999900, 4000000, 3980000, 3990000)                     # low through X = 3980.1
    out, _ = settle.trade(minutes(rows), W, 1, S_D, T_D)
    assert (out["reason"], out["fill"], out["P_x"]) == ("stop", "level", 3980.1)
    assert out["exit_minute_utc"] == "2026-11-10T08:10:00Z"
    rows = flat_path()
    set_bid(rows, 5, 3970000, 3990000, 3960000, 3985000)                      # opens through X
    out, _ = settle.trade(minutes(rows), W, 1, S_D, T_D)
    assert (out["reason"], out["fill"], out["P_x"]) == ("stop", "open", 3970.0)
    rows = flat_path()
    set_bid(rows, 7, 4000000, 4050000, 3970000, 4000000)                      # both in one minute: the stop wins
    out, _ = settle.trade(minutes(rows), W, 1, S_D, T_D)
    assert out["reason"] == "stop" and out["P_x"] == 3980.1


def test_target_and_target_gap_fills_at_target():
    rows = flat_path()
    set_bid(rows, 30, 4010000, 4045000, 4005000, 4020000)
    out, _ = settle.trade(minutes(rows), W, 1, S_D, T_D)
    assert (out["reason"], out["fill"], out["P_x"]) == ("target", "level", 4040.1)
    rows = flat_path()
    set_bid(rows, 30, 4060000, 4070000, 4055000, 4065000)                     # gaps over the target
    out, _ = settle.trade(minutes(rows), W, 1, S_D, T_D)
    assert out["P_x"] == 4040.1 and out["reason"] == "target"                 # never better than Tg


def test_short_mirrors_on_the_ask():
    rows = flat_path()
    t = T0 + 60 * 12
    b = rows[t][0]
    rows[t] = (b, (4000100, 4021000, 4000000, 4010000, 1.0))                 # ask high through X = 3999.9 + 20
    out, _ = settle.trade(minutes(rows), W, -1, S_D, T_D)
    assert out["P_e"] == 3999.9 and out["X"] == 4019.9 and out["Tg"] == 3959.9
    assert (out["reason"], out["P_x"]) == ("stop", 4019.9)
    assert out["net_usd"] == pytest.approx(-(4019.9 - 3999.9) - 0.10, abs=1e-9)


def test_no_quote_run_of_15_exits_at_x_and_flats_are_tested():
    rows = flat_path()
    for i in range(20, 35):                                                    # 15 minutes without volume
        t = T0 + 60 * i
        b, a = rows[t]
        rows[t] = (b[:4] + (0.0,), a[:4] + (0.0,))
    out, _ = settle.trade(minutes(rows), W, 1, S_D, T_D)
    assert (out["reason"], out["fill"], out["P_x"]) == ("gap", "level", 3980.1)
    assert out["exit_minute_utc"] == "2026-11-10T08:34:00Z"
    rows = flat_path()
    t = T0 + 60 * 3
    rows[t] = ((3990000, 3990000, 3970000, 3990000, 0.0), rows[t][1])         # a zero-volume minute with a row
    out, _ = settle.trade(minutes(rows), W, 1, S_D, T_D)
    assert out["reason"] == "stop" and out["exit_minute_utc"] == "2026-11-10T08:03:00Z"
    rows = flat_path()
    for i in range(40, 55):                                                    # 15 missing minutes
        del rows[T0 + 60 * i]
    out, _ = settle.trade(minutes(rows), W, 1, S_D, T_D)
    assert out["reason"] == "gap" and out["exit_minute_utc"] == "2026-11-10T08:54:00Z"


def test_entry_fallback_and_no_quote():
    rows = flat_path()
    t = T0
    rows[t] = (rows[t][0], None)                                               # no ask row at the entry minute
    out, _ = settle.trade(minutes(rows), W, 1, S_D, T_D)
    assert out["P_e_src"] == "fallback_close" and out["P_e_minute_utc"] == "2026-11-10T07:59:00Z"
    rows = {k: (b, None) for k, (b, a) in flat_path().items()}                # no ask at all that day
    out, aux = settle.trade(minutes(rows), W, 1, S_D, T_D)
    assert out["status"] == "no_quote" and (out["y"], out["R"], out["net_usd"]) == (-1.0, -1.0, -20.0)
    assert aux is None


def test_time_exit_fallback_close():
    rows = flat_path()
    del rows[T0 + 60 * 239]
    set_bid(rows, 238, 4000000, 4000000, 4000000, 4001000)
    out, _ = settle.trade(minutes(rows), W, 1, S_D, T_D)
    assert out["reason"] == "time" and out["P_x"] == 4001.0 and out["exit_minute_utc"] == "2026-11-10T11:58:00Z"


def test_void_needs_both_sides_missing():
    rbk = {("2026-11-10", "BID"): {"status": "missing"}, ("2026-11-10", "ASK"): {"status": "missing"}}
    assert settle.is_void(rbk, W)
    rbk[("2026-11-10", "ASK")] = {"status": "ok"}
    assert not settle.is_void(rbk, W)


def test_twin():
    rows = flat_path()
    set_bid(rows, 239, 4000000, 4000000, 4000000, 4002000)
    st, me, mes, mx, mxs, dW = settle.twin(minutes(rows), W)
    assert (st, mes, mxs) == ("final", "open", "close")
    assert me == 4000.0 and mx == (4002.0 + 4000.1) / 2
    assert dW == pytest.approx((4002000 + 4000100 - (3999900 + 4000100)) / 2000)


# ---------------------------------------------------------------- equality with lab v2's execution
def load_v2():
    p = HERE / "vendor" / "v2_exec.py"
    assert hashlib.sha256(p.read_bytes()).hexdigest() == V2_SHA
    spec = importlib.util.spec_from_file_location("v2_exec_vendored", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class MD:
    """lab.execution.Minutes for one model day, built from the same rows."""

    def __init__(self, rows):
        self.pos = np.full(1440, -1, dtype=int)
        cols = {k: [] for k in ("bid_o", "bid_h", "bid_l", "bid_c", "ask_o", "ask_h", "ask_l", "ask_c")}
        vol = []
        ts = sorted(rows)
        for i, t in enumerate(ts):
            self.pos[(t - DAY0) // 60] = i
            b, a = rows[t]
            for k, j in (("o", 0), ("h", 1), ("l", 2), ("c", 3)):
                cols["bid_" + k].append(b[j] / 1000.0 if b else np.nan)
                cols["ask_" + k].append(a[j] / 1000.0 if a else np.nan)
            vol.append(bool(b and a and b[4] > 0 and a[4] > 0))
        for k, v in cols.items():
            setattr(self, k, np.array(v, dtype=float))
        for k in "ohlc":
            setattr(self, k, (getattr(self, "bid_" + k) + getattr(self, "ask_" + k)) / 2)
        self.vol = np.array(vol, dtype=bool)

    def row(self, minute):
        return self.pos[minute] if 0 <= minute < 1440 else -1


def random_rows(rng):
    rows = {}
    mid = 4000000
    for i in range(-300, 240):
        t = T0 + 60 * i
        if t < DAY0:
            continue
        mid += int(rng.normal(0, 400))
        if rng.random() < 0.01:
            mid += int(rng.choice([-1, 1]) * rng.integers(5000, 30000))      # jumps through the stop
        sp = int(rng.integers(100, 400))
        hi, lo = mid + int(abs(rng.normal(0, 600))), mid - int(abs(rng.normal(0, 600)))
        c = int(rng.integers(lo, hi + 1))
        vb = 0.0 if rng.random() < 0.05 else 1.0
        va = 0.0 if rng.random() < 0.05 else 1.0
        b = (mid - sp // 2, hi - sp // 2, lo - sp // 2, c - sp // 2, vb)
        a = (mid + sp // 2, hi + sp // 2, lo + sp // 2, c + sp // 2, va)
        r = rng.random()
        if r < 0.03:
            rows[t] = (b, None)
        elif r < 0.06:
            rows[t] = (None, a)
        elif r < 0.10:
            continue
        else:
            rows[t] = (b, a)
        mid = c
    if rng.random() < 0.1:                                                     # long holes: gap exits
        k = int(rng.integers(0, 220))
        for i in range(k, k + 16):
            rows.pop(T0 + 60 * i, None)
    return rows


def test_equal_to_lab_v2_execution_without_target():
    v2 = load_v2()
    rng = np.random.default_rng(20261023)
    T = (T0 - DAY0) // 60 - 1
    n_stop = n_gap = n_time = 0
    for _ in range(400):
        rows = random_rows(rng)
        S_d = int(rng.integers(50, 400))
        for side in (1, -1):
            mine, _ = settle.trade(minutes(rows), W, side, S_d, 10 ** 7)       # a target that is never reached
            orig = v2.unit_d
            v2.unit_d = lambda P_e, F_S, _s=S_d: (_s / 10) / 3.0
            try:
                ref = v2.trade(MD(rows), side, T, 241, F_S=1.0)
            finally:
                v2.unit_d = orig
            if ref["status"] == "no_quote":
                assert mine["status"] == "no_quote"
                continue
            assert mine["status"] == "final"
            assert mine["reason"] == ref["reason"]
            assert (rec.parse_iso(mine["exit_minute_utc"]) - clock.model_to_utc(dt.datetime(2026, 11, 10))) \
                // dt.timedelta(minutes=1) == ref["exit_min"]
            assert mine["P_e"] == pytest.approx(ref["P_e"], rel=1e-12)
            assert mine["P_x"] == pytest.approx(ref["P_x"], rel=1e-9)
            assert mine["net_usd"] == pytest.approx(ref["net_usd"], abs=1e-9)
            assert mine["cost_usd"] == pytest.approx(ref["cost_usd"], abs=1e-9)
            assert mine["R"] == pytest.approx(ref["net_usd"] / (S_d / 10), rel=1e-9, abs=1e-9)   # R in S units
            n_stop += mine["reason"] == "stop"
            n_gap += mine["reason"] == "gap"
            n_time += mine["reason"] == "time"
    assert n_stop > 50 and n_gap > 5 and n_time > 50
