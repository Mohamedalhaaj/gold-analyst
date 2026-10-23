"""Settlement, the opposite side and the drift twins (prereg §4; register R-AN-40..R-AN-47). FROZEN.

The path rules are lab v2's (gold-forward-watch analysis/system/lab_v2/v2_exec.py, R-V2-17) on integer price points,
with the plan's target added and the stop distance S from the call:
- P_e = the entry minute's open on the entry side (ask for a long, bid for a short), else the close of the latest
  earlier minute of the model day with that side's row, else `no_quote`. X = P_e − s·S, Tg = P_e + s·T.
- For each path minute in order: if quoted on the exit side (bid for a long, ask for a short): open through X ->
  exit at the open (stop, open); else low/high through X -> X (stop, level); else high/low through Tg -> Tg (target,
  level; a target gap fills at Tg, never better). Then run = 0 if the minute is live (both rows, both volumes > 0),
  else run + 1; at 15 the trade exits at X (gap, level). The stop wins a tie with the target, and both are tested
  before the run; quoted zero-volume minutes are tested.
- Time exit: the exit side's close of t1 − 1 if quoted, else of the latest quoted path minute (time, close); none ->
  X (gap, level).
- net$ = s·(P_x − P_e) − 0.10; stress = net$ − 0.25; R = net$/S; R_stress = stress/S; y = clip(R_stress, −2, 2);
  cost$ = 0.10 + half spreads at the entry and exit quotes; gross$ = net$ + cost$.
- void (both sides of UTC day d missing) and no_quote count with y = R = R_stress = −1, net$ = stress = −S, cost 0.
"""
import datetime as dt

from . import clock, paths, rec, store

SETTLED_SCHEMA = "analyst-settled/1"
TWIN_SCHEMA = "analyst-twin/1"
ALLOWANCE = 0.10
STRESS_ADD = 0.25
GAP_MINUTES = 15
CLIP = 2.0
STOP_FIRST = True


def _row(m, side, t):
    return m.bid(t) if side == "BID" else m.ask(t)


def _live(m, t):
    b, a = m.bid(t), m.ask(t)
    return b is not None and a is not None and b[4] > 0 and a[4] > 0


def _half_spread(m, t, field):
    """Half the spread ($) at the open (field 0) or close (field 3) of minute t; 0 without both sides."""
    b, a = m.bid(t), m.ask(t)
    if b is None or a is None:
        return 0.0
    return (a[field] - b[field]) / 2000


def full_stop(S, status):
    return {"status": status, "P_e": None, "P_e_src": None, "P_e_minute_utc": None, "X": None, "Tg": None,
            "exit_minute_utc": None, "P_x": None, "reason": None, "fill": None, "cost_usd": 0.0, "gross_usd": None,
            "net_usd": -S, "stress_net_usd": -S, "R": -1.0, "R_stress": -1.0, "y": -1.0}


