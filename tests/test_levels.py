"""Levels (register R-AN-88, §16.18 A12): prev_day, week_open, asia with its hist and width_ratio, ret_*, vol_ratio,
their rounding and their nulls, computed from the context's own bars."""
import datetime as dt
import statistics

from ga import clock, levels, rec

D = dt.date(2026, 11, 10)                                   # a Tuesday
P, DREF = 4000.0, 31.32


def hour_bar(day, hour, o, h, l, c):
    """A bar at model `day` `hour` (server time), keyed as the context's bars."""
    t = clock.model_to_utc(dt.datetime.combine(day, dt.time(hour)))
    return {"t_utc": rec.iso(t), "t_server": rec.server_str(clock.to_server(t)), "o": o, "h": h, "l": l, "c": c,
            "n": None, "src": "td"}


def px(k, hour):
    """A deterministic price for the k-th history day (0 = today) and model hour."""
    return 3900.0 + 3.0 * k + 0.7 * hour + (k % 3) * 1.1


def day_bars(day, k, hours=range(1, 24), wide=0.0):
    out = []
    for hr in hours:
        o = px(k, hr)
        c = o + (0.4 if hr % 2 else -0.3)
        out.append(hour_bar(day, hr, o, max(o, c) + 1.0 + wide + 0.05 * k, min(o, c) - 1.0, c))
    return out


def d1(day, bs):
    return {"day": day.isoformat(), "o": bs[0]["o"], "h": max(b["h"] for b in bs), "l": min(b["l"] for b in bs),
            "c": bs[-1]["c"], "n_hours": len(bs)}


def make(n_days=25, today_hours=range(1, 6), skip=(), short_days=()):
    """Bars of n_days history weekdays (all hours; `short_days` only hours 12-23) and today's hours."""
    bars, bars_d1 = [], []
    for k in range(n_days, 0, -1):
        day = clock.weekday_before(D, k)
        if k in skip:
            continue
        bs = day_bars(day, k, range(12, 24) if k in short_days else range(1, 24))
        bars += bs
        bars_d1.append(d1(day, bs))
    bars += day_bars(D, 0, today_hours, wide=2.0)
    return bars, bars_d1


def dist(v):
    return round((v - P) / DREF, 2)


def test_full_levels_shape_and_values():
    bars, bars_d1 = make()
    lv = levels.compute(D, bars, bars_d1, P, DREF)
    assert list(lv) == ["prev_day", "week_open", "asia", "ret_1d", "ret_5d", "ret_20d", "vol_ratio"]
    e = bars_d1[-1]
    assert lv["prev_day"] == {"day": "2026-11-09", "h": e["h"], "l": e["l"], "c": e["c"], "dist_h": dist(e["h"]),
                              "dist_l": dist(e["l"]), "dist_c": dist(e["c"])}
    # week_open: the earliest bar whose model day is in [Monday 11-09, 11-10]: Monday's server 01:00 bar
    mon = [b for b in bars if b["t_server"].startswith("2026-11-09")][0]
    assert lv["week_open"] == {"t_utc": mon["t_utc"], "o": mon["o"], "dist": dist(mon["o"])}
    assert mon["t_utc"] == "2026-11-08T23:00:00Z"                 # EST: model 00:00 = 22:00 UTC the day before
    a = lv["asia"]
    today = [b for b in bars if b["t_server"].startswith("2026-11-10")]
    assert list(a) == ["from_utc", "to_utc", "n_hours", "complete", "h", "l", "dist_h", "dist_l", "width_ratio",
                       "hist"]
    assert (a["from_utc"], a["to_utc"], a["n_hours"], a["complete"]) == (
        "2026-11-09T23:00:00Z", "2026-11-10T04:00:00Z", 5, False)
    assert a["h"] == max(b["h"] for b in today) and a["l"] == min(b["l"] for b in today)
    assert (a["dist_h"], a["dist_l"]) == (dist(a["h"]), dist(a["l"]))
    # hist: the last 20 bars_d1 days, each over model hours 1 to k_end = 5 (the same hours as today's range)
    assert [x["day"] for x in a["hist"]] == [e["day"] for e in bars_d1[-20:]]
    for x in a["hist"]:
        bs = [b for b in bars if b["t_server"][:10] == x["day"] and 1 <= int(b["t_server"][11:13]) <= 5]
        assert x == {"day": x["day"], "h": max(b["h"] for b in bs), "l": min(b["l"] for b in bs)}
    m = statistics.median([x["h"] - x["l"] for x in a["hist"]])
    assert a["width_ratio"] == round((a["h"] - a["l"]) / m, 2) and a["width_ratio"] > 1.0
    c = [e["c"] for e in bars_d1]
    assert lv["ret_1d"] == round(100 * (c[-1] / c[-2] - 1), 3)
    assert lv["ret_5d"] == round(100 * (c[-1] / c[-6] - 1), 3)
    assert lv["ret_20d"] == round(100 * (c[-1] / c[-21] - 1), 3)
    rng = [e["h"] - e["l"] for e in bars_d1]
    assert lv["vol_ratio"] == round(statistics.fmean(rng[-5:]) / statistics.fmean(rng[-20:]), 2)


