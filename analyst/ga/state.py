"""The test, the states and the derived ledger (prereg §6, §1 PAUSED; register R-AN-51..R-AN-58, R-AN-62). FROZEN.

E_n = eproc.e_value(y) from the vendored, byte-identical lab e-process; K = eproc.k_value over post-promotion trades;
E* = 400 exactly (α_A = alpha_version(3) = 0.0025). Promotion needs P1-P5 and P7; demotion D1-D3; retirement n150, age,
demoted (>= 5 weekdays after the demotion) or violation. Events are create-only latches.
"""
import csv
import datetime as dt
import io

from . import clock, paths, rec

E_STAR = 400.0
ALPHA = 0.0025
MODEL = "claude-opus-5-5"
EVENT_SCHEMA = "analyst-state-event/1"
STATE_SCHEMA = "analyst-state/1"
LEDGER_COLS = ["call_id", "model_day", "window", "class", "terminal_status", "status", "detail", "action", "side",
               "stop_mult", "target_mult", "confidence", "D_ref", "S", "T", "lock_utc", "attempts", "entry_utc",
               "exit_utc", "P_e", "X", "Tg", "exit_minute_utc", "P_x", "reason", "fill", "gross_usd", "cost_usd",
               "net_usd", "stress_net_usd", "R", "R_stress", "y", "trade_no", "E", "K", "d_W", "twin_status",
               "opp_net_usd", "opp_R", "settled_utc", "terminal_sha256", "call_sha256", "settled_sha256"]


def _eproc():
    import eproc       # analyst/vendor/eproc.py (on sys.path via ga/__init__)
    return eproc


def _windows(view, prefix):
    out = []
    for p in view.list(prefix):
        if p.endswith(".json"):
            cid = p[len(prefix):-5]
            try:
                out.append((clock.window_key(cid), cid, p))
            except ValueError:
                pass
    return sorted(out)


def model_overlays(view):
    """{call_id: run_id} for live called/stay_out windows whose matching run is on another model (R-AN-57), and the
    set of call_ids without a matching run."""
    runs = []
    for p in view.list("analyst/runs/"):
        doc = view.read_json(p) or {}
        runs.extend(doc.get("runs") or [])
    over, unmatched = {}, set()
    for (_k, cid, p) in _windows(view, "analyst/terminal/"):
        t = view.read_json(p)
        if t.get("status") not in ("called", "stay_out"):
            continue
        call = view.read_json(paths.call_path("live", cid)) or {}
        sid = (call.get("self_reported") or {}).get("session_id")
        routine = "analyst-w1" if cid.endswith("W1") else "analyst-w2"
        cand = [r for r in runs if r.get("routine") == routine]
        match = [r for r in cand if sid and r.get("session_id") == sid]
        if not match and not any(r.get("session_id") for r in cand):
            w = clock.window_of(cid)
            lo = rec.iso(w.entry_utc - dt.timedelta(minutes=60))
            hi = rec.iso(w.deadline_utc)
            match = [r for r in cand if lo <= (r.get("created_utc") or "") <= hi]
        if not match:
            unmatched.add(cid)
            continue
        bad = [r for r in match if r.get("model") != MODEL]
        if bad:
            over[cid] = bad[0].get("run_id")
    return over, unmatched


def cleared_subjects(view):
    b = view.read("analyst/ADJUDICATIONS.csv")
    if not b:
        return set()
    return {r["subject"] for r in csv.DictReader(io.StringIO(b.decode("utf-8")))
            if r.get("ref") == "pause" and r.get("decision") == "clear"}


def events(view):
    out = []
    for p in view.list("analyst/state/"):
        if p.endswith(".json"):
            out.append(view.read_json(p))
    return sorted(out, key=lambda e: e["seq"])


