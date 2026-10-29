"""The context builder (prereg R2 and §2; register R-AN-14..R-AN-23, R-AN-87, R-AN-88; §16.18 A9, A12). FROZEN.

Nothing after the fetch time enters: hour h is admissible iff h + 1 h <= asof_utc − L_min, and the history comes from
the mirrored store at repo_head_sha. On a data or D_ref failure the context is returned with status
`no_call:data` / `no_call:d_ref` and a detail code; a partial context is never used for a plan.

Since §16.18 A9 (`analyst-context/2`) the model day's inputs come from Twelve Data (ga/td.py, R-AN-87): one 1h request
for the listed XAU hours, one for each pair of the dollar proxy (t0 to h_new), then the 1-minute `fresh` request that
measures freshness only and keeps its bar's time, never a price (review B1, decision A9-D15: that bar is newer than
every admissible hour, and the model reads the whole context). Listed-hour bars have `src` "td" and n null; P_ref is
the c of the newest listed hour's bar, timed at that hour's end; p0 is the o of the pair's t0 bar and p1 the c of
its h_new bar. Levels are shown as Twelve Data gives them, with no offset, and the fixed `sources` note names both
sources. Past days still come from the store.

Since §16.18 A12 (`analyst-context/3`): `risk`, right after `fx`, is AUD/JPY from one more 1h request after USDCHF
and before `fresh` ({pair, p0 = the o of its t0 bar, p1 = the c of its h_new bar, pct = 100·(p1/p0 − 1), t0_hour_utc,
t1_hour_utc}); it is null when the request was not made, is not `ok`, or lacks either bar, never changes `status` and
does not enter proxy_pct. The store days now run from start_lv = model 00:00 of the 25th weekday before d, so that
`bars_d1` covers 25 history weekdays; the hours in [start_lv, start_hist) feed bars_d1 and `levels` only, never
bars_h1. `levels` (ga/levels.py, R-AN-88), right after `d_ref`, is computed after D_ref from these bars, and is null
unless `status` is `ok`.

Since §16.18 A14 (`analyst-context/4`, every window): `wake`, right after `levels`, is ga/wake.context_block of the
attempt record's `wake` (kind, reason, check_utc; for a trigger also `values`, `r20_days` and `bars_m30` from its
recorded 30-minute bars); D_ref's rows come from `mirror/system/forecasts.csv` (every slot), whose sha256 is
`store.slots_sha256`; the `sources` note gains one sentence on the 30-minute bars.

Since §16.18 A15 (`analyst-context/5`, every window): `review`, right after `wake`, is ga/review.block of the open set
`context` read before its first request (R-AN-92; null when the set is empty or the model day is before a15_from).
When it is not empty and some open trade's entry bar has closed by asof, one more request, `XAUUSD_M30R` (30-minute
XAU/USD bars from the earliest open entry to asof floored to the half hour; its `bars` hold the elements with
A <= t_utc and t_utc + 30 min <= asof_utc), is the context's last request, after `fresh`; it never changes `status`,
and a context that stops at XAUUSD does not make it. In canary mode a context of a model day >= a15_from has a
`memory` built from the canary records (R-AN-95); `settled` and `pending` entries carry `side` after `action`. The
`sources` note gains one sentence on the review prices.

Detail codes: store_stale (more than 60 listed hours), store_mismatch (a raw file differs from its row), td_<reason>
(the XAUUSD request not `ok` while a listed hour is required: td_key, td_plan, td_credits, td_server, td_invalid,
td_other, td_budget), hour_absent (XAUUSD `ok` without a bar at a required listed hour), no_price (no P_ref at all),
and D_ref's rows / stale / nonpositive. Each of these data stops ends the requests: no later request is made.
Interpretations (logged for the register): `fresh` is null when its fetched_utc is unknown (no session clock); with t0
= h_new a pair keeps its one bar for both p0 and p1.
"""
import datetime as dt
import math

from . import clock, dref, hours, levels, memory, paths, rec, store, tables, td
from . import review as review_rule
from . import wake as wake_rule

