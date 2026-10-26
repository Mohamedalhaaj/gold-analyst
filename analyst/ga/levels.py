"""Levels of the session context (register R-AN-88, §16.18 A12). FROZEN.

Computed after D_ref from the context's own bars, with no request; the context sets `levels` null when its status is
not `ok`. Inputs: P = p_ref.p_ref; D = d_ref.D_ref (dollars); `bars` = R-AN-17's hourly bars over [start_lv, h_new]
(those from start_hist on are bars_h1), oldest first; `bars_d1` = the 25 history weekdays. A bar's model day and model
hour are the date and the hour of its `t_server`.

- rnd(x, k) = Python round(x, k) on the double; dist(v) = rnd((v − P)/D, 2): positive above P, 1.0 = one D_ref.
  Prices are copied from their bars, unrounded.
- prev_day: the last bars_d1 entry with dist_h, dist_l, dist_c; null when bars_d1 is empty.
- week_open: the earliest bar whose model day is in [Monday on or before d, d]; null when there is none.
- asia: today's bars at model hours 1 to 9 (server 01:00-10:00, VolMap's L2), null when there is none; `hist` = the
  last 20 bars_d1 days measured over model hours 1 to k_end (the model hour of today's last Asia bar), days without
  such bars left out; width_ratio = rnd(today's width / median hist width, 2), null with fewer than 10 hist days or a
  median <= 0.
- ret_1d, ret_5d, ret_20d: rnd(100·(c[N−1]/c[N−1−n] − 1), 3) over bars_d1 closes; null when N < n + 1.
- vol_ratio: rnd(fmean(range of the last 5 bars_d1) / fmean(range of the last 20), 2); null when bars_d1 has fewer
  than 20 entries or the denominator is <= 0.
A missing input makes only its own field null.
"""
import datetime as dt
import statistics

from . import clock, rec

ASIA_FIRST_HOUR, ASIA_LAST_HOUR = 1, 9
HIST_DAYS = 20
MIN_HIST = 10
RET_DAYS = (1, 5, 20)
VOL_SHORT, VOL_LONG = 5, 20
KEYS = ("prev_day", "week_open", "asia", "ret_1d", "ret_5d", "ret_20d", "vol_ratio")


def rnd(x, k):
    return round(x, k)


def _day(b):
    return b["t_server"][:10]


def _hour(b):
    return int(b["t_server"][11:13])


def compute(d, bars, bars_d1, p, dref):
    """The `levels` object for model day `d` (a date)."""
    def dist(v):
        return rnd((v - p) / dref, 2)

    out = dict.fromkeys(KEYS)
    if bars_d1:
        e = bars_d1[-1]
        out["prev_day"] = {"day": e["day"], "h": e["h"], "l": e["l"], "c": e["c"], "dist_h": dist(e["h"]),
                           "dist_l": dist(e["l"]), "dist_c": dist(e["c"])}
    monday = (d - dt.timedelta(days=d.weekday())).isoformat()
    week = [b for b in bars if monday <= _day(b) <= d.isoformat()]
    if week:
        b = min(week, key=lambda x: x["t_utc"])
        out["week_open"] = {"t_utc": b["t_utc"], "o": b["o"], "dist": dist(b["o"])}
    today = d.isoformat()
    asia = [b for b in bars if _day(b) == today and ASIA_FIRST_HOUR <= _hour(b) <= ASIA_LAST_HOUR]
    if asia:
        k_end = _hour(asia[-1])
        hist = []
        for e in bars_d1[-HIST_DAYS:]:
            bs = [b for b in bars if _day(b) == e["day"] and ASIA_FIRST_HOUR <= _hour(b) <= k_end]
            if bs:
                hist.append({"day": e["day"], "h": max(b["h"] for b in bs), "l": min(b["l"] for b in bs)})
        hi, lo = max(b["h"] for b in asia), min(b["l"] for b in asia)
        width_ratio = None
        if len(hist) >= MIN_HIST:
            m = statistics.median([x["h"] - x["l"] for x in hist])
            if m > 0:
                width_ratio = rnd((hi - lo) / m, 2)
        out["asia"] = {
            "from_utc": rec.iso(clock.model_to_utc(dt.datetime.combine(d, dt.time(ASIA_FIRST_HOUR)))),
            "to_utc": rec.iso(rec.parse_iso(asia[-1]["t_utc"]) + clock.HOUR), "n_hours": len(asia),
            "complete": len(asia) == ASIA_LAST_HOUR - ASIA_FIRST_HOUR + 1, "h": hi, "l": lo, "dist_h": dist(hi),
            "dist_l": dist(lo), "width_ratio": width_ratio, "hist": hist}
    closes = [e["c"] for e in bars_d1]
    n_c = len(closes)
    for n in RET_DAYS:
        if n_c >= n + 1:
            out[f"ret_{n}d"] = rnd(100.0 * (closes[n_c - 1] / closes[n_c - 1 - n] - 1.0), 3)
    if len(bars_d1) >= VOL_LONG:
        den = statistics.fmean([e["h"] - e["l"] for e in bars_d1[-VOL_LONG:]])
        if den > 0:
            out["vol_ratio"] = rnd(statistics.fmean([e["h"] - e["l"] for e in bars_d1[-VOL_SHORT:]]) / den, 2)
    return out