def trade(m, w, side, S_dimes, T_dimes):
    """Settle one side. m: store.Minutes with UTC days d−1 and d."""
    S = S_dimes / 10
    S_pts, T_pts = 100 * S_dimes, 100 * T_dimes
    t0, t1 = int(w.entry_utc.timestamp()), int(w.exit_utc.timestamp())
    day0 = int(clock.model_to_utc(dt.datetime.combine(w.day, dt.time(0))).timestamp())
    ent, ext = ("ASK", "BID") if side > 0 else ("BID", "ASK")
    r = _row(m, ent, t0)
    if r is not None:
        pe, src, e_min, e_field = r[0], "open", t0, 0
    else:
        pe = None
        t = t0 - 60
        while t >= day0:
            r = _row(m, ent, t)
            if r is not None:
                pe, src, e_min, e_field = r[3], "fallback_close", t, 3
                break
            t -= 60
        if pe is None:
            return full_stop(S, "no_quote"), None
    X, Tg = pe - side * S_pts, pe + side * T_pts
    run = 0
    exit_px = reason = fill = exit_t = None
    x_field = 3
    for t in range(t0, t1, 60):
        q = _row(m, ext, t)
        if q is not None:
            o, h, lo, _c = q[0], q[1], q[2], q[3]
            if side > 0:
                if o <= X:
                    exit_px, reason, fill, exit_t, x_field = o, "stop", "open", t, 0
                elif lo <= X:
                    exit_px, reason, fill, exit_t = X, "stop", "level", t
                elif h >= Tg:
                    exit_px, reason, fill, exit_t = Tg, "target", "level", t
            else:
                if o >= X:
                    exit_px, reason, fill, exit_t, x_field = o, "stop", "open", t, 0
                elif h >= X:
                    exit_px, reason, fill, exit_t = X, "stop", "level", t
                elif lo <= Tg:
                    exit_px, reason, fill, exit_t = Tg, "target", "level", t
            if exit_px is not None:
                break
        run = 0 if _live(m, t) else run + 1
        if run >= GAP_MINUTES:
            exit_px, reason, fill, exit_t = X, "gap", "level", t
            break
    if exit_px is None:
        last = None
        for t in range(t1 - 60, t0 - 60, -60):
            if _row(m, ext, t) is not None:
                last = t
                break
        if last is None:
            exit_px, reason, fill, exit_t = X, "gap", "level", t1 - 60
        else:
            exit_px, reason, fill, exit_t = _row(m, ext, last)[3], "time", "close", last
    net = side * (exit_px - pe) / 1000 - ALLOWANCE
    stress = net - STRESS_ADD
    cost = ALLOWANCE + _half_spread(m, e_min, e_field) + _half_spread(m, exit_t, x_field)
    R, Rs = net / S, stress / S
    out = {"status": "final", "P_e": pe / 1000, "P_e_src": src, "P_e_minute_utc": rec.iso(_ts(e_min)),
           "X": X / 1000, "Tg": Tg / 1000, "exit_minute_utc": rec.iso(_ts(exit_t)), "P_x": exit_px / 1000,
           "reason": reason, "fill": fill, "cost_usd": cost, "gross_usd": net + cost, "net_usd": net,
           "stress_net_usd": stress, "R": R, "R_stress": Rs, "y": max(-CLIP, min(CLIP, Rs))}
    return out, {"pe": pe, "exit_t": exit_t}


def _ts(t):
    return dt.datetime.fromtimestamp(t, tz=rec.UTC)


def path_summary(m, w, side, outcome, aux):
    t0, t1 = int(w.entry_utc.timestamp()), int(w.exit_utc.timestamp())
    buckets = []
    for k in range(16):
        mids = [m.mid(t) for t in range(t0 + 900 * k, t0 + 900 * (k + 1), 60)]
        mids = [x for x in mids if x is not None]
        buckets.append({"k": k, "o": mids[0][0] if mids else None, "h": max(x[1] for x in mids) if mids else None,
                        "l": min(x[2] for x in mids) if mids else None, "c": mids[-1][3] if mids else None,
                        "n": len(mids)})
    allm = [m.mid(t) for t in range(t0, t1, 60)]
    allm = [x for x in allm if x is not None]
    rng = (max(x[1] for x in allm) - min(x[2] for x in allm)) if allm else None
    mfe = mae = after = None
    if outcome["status"] == "final" and aux:
        pe, xt = aux["pe"], aux["exit_t"]
        ext = "BID" if side > 0 else "ASK"
        rows = [_row(m, ext, t) for t in range(t0, xt + 60, 60)]
        rows = [r for r in rows if r is not None]
        if rows:
            if side > 0:
                mfe, mae = (max(r[1] for r in rows) - pe) / 1000, (min(r[2] for r in rows) - pe) / 1000
            else:
                mfe, mae = (pe - min(r[2] for r in rows)) / 1000, (pe - max(r[1] for r in rows)) / 1000
        after = (xt - t0) // 60
    return {"buckets": buckets, "mfe_usd": mfe, "mae_usd": mae, "exit_after_min": after, "range_mid_usd": rng}


def twin(m, w):
    """(status, mid_entry, mid_entry_src, mid_exit, mid_exit_src, d_W)."""
    t0, t1 = int(w.entry_utc.timestamp()), int(w.exit_utc.timestamp())
    day0 = int(clock.model_to_utc(dt.datetime.combine(w.day, dt.time(0))).timestamp())

    def both(t):
        b, a = m.bid(t), m.ask(t)
        return (b, a) if b is not None and a is not None else None

    e = both(t0)
    if e:
        e_sum, e_src, e_mid = e[0][0] + e[1][0], "open", m.mid(t0)[0]
    else:
        e_sum = None
        for t in range(t0 - 60, day0 - 60, -60):
            x = both(t)
            if x:
                e_sum, e_src, e_mid = x[0][3] + x[1][3], "fallback_close", m.mid(t)[3]
                break
    x_sum = None
    for t in range(t1 - 60, t0 - 60, -60):
        x = both(t)
        if x:
            x_sum, x_src, x_mid = x[0][3] + x[1][3], ("close" if t == t1 - 60 else "fallback_close"), m.mid(t)[3]
            break
    if e_sum is None or x_sum is None:
        return "no_quote", None, None, None, None, None
    return "final", e_mid, e_src, x_mid, x_src, (x_sum - e_sum) / 2000


