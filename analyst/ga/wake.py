"""Watching: the trigger rule R(T) (register R-AN-89, §16.18 A14). FROZEN.

One pure rule, used by `run_window.py start` (R-AN-90) and by the tests; the watcher (R-AN-91) and the judge (check
A14) apply the same text. Nothing here reads a file, a clock or the network.

- Bars: Twelve Data XAU/USD 30-minute bars (t_utc aware UTC, o, h, l, c), labelled by their start. At check T only
  the bars with t_utc <= T − 30 min are used; a bar's model day is clock.tday(t_utc).
- Values at T: prior days = the model weekdays e < d with at least one used bar; R20 = statistics.median over the
  latest 20 prior days of (max h − min l), undefined with fewer than 20; y_day = the latest prior day (y_h, y_l);
  Asia = the used bars of d with t_utc < 07:00 UTC of d (asia_h, asia_l); bar_T = the bar with t_utc = T − 30 min
  (c_T its c); c_prev = the c of the latest used bar of d with t_utc <= T − 90 min.
- Conditions (all false without bar_T or R20), in priority order: asia_up (T >= 07:00 UTC of d, c_T > asia_h),
  asia_dn (c_T < asia_l), y_high (bar_T.h >= y_h − 0.1 * R20), y_low (bar_T.l <= y_l + 0.1 * R20), move (c_prev
  defined, abs(c_T − c_prev) >= 0.3 * R20), each evaluated exactly as written in IEEE doubles.
- E(T) = the windows of d with check < T that have an attempt; consumed(T) = the reasons of its trigger windows;
  g(T) = T − its latest check in minutes (infinite when empty). O(T) = the windows of d with check < T whose call is
  a buy or a sell and whose exit_utc > T + 30 min.
- R(T), for T in G(d): O(T) → trade_open; |E(T)| >= 6 → day_cap; a scheduled check → that window (fixed or
  second_look, reason null); a holiday-table day → none; g(T) < 60 → none; the first true condition not consumed
  (move only when g(T) >= 120) → the trigger window `<d>_T<hhmm>`; else none.
"""
import datetime as dt
import math
import statistics

from . import clock, rec

CONDITIONS = ("asia_up", "asia_dn", "y_high", "y_low", "move")
DAY_CAP = 6
PRIOR_DAYS = 20
MIN_GAP = 60
MOVE_GAP = 120


def bars_from_objs(objs):
    """[{t_utc, o, h, l, c}] (a provenance object's `bars`) -> [(t aware UTC, o, h, l, c)]."""
    return [(rec.parse_iso(b["t_utc"]), b["o"], b["h"], b["l"], b["c"]) for b in objs]


def used_bars(bars, T):
    """The bars closed at or before T, in time order."""
    lim = T - clock.HALF
    return sorted((b for b in bars if b[0] <= lim), key=lambda b: b[0])


def values(d, T, bars):
    """The values of R-AN-89 at check T of model day d from `bars` [(t, o, h, l, c)]; undefined values are None."""
    used = used_bars(bars, T)
    by_day = {}
    for b in used:
        by_day.setdefault(clock.tday(b[0]), []).append(b)
    prior = sorted(e for e in by_day if e < d and clock.is_weekday(e))
    today = by_day.get(d, [])
    ranges = [{"day": e.isoformat(), "h": max(b[2] for b in by_day[e]), "l": min(b[3] for b in by_day[e])}
              for e in prior[-PRIOR_DAYS:]]
    r20 = statistics.median([r["h"] - r["l"] for r in ranges]) if len(prior) >= PRIOR_DAYS else None
    y_day = prior[-1] if prior else None
    y_h = max(b[2] for b in by_day[y_day]) if y_day else None
    y_l = min(b[3] for b in by_day[y_day]) if y_day else None
    seven = dt.datetime(d.year, d.month, d.day, 7, tzinfo=rec.UTC)
    asia = [b for b in today if b[0] < seven]
    bar_t = next((b for b in today if b[0] == T - clock.HALF), None)
    prev = [b for b in today if b[0] <= T - dt.timedelta(minutes=90)]
    return {"bar_t": bar_t, "c_t": bar_t[4] if bar_t else None,
            "c_prev": prev[-1][4] if prev else None, "c_prev_utc": prev[-1][0] if prev else None,
            "asia_h": max(b[2] for b in asia) if asia else None, "asia_l": min(b[3] for b in asia) if asia else None,
            "y_day": y_day, "y_h": y_h, "y_l": y_l, "r20": r20, "r20_days": ranges, "today": today,
            "after_asia": T >= seven}


