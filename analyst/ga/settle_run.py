"""One analyst-settle run: the closer, settlement in window order, the state, the ledger (prereg §4, §6, §9;
register R-AN-39, R-AN-40, R-AN-56, R-AN-58, R-AN-70, §16.18 A6). FROZEN.

The closer runs first, so the run starts with the complete read of the inbox's activity: a failed read stops it before
any witness, terminal, revision or settlement record (R-AN-39, R-AN-40); settlement waits for the next settle run.
Settlement stops, logging `call_file_missing <call_id>`, at a `called` terminal whose call file is not on main (a lock
whose bytes could not be reproduced, §16.18 A6 second review, GA-B1): it never settles without the call, and never
fails every later run on it.

Fourth review of §16.18 A6 (N1, N2, J-4): a run that reaches the merge's run budget (R-AN-32) skips only steps 2 and 3
of the closer; settlement, the pause rule and the state still run, so slow pushes never hold back a pause. The fetched
verdict feeds P7 and the pause rule only when it is fresh (state.fresh_verdict: asof_utc within 48 h, and, since
§16.18 A7 decision 15, not older than the newest fresh verdict an earlier run fetched); 48 h without a fresh one
pauses `judge_silent` (R-AN-55, R-AN-70).

§16.18 A15 (R-AN-40, R-AN-93..R-AN-95): for model days d >= a15_from, in each mode (live, and canary), the windows of
d are settled as one unit, once now >= model_to_utc((d+1) 00:00), every expected window of d has a terminal, the store
is final for d − 1 and d, and every `called` window's call file is on main (else `call_file_missing <call_id>` stops
the day and every later day of its mode). The open sets are recomputed from main's terminals and call files
(ga/review.open_sets), each trade closed by a review gets its managed exit, every trade its x_end, and the day's
trades their weights and clusters (R-AN-94) before anything is written; then every `called` window (buy, sell,
reverse, add) gets an `analyst-settled/2` record and every terminal window except `skipped:*` a twin. Canary windows
of model days >= a15_from are settled the same way under analyst/canary/ (class `canary`), in the canary's own window
order; earlier canary windows are never settled. The modes never hold each other back; the state and the ledger are
live only."""
import datetime as dt
import json
import urllib.request

from . import clock, hours, merge, paths, rec, review, settle, state, store
from .merge import Merge

SETTLE_WRITE_PREFIXES = merge.ALLOWED_WRITE_PREFIXES + ("analyst/settled/", "analyst/twins/", "analyst/state/",
                                                       "analyst/STATE.json", "analyst/ledger.csv")
SNAPSHOTS = ("analyst/STATE.json", "analyst/ledger.csv")