def collect(view, upto_day, overlays):
    """Rows for the live terminal windows with model day <= upto_day, in window order."""
    start = view.read_json(paths.START_PATH)
    first_day = dt.date.fromisoformat(start["first_day"]) if start else None
    rows = []
    for (k, cid, p) in _windows(view, "analyst/terminal/"):
        if k[0] > upto_day:
            continue
        t = view.read_json(p)
        call = view.read_json(paths.call_path("live", cid)) if t.get("call") else None
        s = view.read_json(paths.settled_path(cid))
        tw = view.read_json(paths.twin_path(cid))
        cls = "counted" if (first_day and k[0] >= first_day) else "shakedown"
        rows.append({"cid": cid, "day": k[0], "W": k[1], "terminal": t, "call": call, "settled": s, "twin": tw,
                     "class": cls, "overlay": cid in overlays,
                     "terminal_sha256": rec.sha256_bytes(view.read(p)),
                     "call_sha256": (t.get("call") or {}).get("sha256"),
                     "settled_sha256": rec.sha256_bytes(view.read(paths.settled_path(cid))) if s else None})
    return rows, first_day


def counted(rows):
    return [r for r in rows if r["terminal"]["status"] == "called" and r["class"] == "counted"
            and not r["overlay"] and r["settled"] is not None]


def promotion(rows, first_day, e, verdict):
    """(checks dict, all) at model day e."""
    ct = counted([r for r in rows if r["day"] <= e])
    n = len(ct)
    y = [r["settled"]["outcome"]["y"] for r in ct]
    E = _eproc().e_value(y)
    out = {"n": n, "E": E}
    out["P1"] = E >= E_STAR
    out["P2"] = n >= 60 and (ct[-1]["day"] - ct[0]["day"]).days >= 56 if n else False
    st = [r["settled"]["outcome"]["stress_net_usd"] for r in ct]
    cost = [r["settled"]["outcome"]["cost_usd"] for r in ct]
    out["P3"] = bool(n) and sum(st) > 0 and sum(s - 0.5 * c for s, c in zip(st, cost)) > 0
    mu = {}
    for W in ("W1", "W2"):
        tw = [r["twin"]["d_W"] for r in rows if r["W"] == W and r["twin"] and r["twin"]["status"] == "final"
              and first_day is not None and first_day <= r["day"] <= e]
        mu[W] = sum(tw) / len(tw) if tw else None
    if n and all(mu[r["W"]] is not None for r in ct):
        out["P4"] = sum(r["settled"]["outcome"]["net_usd"] - r["settled"]["side"] * mu[r["W"]] for r in ct) > 0
    else:
        out["P4"] = False
    R = [r["settled"]["outcome"]["R"] for r in ct]
    out["P5"] = bool(n) and sum(R) - max(R) > 0
    out["P7"] = verdict is not None and verdict.get("analyst_verdict") in ("SOUND", "WARNING")
    out["mu"] = mu
    out["all"] = all(out[k] for k in ("P1", "P2", "P3", "P4", "P5", "P7"))
    return out