def day_inputs(rows_by_key, w):
    d = w.day
    return [d - dt.timedelta(days=1), d], [
        {"day": x.isoformat(), "side": s, "status": (rows_by_key.get((x.isoformat(), s)) or {}).get("status"),
         "sha256": (rows_by_key.get((x.isoformat(), s)) or {}).get("sha256", "")}
        for x in (d - dt.timedelta(days=1), d) for s in store.SIDES]


def is_void(rows_by_key, w):
    k = w.day.isoformat()
    return all((rows_by_key.get((k, s)) or {}).get("status") == "missing" for s in store.SIDES)


def processable(rows_by_key, view, w, now):
    if now < w.exit_utc:
        return False
    for x in (w.day - dt.timedelta(days=1), w.day):
        for s in store.SIDES:
            r = rows_by_key.get((x.isoformat(), s))
            if r is None or r["status"] not in ("ok", "missing"):
                return False
            if r["status"] == "ok":
                b = view.read(store.raw_path(x, s))
                if b is None or rec.sha256_bytes(b) != r["sha256"]:
                    raise store.StoreMismatch(f"{x} {s}")
    return True


def settled_record(view, w, call, terminal, cls, m, rows_by_key, now, run_id, head_sha):
    plan_ = call["plan"]
    side = call["side"]
    void = is_void(rows_by_key, w)
    if void:
        out, aux = full_stop(call["S"], "void"), None
        opp, oaux = full_stop(call["S"], "void"), None
    else:
        out, aux = trade(m, w, side, call["S_dimes"], call["T_dimes"])
        opp, oaux = trade(m, w, -side, call["S_dimes"], call["T_dimes"])
    _days, inputs = day_inputs(rows_by_key, w)
    days_b = view.read(store.DAYS_PATH) or b""
    return {
        "schema": SETTLED_SCHEMA, "call_id": w.call_id, "mode": "live", "window": w.name,
        "model_day": w.day.isoformat(), "class": cls, "action": plan_["action"], "side": side,
        "stop_mult": call["stop_mult"], "target_mult": call["target_mult"], "D_ref": call["d_ref"]["D_ref"],
        "S": call["S"], "T": call["T"], "S_dimes": call["S_dimes"], "T_dimes": call["T_dimes"],
        "entry_utc": rec.iso(w.entry_utc), "exit_utc": rec.iso(w.exit_utc), "outcome": out, "opposite": opp,
        "path_summary": path_summary(m, w, side, out, aux) if not void else None,
        "call": {"action": plan_["action"], "stop_mult": plan_.get("stop_mult"),
                 "target_mult": plan_.get("target_mult"), "confidence": plan_["confidence"],
                 "reason_ar": plan_["reason_ar"], "key_risks_ar": plan_["key_risks_ar"]},
        "context": call["context"], "inputs": {"days": inputs, "days_csv_sha256": rec.sha256_bytes(days_b)},
        "call_sha256": terminal["call"]["sha256"],
        "terminal_sha256": rec.sha256_bytes(view.read(paths.terminal_path("live", w.call_id))),
        "context_sha256": call["context_sha256"], "settled_utc": rec.iso(now), "run_id": run_id,
        "head_sha": head_sha}


def twin_record(view, w, terminal, m, rows_by_key, now, run_id, head_sha):
    if is_void(rows_by_key, w):
        st, me, mes, mx, mxs, dW = "void", None, None, None, None, None
    else:
        st, me, mes, mx, mxs, dW = twin(m, w)
    return {"schema": TWIN_SCHEMA, "call_id": w.call_id, "mode": "live", "window": w.name,
            "model_day": w.day.isoformat(), "terminal_status": terminal["status"], "entry_utc": rec.iso(w.entry_utc),
            "exit_utc": rec.iso(w.exit_utc), "mid_entry": me, "mid_entry_src": mes, "mid_exit": mx,
            "mid_exit_src": mxs, "d_W": dW, "status": st, "settled_utc": rec.iso(now), "run_id": run_id,
            "head_sha": head_sha}
