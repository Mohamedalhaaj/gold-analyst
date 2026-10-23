"""One analyst-settle run: the closer, settlement in window order, the state, the ledger (prereg §4, §6, §9;
register R-AN-39, R-AN-40, R-AN-56, R-AN-58, R-AN-70, §16.18 A6). FROZEN.

The closer runs first, so the run starts with the complete read of the inbox's activity: a failed read stops it before
any witness, terminal, revision or settlement record (R-AN-39, R-AN-40); settlement waits for the next settle run.
Settlement stops, logging `call_file_missing <call_id>`, at a `called` terminal whose call file is not on main (a lock
whose bytes could not be reproduced, §16.18 A6 second review, GA-B1): it never settles without the call, and never
fails every later run on it.

Fourth review of §16.18 A6 (N1, N2, J-4): a run that reaches the merge's run budget (R-AN-32) skips only steps 2 and 3
of the closer; settlement, the pause rule and the state still run, so slow pushes never hold back a pause. The fetched
verdict feeds P7 and the pause rule only when it is fresh (state.fresh_verdict: asof_utc within 48 h); 48 h without a
fresh one pauses `judge_silent` (R-AN-55, R-AN-70)."""
import json
import urllib.request

from . import clock, merge, paths, rec, settle, state, store
from .merge import Merge

SETTLE_WRITE_PREFIXES = merge.ALLOWED_WRITE_PREFIXES + ("analyst/settled/", "analyst/twins/", "analyst/state/",
                                                       "analyst/STATE.json", "analyst/ledger.csv")
SNAPSHOTS = ("analyst/STATE.json", "analyst/ledger.csv")


def fetch_verdict(pins, timeout=20):
    """The deployed verdict, or None when the fetch fails or the file is not a verdict. Whether it counts (fresh) is
    decided against the run's t0 in state.run_state (R-AN-70)."""
    base = (pins or {}).get("judge_site_url")
    if not base:
        return None
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/judge_analyst/analyst_verdict.json", timeout=timeout) as r:
            doc = json.loads(r.read().decode("utf-8"))
        if doc.get("schema") != "analyst-verdict/1" or doc.get("analyst_verdict") not in ("SOUND", "WARNING",
                                                                                           "VIOLATION"):
            return None
        return doc
    except Exception:  # noqa: BLE001
        return None


def settle_windows(ws, now, run_id, head_sha, log=lambda s: None):
    """Settle live windows in window order; returns every processed window (earlier runs included)."""
    view = ws.view
    mode_doc = paths.read_mode(view)
    _m, a, _b = paths.mode_period(mode_doc, "live")
    if a is None:
        return []
    rows = store.parse_days((view.read(store.DAYS_PATH) or b"").decode("utf-8"))
    rbk = store.final_status(rows)
    done = []
    for w in merge.expected_windows(mode_doc, "live", now.date()):
        t = view.read_json(paths.terminal_path("live", w.call_id))
        if t is None:
            break
        if t["status"] == "skipped:holiday":
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
    ids = {w.call_id for w in done}
    prev = (ws.view.read_json(paths.STATE_PATH) or {}).get("last_processed_day") or ""
    full = [d for d in sorted({w.day for w in done})
            if all(x.call_id in ids for x in clock.windows_on(d)) and d.isoformat() > prev]
    st = state.run_state(ws.view, full, now, run_info.get("id"), head, verdict, ws.write)
    ws.write(paths.STATE_PATH, rec.record_bytes(st))
    ws.write(paths.LEDGER_PATH, state.ledger_csv(ws.view, now))
    if ws.commit_push(f"analyst-settle {rec.iso(now)[:16]}Z", SETTLE_WRITE_PREFIXES, sleep=sleep,
                      modifiable=SNAPSHOTS) and dispatch:
        m.dispatch_after_commit()
    return st, done