def run_state(view, processed_days, now, run_id, head_sha, verdict, write):
    """Evaluate the state after each processed model day (R-AN-53..R-AN-56). `write(path, bytes)` writes a file.
    Returns the STATE.json object."""
    overlays, _unmatched = model_overlays(view)
    evs = events(view)
    seq = max([0] + [e["seq"] for e in evs])
    cleared = cleared_subjects(view)
    state = "SHAKEDOWN"
    promoted_day = demoted_day = None
    pauses = set()
    for e in evs:
        ev = e["event"]
        if ev in ("TESTING", "PROMOTED", "DEMOTED", "RETIRED"):
            state = ev
            if ev == "PROMOTED":
                promoted_day = dt.date.fromisoformat(e["model_day"])
            if ev == "DEMOTED":
                demoted_day = dt.date.fromisoformat(e["model_day"])
        elif ev == "PAUSED":
            pauses.add(e["reason"])
        elif ev == "RESUMED":
            pauses.discard(e["reason"].replace("clear:", "", 1))
    upto = max(processed_days) if processed_days else now.date()
    rows, first_day = collect(view, upto, overlays)

    def emit(event, reason, day, n, E, K):
        nonlocal seq
        seq += 1
        doc = {"schema": EVENT_SCHEMA, "seq": seq, "event": event, "reason": reason,
               "model_day": day.isoformat() if day else None, "n": n, "E": E, "K": K, "utc": rec.iso(now),
               "run_id": run_id, "head_sha": head_sha}
        write(paths.state_event_path(seq), rec.record_bytes(doc))
        evs.append(doc)

    # pauses from the model overlay and the judge
    for cid, run in sorted(overlays.items()):
        subj = f"model:{run}"
        if subj not in pauses and subj not in cleared and not any(e["reason"] == subj for e in evs):
            emit("PAUSED", subj, None, None, None, None)
            pauses.add(subj)
    vkeys = (verdict or {}).get("violation_keys") or []
    ever_cleared_judge = any(s.startswith("judge_violation:") for s in cleared)
    for key in vkeys:
        subj = f"judge_violation:{key}"
        if subj in cleared or any(e["reason"] == subj for e in evs):
            continue
        if ever_cleared_judge and state != "RETIRED":
            emit("RETIRED", "violation", None, None, None, None)
            state = "RETIRED"
        else:
            emit("PAUSED", subj, None, None, None, None)
            pauses.add(subj)
    for subj in sorted(pauses & cleared):
        emit("RESUMED", f"clear:{subj}", None, None, None, None)
        pauses.discard(subj)

    E = K = None
    for e in sorted(processed_days):
        if state == "RETIRED":
            break
        if first_day is None or e < first_day:
            continue
        if state == "SHAKEDOWN":
            emit("TESTING", "first_day", e, 0, 1.0, None)
            state = "TESTING"
        chk = promotion(rows, first_day, e, verdict)
        E = chk["E"]
        ct = counted([r for r in rows if r["day"] <= e])
        R = [r["settled"]["outcome"]["R"] for r in ct]
        if state == "TESTING":
            if chk["all"]:
                emit("PROMOTED", "P1-P5,P7", e, chk["n"], E, 1.0)
                state, promoted_day = "PROMOTED", e
            elif chk["n"] >= 150 and sum(R) <= 0:
                emit("RETIRED", "n150", e, chk["n"], E, None)
                state = "RETIRED"
            elif e >= clock.add_months(first_day, 12):
                emit("RETIRED", "age", e, chk["n"], E, None)
                state = "RETIRED"
        elif state == "PROMOTED":
            post = [r for r in ct if r["day"] > promoted_day]
            py = [r["settled"]["outcome"]["y"] for r in post]
            pR = [r["settled"]["outcome"]["R"] for r in post]
            K = _eproc().k_value(py)
            reason = None
            if K >= 10:
                reason = "D1"
            elif pR and sum(pR) <= -5:
                reason = "D2"
            else:
                for mm in range(100, len(pR) + 1, 100):
                    if sum(pR[:mm]) <= 0:
                        reason = "D3"
                        break
            if reason:
                emit("DEMOTED", reason, e, chk["n"], E, K)
                state, demoted_day = "DEMOTED", e
        elif state == "DEMOTED":
            if clock.busday_count(demoted_day + dt.timedelta(days=1), e + dt.timedelta(days=1)) >= 5:
                emit("RETIRED", "demoted", e, chk["n"], E, K)
                state = "RETIRED"
    rows_all, _ = collect(view, now.date(), overlays)
    ct = counted(rows_all)
    y = [r["settled"]["outcome"]["y"] for r in ct]
    E = _eproc().e_value(y)
    if promoted_day:
        K = _eproc().k_value([r["settled"]["outcome"]["y"] for r in ct if r["day"] > promoted_day])
    term = [r for r in rows_all if r["terminal"]["status"] != "skipped:holiday"][-40:]
    bad = [r for r in term if r["overlay"] or r["terminal"]["status"].startswith(("missed:", "no_call:"))]
    share = len(bad) / len(term) if term else 0.0
    sides = [r["settled"]["side"] for r in ct]
    side_share = max(sides.count(1), sides.count(-1)) / len(sides) if sides else None
    return {"schema": STATE_SCHEMA, "asof_utc": rec.iso(now), "state": state,
            "calls_enabled": state != "RETIRED" and not pauses, "pause_reasons": sorted(pauses),
            "paused_display": share > 0.15, "missed_share_40": share, "n": len(ct), "E": E, "E_star": E_STAR, "K": K,
            "first_day": first_day.isoformat() if first_day else None,
            "last_processed_day": max(processed_days).isoformat() if processed_days else
            (view.read_json(paths.STATE_PATH) or {}).get("last_processed_day"),
            "side_share": side_share, "side_label": bool(side_share is not None and side_share > 0.85),
            "judge_verdict": {"value": (verdict or {}).get("analyst_verdict"),
                              "asof_utc": (verdict or {}).get("asof_utc"), "fetched": verdict is not None},
            "last_event_seq": seq, "run_id": run_id, "head_sha": head_sha}