SCHEMA = "analyst-context/5"
FORECASTS_PATH = "mirror/system/forecasts.csv"
WAKE_NOTE = " wake values and wake.bars_m30 come from Twelve Data 30-minute bars in UTC, labelled by their start."
REVIEW_NOTE = (" review prices come from Twelve Data 30-minute bars in UTC and are indicative; settlement uses "
               "Dukascopy minutes, where an open trade may already have reached its stop or target.")
SOURCES = {"history": "dukascopy_store", "today": "twelvedata_1h",
           "note": "bars_h1 entries with src td, p_ref when p_ref_src is td, and fx come from Twelve Data hourly bars "
                   "in UTC; bars_h1 entries with src store are Dukascopy mid; bars_d1 cover the 25 weekdays before "
                   "the model day and are built from hourly bars made as bars_h1's are, but bars_h1 lists only the "
                   "last 10 of those days, so the older bars_d1 days come from Dukascopy store hours not shown in "
                   "bars_h1; Twelve Data and Dukascopy levels can differ slightly; settlement uses Dukascopy."
                   + WAKE_NOTE + REVIEW_NOTE}
MAX_LISTED_HOURS = 60
HIST_WEEKDAYS = 10
LV_WEEKDAYS = 25
RISK_PAIR = "AUDJPY"


def trading_hour(h):
    """R-AN-15: model start on a model weekday with model hour >= 1."""
    m = clock.to_server(h)
    return m.weekday() < 5 and m.hour >= 1


def _bar(h, o, hi, lo, c, n, src):
    return {"t_utc": rec.iso(h), "t_server": rec.server_str(clock.to_server(h)), "o": o, "h": hi, "l": lo, "c": c,
            "n": n, "src": src}


def _store_bar(minutes, h):
    t0 = int(h.timestamp())
    mids = [minutes.mid(t) for t in range(t0, t0 + 3600, 60)]
    mids = [m for m in mids if m is not None and all(math.isfinite(x) for x in m)]
    if not mids:
        return None
    return _bar(h, mids[0][0], max(m[1] for m in mids), min(m[2] for m in mids), mids[-1][3], len(mids), "store")


def _td_bar(h, v):
    if v is None:
        return None
    _t, o, hi, lo, c = v
    return _bar(h, o, hi, lo, c, None, "td")


def _by_time(values):
    return {v[0]: v for v in values}