def fetch_verdict(pins, timeout=20):
    """The deployed verdict, or None when the fetch fails or the file is not a verdict. Whether it counts (fresh) is
    decided against the run's t0 and STATE.json's last_fresh_asof_utc in state.run_state (R-AN-70)."""
    base = (pins or {}).get("judge_site_url")
    if not base:
        return None
    try:
        # §16.18 A13: the judge's site refuses Python's default agent (Cloudflare 1010), so send the package's own
        req = urllib.request.Request(base.rstrip("/") + "/judge_analyst/analyst_verdict.json",
                                     headers={"User-Agent": hours.UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            doc = json.loads(r.read().decode("utf-8"))
        if doc.get("schema") != "analyst-verdict/1" or doc.get("analyst_verdict") not in ("SOUND", "WARNING",
                                                                                           "VIOLATION"):
            return None
        return doc
    except Exception:  # noqa: BLE001
        return None


def settle_windows(ws, now, run_id, head_sha, log=lambda s: None, mode="live"):
    """Settle `mode`'s windows in window order; returns every processed window (earlier runs included)."""
    view = ws.view
    mode_doc = paths.read_mode(view)
    _m, a, _b = paths.mode_period(mode_doc, mode)
    a15 = paths.a15_from(mode_doc)
    if a is None or (mode == "canary" and a15 is None):
        return []
    rows = store.parse_days((view.read(store.DAYS_PATH) or b"").decode("utf-8"))
    rbk = store.final_status(rows)
    done = []
    expected = merge.expected_windows(mode_doc, mode, now.date(), view)
    if mode == "canary":                       # canary windows before a15_from are never settled (R-AN-95)
        expected = [w for w in expected if w.day >= a15]
    days_done = set()
    for w in expected:
        if a15 is not None and w.day >= a15:
            if w.day in days_done:
                continue
            day = [x for x in expected if x.day == w.day]
            if not settle_day(ws, mode, day, rbk, now, run_id, head_sha, log):
                break
            days_done.add(w.day)
            done.extend(day)
            continue
        t = view.read_json(paths.terminal_path("live", w.call_id))
        if t is None:
            break
        if t["status"].startswith("skipped:"):     # no twin for any skipped:* status (§16.18 A14, R-AN-40)
            done.append(w)
            continue
        need_settle = t["status"] == "called"
        if view.exists(paths.twin_path(w.call_id)) and (not need_settle or view.exists(paths.settled_path(w.call_id))):
            done.append(w)
            continue
        if need_settle and not view.exists(paths.call_path("live", w.call_id)):
            log(f"call_file_missing {w.call_id}")
            break
        if not settle.processable(rbk, view, w, now):
            break
        m, _used = store.load_days(view, rbk, settle.day_inputs(rbk, w)[0])
        start = view.read_json(paths.START_PATH)
        cls = "counted" if start and w.day.isoformat() >= start["first_day"] else "shakedown"
        if need_settle and not view.exists(paths.settled_path(w.call_id)):
            call = view.read_json(paths.call_path("live", w.call_id))
            doc = settle.settled_record(view, w, call, t, cls, m, rbk, now, run_id, head_sha)
            ws.write(paths.settled_path(w.call_id), rec.record_bytes(doc))
        if not view.exists(paths.twin_path(w.call_id)):
            ws.write(paths.twin_path(w.call_id), rec.record_bytes(
                settle.twin_record(view, w, t, m, rbk, now, run_id, head_sha)))
        done.append(w)
    return done


def settle_day(ws, mode, day, rbk, now, run_id, head_sha, log=lambda s: None):
    """Settle the windows `day` of one model day >= a15_from of `mode` as one unit (R-AN-40, R-AN-93, R-AN-94).
    True when the day is (now or already) settled; False when it is not processable yet (the caller stops)."""
    view = ws.view
    d = day[0].day
    terms = {w.call_id: view.read_json(paths.terminal_path(mode, w.call_id)) for w in day}
    if any(t is None for t in terms.values()) or now < day_end(d):
        return False
    called = [w for w in day if terms[w.call_id]["status"] == "called"]
    twins = [w for w in day if not terms[w.call_id]["status"].startswith("skipped:")]
    if all(view.exists(paths.settled_path(w.call_id, mode)) for w in called) and \
            all(view.exists(paths.twin_path(w.call_id, mode)) for w in twins):
        return True
    for w in called:
        if not view.exists(paths.call_path(mode, w.call_id)):
            log(f"call_file_missing {w.call_id}")
            return False
    if not all(settle.processable(rbk, view, w, now) for w in day):
        return False
    m, _used = store.load_days(view, rbk, settle.day_inputs(rbk, day[0])[0])
    calls = {w.call_id: view.read_json(paths.call_path(mode, w.call_id)) for w in day
             if view.exists(paths.call_path(mode, w.call_id))}
    entries = [review.entry_from_call(w.call_id, w, terms[w.call_id]["status"], calls.get(w.call_id)) for w in day]
    o_sets, _eff, closed_by = review.open_sets(entries)
    revs = review.reviews_of(entries, o_sets)
    by_id = {w.call_id: w for w in day}
    res = {}
    for w in called:
        c = closed_by.get(w.call_id)
        res[w.call_id] = settle.outcomes(m, w, calls[w.call_id], rbk, by_id[c].entry_utc if c else None)
    wts = review.weights([(w.call_id, w.entry_utc, res[w.call_id]["x_end"]) for w in called])
    start = view.read_json(paths.START_PATH)
    if mode == "canary":
        cls = "canary"
    else:
        cls = "counted" if start and d.isoformat() >= start["first_day"] else "shakedown"
    for w in called:
        if view.exists(paths.settled_path(w.call_id, mode)):
            continue
        c = closed_by.get(w.call_id)
        rv = []
        for r in revs.get(w.call_id, []):
            pl = (calls.get(r["call_id"]) or {}).get("plan")
            pl = pl if isinstance(pl, dict) else None
            rv.append({"call_id": r["call_id"], "status": terms[r["call_id"]]["status"],
                       "action": pl.get("action") if pl else None, "confidence": pl.get("confidence") if pl else None,
                       "reason_ar": pl.get("reason_ar") if pl else None})
        extra = {"managed_utc": by_id[c].entry_utc if c else None, "closed_by": c, "weight": wts[w.call_id][0],
                 "cluster": wts[w.call_id][1], "reviews": rv, "res": res[w.call_id]}
        doc = settle.settled_record(view, w, calls[w.call_id], terms[w.call_id], cls, m, rbk, now, run_id, head_sha,
                                    mode=mode, a15=extra)
        ws.write(paths.settled_path(w.call_id, mode), rec.record_bytes(doc))
    for w in twins:
        if not view.exists(paths.twin_path(w.call_id, mode)):
            ws.write(paths.twin_path(w.call_id, mode), rec.record_bytes(
                settle.twin_record(view, w, terms[w.call_id], m, rbk, now, run_id, head_sha, mode=mode)))
    return True


def day_end(d):
    """model_to_utc((d+1) 00:00): the end of model day d's trading (R-AN-40, §16.18 A14)."""
    return clock.model_to_utc(dt.datetime.combine(d + dt.timedelta(days=1), dt.time(0)))


def day_expected(view, d):
    """The live expected windows of model day d (R-AN-11): W1, W2 (W1b, W2b and its trigger windows with an
    accepted attempt witness once d >= a14_from)."""
    return [w for w in merge.expected_windows(paths.read_mode(view), "live", d, view) if w.day == d]


def run_settle(ws, api, now_fn, run_info, verdict=None, sleep=lambda s: None, inbox=None, log=lambda s: None,
               dispatch=True):
    """now_fn: a callable returning the run's aware UTC time (R-C5: taken once for the records). `inbox` defaults
    to `api` (a test double serving both). After its commit reached main, the run dispatches analyst-track for each
    buy/sell lock its closer witnessed, then analyst-deploy once (R-AN-40)."""
    t0 = now_fn()
    m = Merge(ws, api, lambda: t0, run_info, sleep=sleep, inbox=api if inbox is None else inbox, log=log)
    m.read_inbox()
    m.complete_from_comments()
    m.closer()
    now = t0
    head = m.head_sha
    done = settle_windows(ws, now, run_info.get("id"), head, log=log)
    settle_windows(ws, now, run_info.get("id"), head, log=log, mode="canary")      # R-AN-95 (§16.18 A15)
    ids = {w.call_id for w in done}
    prev = (ws.view.read_json(paths.STATE_PATH) or {}).get("last_processed_day") or ""
    full = [d for d in sorted({w.day for w in done})
            if all(x.call_id in ids for x in day_expected(ws.view, d)) and d.isoformat() > prev
            and (not paths.a14_applies(paths.read_mode(ws.view), d) or now >= day_end(d))]
    st = state.run_state(ws.view, full, now, run_info.get("id"), head, verdict, ws.write)
    ws.write(paths.STATE_PATH, rec.record_bytes(st))
    ws.write(paths.LEDGER_PATH, state.ledger_csv(ws.view, now))
    if ws.commit_push(f"analyst-settle {rec.iso(now)[:16]}Z", SETTLE_WRITE_PREFIXES, sleep=sleep,
                      modifiable=SNAPSHOTS) and dispatch:
        m.dispatch_after_commit()
    return st, done