def test_complete_asia_and_hours_after_asia_are_ignored():
    bars, bars_d1 = make(today_hours=range(1, 13))
    a = levels.compute(D, bars, bars_d1, P, DREF)["asia"]
    assert (a["n_hours"], a["complete"], a["to_utc"]) == (9, True, "2026-11-10T08:00:00Z")
    nine = [b for b in bars if b["t_server"].startswith("2026-11-10") and int(b["t_server"][11:13]) <= 9]
    assert a["h"] == max(b["h"] for b in nine)
    assert all(len(x) == 3 for x in a["hist"])


def test_hist_leaves_out_days_without_those_hours_and_width_ratio_needs_ten():
    # 11 of the last 20 days have no bar in hours 1-5: 9 hist entries -> width_ratio null
    short = set(range(1, 12))
    bars, bars_d1 = make(short_days=short)
    a = levels.compute(D, bars, bars_d1, P, DREF)["asia"]
    assert len(a["hist"]) == 9 and a["width_ratio"] is None
    bars, bars_d1 = make(short_days=set(range(1, 11)))
    a = levels.compute(D, bars, bars_d1, P, DREF)["asia"]
    assert len(a["hist"]) == 10 and a["width_ratio"] is not None


def test_counts_and_nulls():
    bars, bars_d1 = make(n_days=19)
    lv = levels.compute(D, bars, bars_d1, P, DREF)
    assert lv["ret_20d"] is None and lv["vol_ratio"] is None and lv["ret_5d"] is not None
    bars, bars_d1 = make(n_days=20)
    lv = levels.compute(D, bars, bars_d1, P, DREF)
    assert lv["ret_20d"] is None and lv["vol_ratio"] is not None       # 20 closes: ret_20d needs 21
    bars, bars_d1 = make(n_days=21)
    assert levels.compute(D, bars, bars_d1, P, DREF)["ret_20d"] is not None
    bars, bars_d1 = make(n_days=5)
    lv = levels.compute(D, bars, bars_d1, P, DREF)
    assert lv["ret_5d"] is None and lv["ret_1d"] is not None and lv["asia"]["width_ratio"] is None
    # no history at all, and no bar today in Asia hours
    bars = day_bars(D, 0, range(10, 12))
    lv = levels.compute(D, bars, [], P, DREF)
    assert lv["prev_day"] is None and lv["asia"] is None
    assert (lv["ret_1d"], lv["ret_5d"], lv["ret_20d"], lv["vol_ratio"]) == (None, None, None, None)
    assert lv["week_open"]["t_utc"] == bars[0]["t_utc"]
    lv = levels.compute(D, [], [], P, DREF)
    assert lv == {"prev_day": None, "week_open": None, "asia": None, "ret_1d": None, "ret_5d": None,
                  "ret_20d": None, "vol_ratio": None}


def test_zero_ranges_give_null_ratios():
    bars, bars_d1 = make()
    flat = [dict(b, h=4000.0, l=4000.0, o=4000.0, c=4000.0) for b in bars]
    flat_d1 = [dict(e, h=4000.0, l=4000.0) for e in bars_d1]
    lv = levels.compute(D, flat, flat_d1, P, DREF)
    assert lv["vol_ratio"] is None and lv["asia"]["width_ratio"] is None


def test_week_open_on_a_monday_and_a_missing_monday():
    mon = dt.date(2026, 11, 9)
    bars = day_bars(clock.weekday_before(mon, 1), 1) + day_bars(mon, 0, range(1, 4))
    lv = levels.compute(mon, bars, [], P, DREF)
    assert lv["week_open"]["t_utc"] == bars[23]["t_utc"]            # Monday's first bar, not Friday's
    # Monday missing: the week's open is Tuesday's first bar
    bars, bars_d1 = make(skip={1})
    lv = levels.compute(D, bars, bars_d1, P, DREF)
    first_tue = [b for b in bars if b["t_server"].startswith("2026-11-10")][0]
    assert lv["week_open"]["o"] == first_tue["o"] and lv["prev_day"]["day"] == "2026-11-06"


def test_rounding_is_python_round_on_the_double():
    assert levels.rnd(0.125, 2) == round(0.125, 2) == 0.12
    assert levels.rnd(2.675, 2) == 2.67                            # 2.675 is 2.67499999... as a double
    bars, bars_d1 = make()
    lv = levels.compute(D, bars, bars_d1, 4000.0, 1.0)
    assert lv["prev_day"]["dist_c"] == round(bars_d1[-1]["c"] - 4000.0, 2)
    assert lv["prev_day"]["c"] == bars_d1[-1]["c"]                  # prices are copied unrounded