def _num(x):
    if x is None:
        return ""
    if isinstance(x, bool):
        return str(int(x))
    if isinstance(x, int):
        return str(x)
    return repr(float(x))


def ledger_csv(view, now):
    overlays, _ = model_overlays(view)
    rows, _first = collect(view, now.date(), overlays)
    ct_ids = {r["cid"] for r in counted(rows)}
    out = io.StringIO()
    wr = csv.writer(out, lineterminator="\n")
    wr.writerow(LEDGER_COLS)
    ys, trade_no = [], 0
    promo = [e for e in events(view) if e["event"] == "PROMOTED"]
    pday = dt.date.fromisoformat(promo[0]["model_day"]) if promo else None
    post = []
    for r in rows:
        t, c, s, tw = r["terminal"], r["call"] or {}, r["settled"], r["twin"]
        o = (s or {}).get("outcome") or {}
        opp = (s or {}).get("opposite") or {}
        plan_ = c.get("plan") or {}
        cnt = r["cid"] in ct_ids
        tn = E = K = None
        if cnt:
            trade_no += 1
            ys.append(o["y"])
            tn, E = trade_no, _eproc().e_value(ys)
            if pday and r["day"] > pday:
                post.append(o["y"])
                K = _eproc().k_value(post)
        status = "no_call:model" if r["overlay"] else t["status"]
        wr.writerow([r["cid"], r["day"].isoformat(), r["W"], r["class"], t["status"], status, t.get("detail") or "",
                     plan_.get("action") or "", _num(c.get("side")), _num(c.get("stop_mult")),
                     _num(c.get("target_mult")), plan_.get("confidence") or "",
                     _num((c.get("d_ref") or {}).get("D_ref")), _num(c.get("S")), _num(c.get("T")),
                     (t.get("call") or {}).get("lock_utc") or "", str(len(t.get("attempts") or [])), t["entry_utc"],
                     rec.iso(clock.window_of(r["cid"]).exit_utc), _num(o.get("P_e")), _num(o.get("X")),
                     _num(o.get("Tg")), o.get("exit_minute_utc") or "", _num(o.get("P_x")), o.get("reason") or "",
                     o.get("fill") or "", _num(o.get("gross_usd")), _num(o.get("cost_usd")), _num(o.get("net_usd")),
                     _num(o.get("stress_net_usd")), _num(o.get("R")), _num(o.get("R_stress")), _num(o.get("y")),
                     _num(tn), _num(E), _num(K), _num((tw or {}).get("d_W")), (tw or {}).get("status") or "",
                     _num(opp.get("net_usd")), _num(opp.get("R")), (s or {}).get("settled_utc") or "",
                     r["terminal_sha256"], r["call_sha256"] or "", r["settled_sha256"] or ""])
    return out.getvalue().encode("utf-8")