def conditions(v):
    """The conditions true at T, in priority order."""
    b, r20 = v["bar_t"], v["r20"]
    if b is None or r20 is None:
        return []
    c_t = b[4]
    out = []
    if v["after_asia"] and v["asia_h"] is not None and c_t > v["asia_h"]:
        out.append("asia_up")
    if v["after_asia"] and v["asia_l"] is not None and c_t < v["asia_l"]:
        out.append("asia_dn")
    if b[2] >= v["y_h"] - 0.1 * r20:
        out.append("y_high")
    if b[3] <= v["y_l"] + 0.1 * r20:
        out.append("y_low")
    if v["c_prev"] is not None and abs(c_t - v["c_prev"]) >= 0.3 * r20:
        out.append("move")
    return out


def earlier(wakes, T):
    """E(T): the entries of `wakes` ({check_utc, kind, reason}: the day's windows with an attempt) with check < T."""
    return [x for x in wakes if x["check_utc"] < T]


def open_trades(trades, T):
    """O(T): the entries of `trades` ({call_id, check_utc, exit_utc}: the day's windows whose call is a buy or a
    sell) with check < T and exit_utc > T + 30 min."""
    return [x for x in trades if x["check_utc"] < T and x["exit_utc"] > T + clock.HALF]


def gap_min(E, T):
    if not E:
        return math.inf
    return (T - max(x["check_utc"] for x in E)).total_seconds() / 60.0


def rule(d, T, bars, wakes, trades, holiday):
    """R(T) for T in G(d): {"result": trade_open | day_cap | window | none, "tag", "kind", "reason"}. `bars` may be
    None for a scheduled check (steps 1-3 need no bar); `holiday` is True when d has a row of either exchange."""
    out = {"result": "none", "tag": None, "kind": None, "reason": None}
    if open_trades(trades, T):
        return dict(out, result="trade_open")
    E = earlier(wakes, T)
    if len(E) >= DAY_CAP:
        return dict(out, result="day_cap")
    sched = clock.scheduled_checks(d)
    if T in sched:
        tag = sched[T]
        return dict(out, result="window", tag=tag, kind=clock.KIND[tag])
    if holiday:
        return out
    g = gap_min(E, T)
    if g < MIN_GAP:
        return out
    consumed = {x.get("reason") for x in E if x.get("kind") == "trigger"}
    for c in conditions(values(d, T, bars or [])):
        if c in consumed or (c == "move" and g < MOVE_GAP):
            continue
        return dict(out, result="window", tag=clock.trigger_tag(T), kind="trigger", reason=c)
    return out


def context_block(wake):
    """The context's `wake` (R-AN-23, analyst-context/4) from an attempt record's `wake`: kind, reason and check_utc
    as recorded; for a trigger, `values`, `r20_days` and `bars_m30` (the used bars of model day d) from wake.td.bars
    at check_utc; null otherwise. The prior days' 30-minute bars are not shown (A14-D9)."""
    out = {"kind": wake["kind"], "reason": wake["reason"], "check_utc": wake["check_utc"], "values": None,
           "r20_days": None, "bars_m30": None}
    if wake["kind"] != "trigger" or not wake.get("td"):
        return out
    T = rec.parse_iso(wake["check_utc"])
    d = clock.tday(T)
    v = values(d, T, bars_from_objs(wake["td"].get("bars") or []))
    out["values"] = {"c_t": v["c_t"], "c_prev": v["c_prev"],
                     "c_prev_utc": rec.iso(v["c_prev_utc"]) if v["c_prev_utc"] else None,
                     "asia_h": v["asia_h"], "asia_l": v["asia_l"],
                     "y_day": v["y_day"].isoformat() if v["y_day"] else None, "y_h": v["y_h"], "y_l": v["y_l"],
                     "r20": v["r20"]}
    out["r20_days"] = v["r20_days"]
    out["bars_m30"] = [{"t_utc": rec.iso(b[0]), "o": b[1], "h": b[2], "l": b[3], "c": b[4]} for b in v["today"]]
    return out