def build(view, head_sha, mode, w, asof, params, fetcher, with_memory=None, wake=None, review_open=None):
    """The context object for window `w` at `asof` (aware UTC, floored to the second); `fetcher` is a td.Fetcher;
    `wake` is the attempt record's `wake` (R-AN-90); `review_open` is O(R), the open trades' review.open_sets
    entries, which the session read before this call (R-AN-92; None or empty: no review)."""
    asof = rec.floor_s(asof)
    asof_s = rec.iso(asof)
    d = w.day
    L = int(params["L_min"])
    holidays = tables.load_holidays(view)
    events = tables.load_events(view)
    days_b = view.read(store.DAYS_PATH) or b""
    slots_b = view.read(FORECASTS_PATH) or b""
    rows = store.parse_days(days_b.decode("utf-8"))
    complete = store.complete_utc(rows, asof)
    start_hist = clock.model_to_utc(dt.datetime.combine(clock.weekday_before(d, HIST_WEEKDAYS), dt.time(0)))
    start_lv = clock.model_to_utc(dt.datetime.combine(clock.weekday_before(d, LV_WEEKDAYS), dt.time(0)))
    h_new = clock.floor_hour(asof - dt.timedelta(minutes=L)) - clock.HOUR
    first_listed = max(complete, start_hist)
    listed = [h for h in clock.hours_between(first_listed, h_new) if trading_hour(h)] if first_listed <= h_new else []
    ctx = {
        "schema": SCHEMA, "call_id": w.call_id, "mode": mode, "window": w.name, "model_day": d.isoformat(),
        "entry_utc": rec.iso(w.entry_utc), "exit_utc": rec.iso(w.exit_utc), "deadline_utc": rec.iso(w.deadline_utc),
        "entry_server": rec.server_str(w.entry_server), "exit_server": rec.server_str(w.exit_server),
        "asof_utc": asof_s, "repo_head_sha": head_sha,
        "params": {"L_min": L, "T_run_min": int(params["T_run_min"]), "provisional": bool(params["provisional"])},
        "store": {"complete_utc": rec.iso(complete), "start_hist_utc": rec.iso(start_hist),
                  "days_csv_sha256": rec.sha256_bytes(days_b), "slots_sha256": rec.sha256_bytes(slots_b)},
        "files": [], "bars_h1": [], "bars_d1": [], "p_ref": None,
        "fx": None, "risk": None, "us2y": "unknown", "fresh": None, "sources": dict(SOURCES),
        "events": events.context_block(d), "d_ref": None, "levels": None,
        "wake": wake_rule.context_block(wake) if wake else None,
        "review": review_rule.block(review_open, w.entry_utc, []) if review_open else None, "memory": None,
        "status": "ok", "detail": None,
    }

    def fail(status, detail):
        ctx["status"], ctx["detail"] = status, detail
        return ctx

    if len(listed) > MAX_LISTED_HOURS:
        return fail("no_call:data", "store_stale")
    store_days = clock.days_range(start_lv.date(), (complete - dt.timedelta(days=1)).date()) \
        if complete > start_lv else []
    try:
        minutes, _used = store.load_days(view, store.final_status(rows, asof), store_days)
    except store.StoreMismatch:
        return fail("no_call:data", "store_mismatch")

    # the listed XAU hours: one Twelve Data request (R-AN-87)
    td_hours = {}
    if listed:
        prov, values = fetcher.fetch("XAUUSD", td.series_url("XAUUSD", listed[0], listed[-1] + clock.HOUR), "1h")
        ctx["files"].append(prov)
        required = [h for h in listed if not holidays.nyse_full(clock.to_server(h).date())]
        if prov["status"] != "ok":
            if required:
                return fail("no_call:data", "td_" + prov["reason"])
        else:
            got = _by_time(values)
            td_hours = {h: got[h] for h in listed if h in got}
            prov["bars"] = [td.bar_obj(td_hours[h]) for h in listed if h in td_hours]
            if any(h not in td_hours for h in required):
                return fail("no_call:data", "hour_absent")

    # hourly bars over [start_lv, h_new]: store before complete_utc, Twelve Data after; bars_h1 from start_hist on
    all_bars = []
    for h in clock.hours_between(start_lv, h_new):
        if not trading_hour(h):
            continue
        b = _store_bar(minutes, h) if h < complete else _td_bar(h, td_hours.get(h))
        if b is not None:
            all_bars.append(b)
            if h >= start_hist:
                ctx["bars_h1"].append(b)
    by_day = {}
    for b in all_bars:
        by_day.setdefault(b["t_server"][:10], []).append(b)
    hist_days = [clock.weekday_before(d, k) for k in range(LV_WEEKDAYS, 0, -1)]
    for hd in hist_days:
        bs = by_day.get(hd.isoformat())
        if bs:
            ctx["bars_d1"].append({"day": hd.isoformat(), "o": bs[0]["o"], "h": max(b["h"] for b in bs),
                                   "l": min(b["l"] for b in bs), "c": bs[-1]["c"], "n_hours": len(bs)})

    # P_ref (R-AN-18)
    asof_ms = rec.epoch_ms(asof)
    present = [h for h in listed if h in td_hours]
    if present:
        h = present[-1]
        p_ref, src, t_ms = td_hours[h][4], "td", rec.epoch_ms(h) + hours.HOUR_MS
    else:
        last = None
        lim = int(complete.timestamp())
        for t in sorted(set(minutes.side["BID"]) & set(minutes.side["ASK"])):
            if t < lim:
                m = minutes.mid(t)
                if m is not None and math.isfinite(m[3]):
                    last = (t, m[3])
        if last is None:
            return fail("no_call:data", "no_price")
        p_ref, src, t_ms = last[1], "store", last[0] * 1000
    ctx["p_ref"] = {"p_ref": p_ref, "p_ref_src": src, "p_ref_utc_ms": rec.iso_ms(
        dt.datetime.fromtimestamp(t_ms / 1000, tz=rec.UTC)), "p_ref_age_ms": int(asof_ms - t_ms)}

    # FX and the dollar proxy (R-AN-19)
    t0h = clock.model_to_utc(dt.datetime.combine(d, dt.time(0)))
    pairs, ratios = [], []
    for pair in hours.FX_PAIRS:
        prov, values = fetcher.fetch(pair, td.series_url(pair, t0h, h_new + clock.HOUR), "1h")
        ctx["files"].append(prov)
        got = _by_time(values) if prov["status"] == "ok" else {}
        prov["bars"] = [td.bar_obj(got[h]) for h in sorted({t0h, h_new}) if h in got]
        p0 = got[t0h][1] if t0h in got else None
        p1 = got[h_new][4] if h_new in got else None
        pct = 100.0 * (p1 / p0 - 1.0) if (p0 is not None and p1 is not None) else None
        pairs.append({"pair": pair, "p0": p0 if (p0 is not None and p1 is not None) else None,
                      "p1": p1 if (p0 is not None and p1 is not None) else None, "pair_pct": pct,
                      "t0_hour_utc": rec.iso(t0h), "t1_hour_utc": rec.iso(h_new)})
        ratios.append(p1 / p0 if pct is not None else None)
    proxy = None
    if all(r is not None for r in ratios):
        prod = 1.0
        for r, e in zip(ratios, hours.FX_EXPONENTS):
            prod *= r ** e
        proxy = 100.0 * (prod - 1.0)
    ctx["fx"] = {"pairs": pairs, "proxy_pct": proxy, "exponents": list(hours.FX_EXPONENTS)}

    # the risk proxy (R-AN-19, R-AN-87, §16.18 A12): a failure makes `risk` null, never a change of status
    prov, values = fetcher.fetch(RISK_PAIR, td.series_url(RISK_PAIR, t0h, h_new + clock.HOUR), "1h")
    ctx["files"].append(prov)
    got = _by_time(values) if prov["status"] == "ok" else {}
    prov["bars"] = [td.bar_obj(got[h]) for h in sorted({t0h, h_new}) if h in got]
    if t0h in got and h_new in got:
        p0, p1 = got[t0h][1], got[h_new][4]
        ctx["risk"] = {"pair": RISK_PAIR, "p0": p0, "p1": p1, "pct": 100.0 * (p1 / p0 - 1.0),
                       "t0_hour_utc": rec.iso(t0h), "t1_hour_utc": rec.iso(h_new)}

    # fresh (R-AN-87): measured only, never a change of status; its time only, no price (decision A9-D15)
    prov, values = fetcher.fetch("fresh", td.FRESH_URL, "1min")
    ctx["files"].append(prov)
    if prov["status"] == "ok" and values:
        newest = max(values, key=lambda v: v[0])
        prov["bars"] = [{"t_utc": rec.iso(newest[0])}]
        if prov["fetched_utc"] is not None:
            lag = rec.parse_iso(prov["fetched_utc"]) - (newest[0] + dt.timedelta(seconds=60))
            ctx["fresh"] = {"bar_utc": rec.iso(newest[0]), "lag_s": int(lag.total_seconds())}

    # the review's prices (R-AN-92, §16.18 A15): the last request; it never changes the status
    if review_open and review_rule.needs_request(review_open, asof):
        a, b = review_rule.request_range(review_open, asof)
        prov, values = fetcher.fetch(review_rule.REQ, td.m30_url(a, b), "30min")
        ctx["files"].append(prov)
        bars = review_rule.used_bars(values, a, asof) if prov["status"] == "ok" else []
        prov["bars"] = [td.bar_obj(v) for v in bars]
        ctx["review"] = review_rule.block(review_open, w.entry_utc, bars)

    # D_ref (R-AN-21)
    block, detail = dref.compute(slots_b.decode("utf-8"), w.slot, d, asof_s, p_ref)
    ctx["d_ref"] = block
    if with_memory is None:
        with_memory = mode == "live" or (mode == "canary" and paths.a15_applies(paths.read_mode(view), d))
    if with_memory:
        ctx["memory"] = memory.build(view, d, mode)
    if detail:
        return fail("no_call:d_ref", detail)
    # levels (R-AN-88): from the context's own bars, after D_ref, only when the status is ok
    ctx["levels"] = levels.compute(d, all_bars, ctx["bars_d1"], p_ref, block["D_ref"])
    return ctx


def context_sha256(ctx):
    return rec.compact_sha256(ctx)
