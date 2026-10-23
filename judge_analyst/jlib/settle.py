"""Independent settlement of a call (R-AN-40..R-AN-47): entry, path rule, money, void/no_quote, the opposite side,
the path summary and the drift twin. Integer points throughout (1 point = $0.001); every level comparison is exact."""
from __future__ import annotations

import datetime as dt

from .clock import Window, model_to_utc
from .common import UTC, fmt_utc
from .m1 import Store

MIN = 60


def _clip(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


class Minutes:
    """The store minutes of UTC days d−1 and d for one window (R-AN-41)."""

    def __init__(self, store: Store, win: Window):
        self.store = store
        self.win = win
        self.t0 = int(win.entry.timestamp())
        self.t1 = int(win.exit.timestamp())
        d = win.day
        self.mds = int(model_to_utc(dt.datetime(d.year, d.month, d.day)).timestamp())

    def row(self, side, t):
        return self.store.side(side, t)

    def live(self, t):
        b = self.store.side("BID", t)
        a = self.store.side("ASK", t)
        return b is not None and a is not None and b[4] > 0 and a[4] > 0

    def half_spread(self, t, field):
        """Half spread in $ at minute t on field 'o' or 'c'; 0 without both sides."""
        b = self.store.side("BID", t)
        a = self.store.side("ASK", t)
        if b is None or a is None:
            return 0.0
        i = 0 if field == "o" else 3
        return (a[i] - b[i]) / 2000.0


def _null_outcome(status, S):
    o = {k: None for k in ("P_e", "P_e_src", "P_e_minute_utc", "X", "Tg", "exit_minute_utc", "P_x", "reason", "fill",
                           "gross_usd")}
    o.update({"status": status, "cost_usd": 0.0, "net_usd": -S, "stress_net_usd": -S, "R": -1.0, "R_stress": -1.0,
              "y": -1.0})
    return o


def settle_side(mins: Minutes, s: int, S_dimes: int, T_dimes: int, void: bool):
    """Outcome dict of R-AN-46 for side s, plus private keys `_exit_t` (epoch) and `_pe_pts`."""
    S = S_dimes / 10
    if void:
        o = _null_outcome("void", S)
        o["_exit_t"] = None
        return o
    entry_side = "ASK" if s == 1 else "BID"
    exit_side = "BID" if s == 1 else "ASK"
    t0, t1 = mins.t0, mins.t1
    r = mins.row(entry_side, t0)
    if r is not None:
        pe, pe_src, pe_t, pe_field = r[0], "open", t0, "o"
    else:
        pe = None
        for t in range(t0 - MIN, mins.mds - 1, -MIN):
            rr = mins.row(entry_side, t)
            if rr is not None:
                pe, pe_src, pe_t, pe_field = rr[3], "fallback_close", t, "c"
                break
        if pe is None:
            o = _null_outcome("no_quote", S)
            o["_exit_t"] = None
            return o
    S_pts = 100 * S_dimes
    T_pts = 100 * T_dimes
    X = pe - s * S_pts
    Tg = pe + s * T_pts
    run = 0
    exit_t = px = reason = fill = None
    for t in range(t0, t1, MIN):
        q = mins.row(exit_side, t)
        if q is not None:
            o_, h_, l_, _c = q[0], q[1], q[2], q[3]
            if s == 1:
                if o_ <= X:
                    exit_t, px, reason, fill = t, o_, "stop", "open"
                elif l_ <= X:
                    exit_t, px, reason, fill = t, X, "stop", "level"
                elif h_ >= Tg:
                    exit_t, px, reason, fill = t, Tg, "target", "level"
            else:
                if o_ >= X:
                    exit_t, px, reason, fill = t, o_, "stop", "open"
                elif h_ >= X:
                    exit_t, px, reason, fill = t, X, "stop", "level"
                elif l_ <= Tg:
                    exit_t, px, reason, fill = t, Tg, "target", "level"
            if exit_t is not None:
                break
        run = 0 if mins.live(t) else run + 1
        if run >= 15:
            exit_t, px, reason, fill = t, X, "gap", "level"
            break
    if exit_t is None:
        last = t1 - MIN
        q = mins.row(exit_side, last)
        if q is not None:
            exit_t, px, reason, fill = last, q[3], "time", "close"
        else:
            for t in range(t1 - 2 * MIN, t0 - 1, -MIN):
                q = mins.row(exit_side, t)
                if q is not None:
                    exit_t, px, reason, fill = t, q[3], "time", "close"
                    break
            if exit_t is None:
                exit_t, px, reason, fill = last, X, "gap", "level"
    net = s * (px - pe) / 1000 - 0.10
    stress = net - 0.25
    cost = 0.10 + mins.half_spread(pe_t, pe_field) + mins.half_spread(exit_t, "o" if fill == "open" else "c")
    return {"status": "final", "P_e": pe / 1000, "P_e_src": pe_src,
            "P_e_minute_utc": fmt_utc(dt.datetime.fromtimestamp(pe_t, UTC)), "X": X / 1000, "Tg": Tg / 1000,
            "exit_minute_utc": fmt_utc(dt.datetime.fromtimestamp(exit_t, UTC)), "P_x": px / 1000, "reason": reason,
            "fill": fill, "cost_usd": cost, "gross_usd": net + cost, "net_usd": net, "stress_net_usd": stress,
            "R": net / S, "R_stress": stress / S, "y": _clip(stress / S, -2.0, 2.0), "_exit_t": exit_t,
            "_pe_pts": pe}


def path_summary(mins: Minutes, s: int, outcome: dict):
    if outcome["status"] != "final":
        return None
    st = mins.store
    t0, t1 = mins.t0, mins.t1
    buckets = []
    hi_all = lo_all = None
    for k in range(16):
        ms = []
        for t in range(t0 + 15 * MIN * k, t0 + 15 * MIN * (k + 1), MIN):
            mb = st.mid_bar(t)
            if mb is not None:
                ms.append(mb)
        if ms:
            b = {"k": k, "o": ms[0][0], "h": max(m[1] for m in ms), "l": min(m[2] for m in ms), "c": ms[-1][3],
                 "n": len(ms)}
            hi_all = b["h"] if hi_all is None else max(hi_all, b["h"])
            lo_all = b["l"] if lo_all is None else min(lo_all, b["l"])
        else:
            b = {"k": k, "o": None, "h": None, "l": None, "c": None, "n": 0}
        buckets.append(b)
    exit_side = "BID" if s == 1 else "ASK"
    pe = outcome["_pe_pts"]
    hs, ls = [], []
    for t in range(t0, outcome["_exit_t"] + MIN, MIN):
        q = st.side(exit_side, t)
        if q is not None:
            hs.append(q[1])
            ls.append(q[2])
    if hs:
        if s == 1:
            mfe, mae = (max(hs) - pe) / 1000, (min(ls) - pe) / 1000
        else:
            mfe, mae = (pe - min(ls)) / 1000, (pe - max(hs)) / 1000
    else:
        mfe = mae = None
    return {"buckets": buckets, "mfe_usd": mfe, "mae_usd": mae,
            "exit_after_min": (outcome["_exit_t"] - t0) // MIN,
            "range_mid_usd": None if hi_all is None else hi_all - lo_all}


def is_void(days, d: dt.date) -> bool:
    """R-AN-44: both sides of UTC day d have `missing` rows."""
    b = days.row(d, "BID")
    a = days.row(d, "ASK")
    return b is not None and a is not None and b["status"] == "missing" and a["status"] == "missing"


def twin(mins: Minutes, void: bool):
    """(mid_entry, mid_entry_src, mid_exit, mid_exit_src, d_W, status) of R-AN-47."""
    if void:
        return None, None, None, None, None, "void"
    st = mins.store
    t0, t1 = mins.t0, mins.t1
    e_sum = e_src = None
    x = st.both(t0)
    if x is not None:
        e_sum, e_src = x[0][0] + x[1][0], "open"
    else:
        for t in range(t0 - MIN, mins.mds - 1, -MIN):
            x = st.both(t)
            if x is not None:
                e_sum, e_src = x[0][3] + x[1][3], "fallback_close"
                break
    x_sum = x_src = None
    x = st.both(t1 - MIN)
    if x is not None:
        x_sum, x_src = x[0][3] + x[1][3], "close"
    else:
        for t in range(t1 - 2 * MIN, t0 - 1, -MIN):
            x = st.both(t)
            if x is not None:
                x_sum, x_src = x[0][3] + x[1][3], "fallback_close"
                break
    if e_sum is None or x_sum is None:
        return (None if e_sum is None else e_sum / 2000, e_src, None if x_sum is None else x_sum / 2000, x_src, None,
                "no_quote")
    return e_sum / 2000, e_src, x_sum / 2000, x_src, (x_sum - e_sum) / 2000, "final"


def public(outcome: dict) -> dict:
    return {k: v for k, v in outcome.items() if not k.startswith("_")}
