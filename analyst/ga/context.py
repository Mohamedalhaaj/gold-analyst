"""The context builder (prereg R2 and §2; register R-AN-14..R-AN-23). FROZEN.

Nothing after the fetch time enters: hour h is admissible iff h + 1 h <= asof_utc − L_min, and the history comes from
the mirrored store at repo_head_sha. On a data or D_ref failure the context is returned with status
`no_call:data` / `no_call:d_ref` and a detail code; a partial context is never used for a plan.

Detail codes: store_stale (more than 60 listed hours), store_mismatch (a raw file differs from its row),
hour_absent (a required hour not present, or an optional hour invalid/error), no_price (no P_ref at all), and
D_ref's rows / stale / nonpositive. Fetching stops at the first absent hour (interpretation, logged for the register).
"""
import datetime as dt
import math

from . import clock, dref, hours, memory, rec, store, tables

SCHEMA = "analyst-context/1"
MAX_LISTED_HOURS = 60
HIST_WEEKDAYS = 10


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


def _tick_bar(h, ticks):
    if not ticks:
        return None
    mids = [hours.tick_mid("XAUUSD", a, b) for (_ms, a, b, _av, _bv) in ticks]
    return _bar(h, mids[0], max(mids), min(mids), mids[-1], len(mids), "tick")


def build(view, head_sha, mode, w, asof, params, fetcher, with_memory=None):
    """The context object for window `w` at `asof` (aware UTC, floored to the second)."""
    asof = rec.floor_s(asof)
    asof_s = rec.iso(asof)
    d = w.day
    L = int(params["L_min"])
    holidays = tables.load_holidays(view)
    events = tables.load_events(view)
    days_b = view.read(store.DAYS_PATH) or b""
    slots_b = view.read(store.SLOTS_PATH) or b""
    rows = store.parse_days(days_b.decode("utf-8"))
    complete = store.complete_utc(rows, asof)
    start_hist = clock.model_to_utc(dt.datetime.combine(clock.weekday_before(d, HIST_WEEKDAYS), dt.time(0)))
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
        "fx": None, "us2y": "unknown", "events": events.context_block(d), "d_ref": None, "memory": None,
        "status": "ok", "detail": None,
    }

    def fail(status, detail):
        ctx["status"], ctx["detail"] = status, detail
        return ctx

    if len(listed) > MAX_LISTED_HOURS:
        return fail("no_call:data", "store_stale")
    store_days = clock.days_range(start_hist.date(), (complete - dt.timedelta(days=1)).date()) \
        if complete > start_hist else []
    try:
        minutes, _used = store.load_days(view, store.final_status(rows, asof), store_days)
    except store.StoreMismatch:
        return fail("no_call:data", "store_mismatch")

    tick_files = {}
    for h in listed:
        prov, ticks = fetcher.fetch("XAUUSD", h)
        ctx["files"].append(prov)
        optional = holidays.nyse_full(clock.to_server(h).date())
        if prov["status"] == "present":
            tick_files[h] = ticks
        elif not (optional and prov["status"] == "empty"):
            return fail("no_call:data", "hour_absent")

    # hourly bars over [start_hist, h_new]: store before complete_utc, tick files after
    for h in clock.hours_between(start_hist, h_new):
        if not trading_hour(h):
            continue
        b = _store_bar(minutes, h) if h < complete else _tick_bar(h, tick_files.get(h))
        if b is not None:
            ctx["bars_h1"].append(b)
    hist_days = [clock.weekday_before(d, k) for k in range(HIST_WEEKDAYS, 0, -1)]
    for hd in hist_days:
        bs = [b for b in ctx["bars_h1"] if b["t_server"][:10] == hd.isoformat()]
        if bs:
            ctx["bars_d1"].append({"day": hd.isoformat(), "o": bs[0]["o"], "h": max(b["h"] for b in bs),
                                   "l": min(b["l"] for b in bs), "c": bs[-1]["c"], "n_hours": len(bs)})

    # P_ref (R-AN-18)
    asof_ms = rec.epoch_ms(asof)
    present = [h for h in listed if h in tick_files]
    if present:
        h = present[-1]
        ms, a, b, _av, _bv = tick_files[h][-1]
        p_ref, src, t_ms = hours.tick_mid("XAUUSD", a, b), "tick", rec.epoch_ms(h) + ms
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
    got = {}
    for which, hh in (("t0", t0h), ("t1", h_new)):
        for pair in hours.FX_PAIRS:
            prov, ticks = fetcher.fetch(pair, hh)
            ctx["files"].append(prov)
            got[(pair, which)] = ticks if prov["status"] == "present" else None
    pairs, ratios = [], []
    for pair in hours.FX_PAIRS:
        t0, t1 = got[(pair, "t0")], got[(pair, "t1")]
        p0 = hours.tick_mid(pair, t0[0][1], t0[0][2]) if t0 else None
        p1 = hours.tick_mid(pair, t1[-1][1], t1[-1][2]) if t1 else None
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

    # D_ref (R-AN-21)
    block, detail = dref.compute(slots_b.decode("utf-8"), w.slot, d, asof_s, p_ref)
    ctx["d_ref"] = block
    if with_memory is None:
        with_memory = mode == "live"
    if with_memory:
        ctx["memory"] = memory.build(view, d)
    if detail:
        return fail("no_call:d_ref", detail)
    return ctx


def context_sha256(ctx):
    return rec.compact_sha256(ctx)
