"""The test, the states and the derived ledger (prereg §6, §1 PAUSED; register R-AN-51..R-AN-58, R-AN-62). FROZEN.

E_n = eproc.e_value(y) from the vendored, byte-identical lab e-process; K = eproc.k_value over post-promotion trades;
E* = 400 exactly (α_A = alpha_version(3) = 0.0025). Promotion needs P1-P5 and P7; demotion D1-D3; retirement n150, age,
demoted (>= 5 weekdays after the demotion) or violation. Events are create-only latches.

The pause rule (R-AN-55 as amended in the fourth and sixth reviews of §16.18 A6, J-4, N2 and R6-2), once per
settle run:
- the judge's verdict counts only when fresh (R-AN-70): valid, with asof_utc no more than 48 h before the run and no
  more than 10 min after it, and (§16.18 A7 decision 15) not earlier than L_prev, the last_fresh_asof_utc of
  STATE.json at the start of the run: an older verdict served after a newer one (a stale cache, an old deploy) is
  not fresh, so last_fresh_asof_utc never decreases. Otherwise P7 is false and the verdict opens, lifts and retires
  nothing;
- before P0 (00:00Z of the earliest set date of canary_from and live_from) the verdict makes no event at all;
- operational keys (A0, system_error:, A12 probe_failed / app_scope / routine_connectors / inbox_identity /
  inbox_config, A13 guard_not_enforced / guard_config, and since the third review of §16.18 A7 A13 routine_project /
  cc_version_below / cc_version_unknown) and every key before LS (00:00Z of live_from) are mirrored:
  paused while a fresh verdict carries them, lifted (`lifted:`) when it does not, never cleared by an analyst-side
  row and never a cause of retirement;
- a live integrity key pauses until an analyst-side clear dated at or after its PAUSED event; a new live integrity
  key after such a clear retires the analyst, "after" by time (sixth review, R6-2): the key's first-seen time in the
  verdict (`violation_first_seen`, the judge's FINDINGS.csv) is later than A, the earliest clear time of a cleared
  live integrity pause (the utc of its earliest clear row dated at or after its PAUSED event), whatever the order in
  which settle runs saw the clear and the key. A key without a usable first-seen time pauses and never retires;
- 48 h without a fresh verdict pauses `judge_silent` (from P0 on); the next fresh verdict lifts it.
Interpretation (logged for the register): a judge subject is mirrored when ANY of its PAUSED events lies before LS
(step 2's own test), so a canary key that recurs in the live period stays mirrored, as its first pause was.
Interpretation (sixth review, logged for the register): a cleared live pause whose clear row is no longer in
ADJUDICATIONS.csv (an append-only file edited by hand) takes its RESUMED event's utc as its clear time, the latest
time it can have, so a lost row never makes an earlier key retire the analyst.

§16.18 A15 (R-AN-51..R-AN-58, R-AN-94): each counted trade carries the weight and cluster of its settled record (1.0
and its own call_id for a record without them). The count n = Σ w_i (P2, n150, STATE `n`, a multiple of 0.5 as a
float); every trade sum is Σ w_i·x_i (P3, P4, P5, D2, n150's Σ R); the e-process and K take one factor per cluster,
in the order of their first trades, with y_C = Σ w_i·y_i / Σ w_i over the cluster's counted trades; P5 drops the best
cluster; the side share is weighted; D3 is tested at the first post-promotion trade at which the cumulative post
weight reaches 100, 200, ... STATE.json is `analyst-state/2`: `n` weighted, then `n_trades` and `n_clusters`. The
ledger has `n_w` after `trade_no` and `weight, cluster, x_end_utc, closed_by` after `y`; E and K are written on each
cluster's last counted row (every counted row for a trade before a15_from, its own cluster). μ_W and the twins stay
unweighted.
"""
import csv
import datetime as dt
import io

from . import clock, paths, rec

E_STAR = 400.0
ALPHA = 0.0025
MODEL = "claude-opus-5-5"
EVENT_SCHEMA = "analyst-state-event/1"
STATE_SCHEMA = "analyst-state/2"
VERDICT_SCHEMA = "analyst-verdict/1"
VERDICT_VALUES = ("SOUND", "WARNING", "VIOLATION")
FRESH_BEFORE = dt.timedelta(hours=48)       # a verdict older than this before the run is not fresh (R-AN-70, N2)
FRESH_AFTER = dt.timedelta(minutes=10)      # nor one dated later than this after it
SILENT_AFTER = dt.timedelta(hours=48)       # judge_silent: no fresh verdict for longer than this (R-AN-55 step 4)
JUDGE = "judge_violation:"
JUDGE_SILENT = "judge_silent"
OPERATIONAL_A12 = ("probe_failed", "app_scope", "routine_connectors", "inbox_identity", "inbox_config")
# the session guard not acting, or its files (§16.18 A7); a routine in a claude.ai project, or a harness that may not
# keep the guard closed (its third review, decisions 21 and 22)
OPERATIONAL_A13 = ("guard_not_enforced", "guard_config", "routine_project", "cc_version_below", "cc_version_unknown")
# A14's table (skipped_wrong, a14_from) and A15's open sets, cap, pairing day, lesson source (R-AN-55, §16.18 A15)
OPERATIONAL_A14 = ("skipped_wrong", "a14_from")
OPERATIONAL_A15 = ("review_open", "open_cap", "a15_from", "lesson_source")
LEDGER_COLS = ["call_id", "model_day", "window", "class", "terminal_status", "status", "detail", "action", "side",
               "stop_mult", "target_mult", "confidence", "D_ref", "S", "T", "lock_utc", "attempts", "entry_utc",
               "exit_utc", "P_e", "X", "Tg", "exit_minute_utc", "P_x", "reason", "fill", "gross_usd", "cost_usd",
               "net_usd", "stress_net_usd", "R", "R_stress", "y", "weight", "cluster", "x_end_utc", "closed_by",
               "trade_no", "n_w", "E", "K", "d_W", "twin_status",
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
        w = clock.window_of(cid)
        if w.name in clock.WINDOWS:
            routines = ("analyst-w1",) if w.name == "W1" else ("analyst-w2",)
            fallback, lo_min = routines, 60
        else:                                   # W1b, W2b, T (§16.18 A14, R-AN-57): start selects by the clock
            routines, fallback, lo_min = ("analyst-wake", "analyst-w1", "analyst-w2"), ("analyst-wake",), 30
        cand = [r for r in runs if r.get("routine") in routines]
        match = [r for r in cand if sid and r.get("session_id") == sid]
        if not match and not any(r.get("session_id") for r in cand):
            lo = rec.iso(w.entry_utc - dt.timedelta(minutes=lo_min))
            hi = rec.iso(w.deadline_utc)
            match = [r for r in cand if r.get("routine") in fallback and lo <= (r.get("created_utc") or "") <= hi]
        if not match:
            unmatched.add(cid)
            continue
        bad = [r for r in match if r.get("model") != MODEL]
        if bad:
            over[cid] = bad[0].get("run_id")
    return over, unmatched


def clear_rows(view):
    """{subject: [utc, ...]} of the owner's pause clears in analyst/ADJUDICATIONS.csv (R-AN-62); a row whose utc is
    not an R-C4 time clears nothing."""
    b = view.read("analyst/ADJUDICATIONS.csv")
    out = {}
    if not b:
        return out
    for r in csv.DictReader(io.StringIO(b.decode("utf-8"))):
        if r.get("ref") != "pause" or r.get("decision") != "clear":
            continue
        t = _parse(r.get("utc"))
        if t is not None:
            out.setdefault(r.get("subject"), []).append(t)
    return out


def _parse(s):
    try:
        return rec.parse_iso(s)
    except (TypeError, ValueError):
        return None


def fresh_verdict(verdict, now, last_fresh=None):
    """The verdict when it is fresh (R-AN-70, §16.18 A6 fourth review, N2), else None: schema analyst-verdict/1, a
    known analyst_verdict, violation_keys a list of strings, and asof_utc an R-C4 time in [now - 48 h, now + 10 min]
    and (§16.18 A7 decision 15) not earlier than `last_fresh`, L_prev: the asof of the newest fresh verdict an earlier
    run fetched (no condition while it is None). The same verdict fetched again stays fresh."""
    if not isinstance(verdict, dict) or verdict.get("schema") != VERDICT_SCHEMA \
            or verdict.get("analyst_verdict") not in VERDICT_VALUES:
        return None
    keys = verdict.get("violation_keys")
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        return None
    t = _parse(verdict.get("asof_utc"))
    if t is None or not now - FRESH_BEFORE <= t <= now + FRESH_AFTER:
        return None
    if last_fresh is not None and t < last_fresh:
        return None
    return verdict


def first_seen_times(verdict):
    """{key: aware UTC} from a fresh verdict's `violation_first_seen` (R-AN-70, §16.18 A6 sixth review, R6-2): the
    first time the judge found each key. An entry counts only as an R-C4 time at or before the verdict's asof_utc;
    a missing or malformed map, or entry, gives none for that key."""
    m = verdict.get("violation_first_seen")
    asof = _parse(verdict.get("asof_utc"))
    if not isinstance(m, dict) or asof is None:
        return {}
    out = {}
    for k, v in m.items():
        t = _parse(v) if isinstance(k, str) and isinstance(v, str) else None
        if t is not None and t <= asof:
            out[k] = t
    return out


def operational(key):
    """An operational VIOLATION key (R-AN-55, R-AN-67): the judge cannot see, a system error, the sandbox leaked, or
    the owner's own configuration; since §16.18 A7 also the session guard not acting or its files wrong (A13
    guard_not_enforced, guard_config), and since its third review a routine in a claude.ai project or a guarded
    session on a Claude Code version below the guard's minimum or of an unknown version (A13 routine_project,
    cc_version_below, cc_version_unknown); since §16.18 A15 also A14 skipped_wrong and a14_from and A15 review_open,
    open_cap, a15_from and lesson_source. It pauses while the verdict carries it and never retires the analyst. A13
    forbidden_attempt and forbidden_executed are integrity keys."""
    if key.startswith(("A0|", "system_error:")):
        return True
    parts = key.split("|")
    return len(parts) >= 2 and ((parts[0] == "A12" and parts[1] in OPERATIONAL_A12)
                                or (parts[0] == "A13" and parts[1] in OPERATIONAL_A13)
                                or (parts[0] == "A14" and parts[1] in OPERATIONAL_A14)
                                or (parts[0] == "A15" and parts[1] in OPERATIONAL_A15))


def period_starts(mode_doc):
    """(P0, LS): 00:00:00Z of the earliest set date of canary_from and live_from, and of live_from (None if unset)."""
    def midnight(d):
        return dt.datetime.combine(dt.date.fromisoformat(d), dt.time(0), tzinfo=rec.UTC)
    set_ = [midnight(d) for d in (mode_doc.get("canary_from"), mode_doc.get("live_from")) if d]
    ls = midnight(mode_doc["live_from"]) if mode_doc.get("live_from") else None
    return (min(set_) if set_ else None), ls


def resumed_subject(reason):
    """The subject of a RESUMED event: its reason after the first `clear:` or `lifted:` (R-AN-56)."""
    for p in ("clear:", "lifted:"):
        if reason.startswith(p):
            return reason[len(p):]
    return reason


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
        rows.append({"cid": cid, "day": k[0], "W": clock.window_type(cid), "terminal": t, "call": call,
                     "settled": s, "twin": tw,
                     "class": cls, "overlay": cid in overlays,
                     "terminal_sha256": rec.sha256_bytes(view.read(p)),
                     "call_sha256": (t.get("call") or {}).get("sha256"),
                     "settled_sha256": rec.sha256_bytes(view.read(paths.settled_path(cid))) if s else None})
    return rows, first_day


def counted(rows):
    return [r for r in rows if r["terminal"]["status"] == "called" and r["class"] == "counted"
            and not r["overlay"] and r["settled"] is not None]


def weight(r):
    """A counted trade's weight (R-AN-94): its settled record's, 1.0 before a15_from."""
    w = r["settled"].get("weight")
    return 1.0 if w is None else float(w)


def cluster_of(r):
    return r["settled"].get("cluster") or r["cid"]


def clusters(ct):
    """The clusters of the counted trades `ct` (window order), in the order of their first trades: [[row, ...]]."""
    out, idx = [], {}
    for r in ct:
        c = cluster_of(r)
        if c not in idx:
            idx[c] = len(out)
            out.append([])
        out[idx[c]].append(r)
    return out


def cluster_y(cl):
    """y_C = Σ w_i·y_i / Σ w_i (R-AN-94)."""
    return sum(weight(r) * r["settled"]["outcome"]["y"] for r in cl) / sum(weight(r) for r in cl)


def wsum(ct, f):
    return sum(weight(r) * f(r) for r in ct)


def e_of(ct):
    return _eproc().e_value([cluster_y(cl) for cl in clusters(ct)])


def k_of(ct):
    return _eproc().k_value([cluster_y(cl) for cl in clusters(ct)])


def promotion(rows, first_day, e, verdict):
    """(checks dict, all) at model day e."""
    ct = counted([r for r in rows if r["day"] <= e])
    n = float(sum(weight(r) for r in ct))
    E = e_of(ct)
    out = {"n": n, "E": E}
    out["P1"] = E >= E_STAR
    out["P2"] = n >= 60 and (ct[-1]["day"] - ct[0]["day"]).days >= 56 if ct else False
    st = wsum(ct, lambda r: r["settled"]["outcome"]["stress_net_usd"])
    st_c = wsum(ct, lambda r: r["settled"]["outcome"]["stress_net_usd"] - 0.5 * r["settled"]["outcome"]["cost_usd"])
    out["P3"] = bool(ct) and st > 0 and st_c > 0
    mu = {}
    # W1, W2, and W1b, W2b and T once they occur (§16.18 A14, R-AN-47, R-AN-53)
    for W in [x for x in clock.WINDOW_TYPES if x in clock.WINDOWS or any(r["W"] == x for r in rows)]:
        tw = [r["twin"]["d_W"] for r in rows if r["W"] == W and r["twin"] and r["twin"]["status"] == "final"
              and first_day is not None and first_day <= r["day"] <= e]
        mu[W] = sum(tw) / len(tw) if tw else None
    if ct and all(mu[r["W"]] is not None for r in ct):
        out["P4"] = wsum(ct, lambda r: r["settled"]["outcome"]["net_usd"] - r["settled"]["side"] * mu[r["W"]]) > 0
    else:
        out["P4"] = False
    R_of = (lambda r: r["settled"]["outcome"]["R"])
    out["P5"] = bool(ct) and wsum(ct, R_of) - max(wsum(cl, R_of) for cl in clusters(ct)) > 0
    out["P7"] = verdict is not None and verdict.get("analyst_verdict") in ("SOUND", "WARNING")   # a fresh one
    out["mu"] = mu
    out["all"] = all(out[k] for k in ("P1", "P2", "P3", "P4", "P5", "P7"))
    return out


def run_state(view, processed_days, now, run_id, head_sha, verdict, write):
    """Evaluate the state after each processed model day (R-AN-53..R-AN-56). `write(path, bytes)` writes a file.
    `verdict` is the one this run fetched (None when the fetch failed); only a fresh one counts (R-AN-70). Returns
    the STATE.json object."""
    overlays, _unmatched = model_overlays(view)
    evs = events(view)
    seq = max([0] + [e["seq"] for e in evs])
    state = "SHAKEDOWN"
    promoted_day = demoted_day = None
    opened = {}                          # the open pauses: subject -> utc of the PAUSED event that opened it
    paused_at = {}                       # subject -> utcs of every PAUSED event for it
    cleared_judge = []                   # (subject, its PAUSED utc, the RESUMED utc) of `clear:judge_violation:`
    for e in evs:
        ev = e["event"]
        if ev in ("TESTING", "PROMOTED", "DEMOTED", "RETIRED"):
            state = ev
            if ev == "PROMOTED":
                promoted_day = dt.date.fromisoformat(e["model_day"])
            if ev == "DEMOTED":
                demoted_day = dt.date.fromisoformat(e["model_day"])
        elif ev == "PAUSED":
            t = _parse(e.get("utc"))
            opened.setdefault(e["reason"], t)
            paused_at.setdefault(e["reason"], []).append(t)
        elif ev == "RESUMED":
            subj = resumed_subject(e["reason"])
            if e["reason"].startswith("clear:" + JUDGE):
                cleared_judge.append((subj, opened.get(subj), _parse(e.get("utc"))))
            opened.pop(subj, None)
    upto = max(processed_days) if processed_days else now.date()
    rows, first_day = collect(view, upto, overlays)
    p0, ls = period_starts(paths.read_mode(view))
    prev_jv = (view.read_json(paths.STATE_PATH) or {}).get("judge_verdict") or {}
    last_fresh = _parse(prev_jv.get("last_fresh_asof_utc"))      # L_prev: STATE.json at the start of the run
    fresh = fresh_verdict(verdict, now, last_fresh)             # an older verdict is not fresh (§16.18 A7, d. 15)
    if fresh is not None:
        t = _parse(fresh["asof_utc"])
        last_fresh = t if last_fresh is None else max(last_fresh, t)

    def emit(event, reason, day, n, E, K):
        nonlocal seq
        seq += 1
        doc = {"schema": EVENT_SCHEMA, "seq": seq, "event": event, "reason": reason,
               "model_day": day.isoformat() if day else None, "n": n, "E": E, "K": K, "utc": rec.iso(now),
               "run_id": run_id, "head_sha": head_sha}
        write(paths.state_event_path(seq), rec.record_bytes(doc))
        evs.append(doc)
        if event == "PAUSED":
            opened.setdefault(reason, now)
            paused_at.setdefault(reason, []).append(now)
        elif event == "RESUMED":
            opened.pop(resumed_subject(reason), None)

    def before_ls(subj):
        return ls is not None and any(t is not None and t < ls for t in paused_at.get(subj, []))

    def mirrored(subj):
        """A judge pause the verdict lifts (R-AN-55): an operational key, no live_from, or paused before LS."""
        return subj.startswith(JUDGE) and (operational(subj[len(JUDGE):]) or ls is None or before_ls(subj))

    def live_integrity(subj):
        return subj.startswith(JUDGE) and not mirrored(subj)

    clears = clear_rows(view)

    def clear_time(subj, paused, resumed):
        """When the owner cleared a pause (sixth review, R6-2): the utc of its earliest clear row dated at or after
        its PAUSED event, the row step 1 applied; the RESUMED event's utc if no such row is left."""
        rows = [t for t in clears.get(subj, []) if paused is not None and t >= paused]
        return min(rows) if rows else resumed

    # A, the arming time (R-AN-55 step 2): the earliest clear time of a live integrity pause the owner cleared
    arm_times = [clear_time(s, p, r) for (s, p, r) in cleared_judge if live_integrity(s)]

    # model pauses (R-AN-57): once per run_id, whatever ADJUDICATIONS.csv held before the pause existed
    for cid, run in sorted(overlays.items()):
        subj = f"model:{run}"
        if subj not in opened and subj not in paused_at:
            emit("PAUSED", subj, None, None, None, None)
    # 1. clears: a model pause or a live integrity pause, by a row dated at or after its PAUSED event (R-AN-62)
    for subj in sorted(opened):
        if not (subj.startswith("model:") or live_integrity(subj)) or opened[subj] is None:
            continue
        if any(t >= opened[subj] for t in clears.get(subj, [])):
            if live_integrity(subj):
                arm_times.append(clear_time(subj, opened[subj], now))
            emit("RESUMED", f"clear:{subj}", None, None, None, None)
    arm_times = [t for t in arm_times if t is not None]
    armed_at = min(arm_times) if arm_times else None
    # 2.-4. the verdict, only from P0 (R-AN-55): keys, lifts, silence
    if p0 is not None and now >= p0:
        if fresh is not None:
            keys = fresh["violation_keys"]
            seen = first_seen_times(fresh)
            for key in sorted(set(keys)):
                subj = JUDGE + key
                if operational(key) or ls is None or now < ls or before_ls(subj):
                    if subj not in opened:
                        emit("PAUSED", subj, None, None, None, None)
                elif any(t is not None and t >= ls for t in paused_at.get(subj, [])):
                    continue                     # a live pause: in force until its clear (step 1), then done
                elif armed_at is not None and key in seen and seen[key] > armed_at and state != "RETIRED":
                    emit("RETIRED", "violation", None, None, None, None)     # first seen after a live clear
                    state = "RETIRED"
                else:
                    emit("PAUSED", subj, None, None, None, None)
            for subj in sorted(opened):
                if mirrored(subj) and subj[len(JUDGE):] not in keys:
                    emit("RESUMED", f"lifted:{subj}", None, None, None, None)
            if JUDGE_SILENT in opened:
                emit("RESUMED", f"lifted:{JUDGE_SILENT}", None, None, None, None)
        elif JUDGE_SILENT not in opened and (last_fresh is None or last_fresh < now - SILENT_AFTER):
            emit("PAUSED", JUDGE_SILENT, None, None, None, None)
    pauses = set(opened)
    verdict_p7 = fresh                           # P7 needs a fresh verdict (R-AN-53, N2)

    E = K = None
    for e in sorted(processed_days):
        if state == "RETIRED":
            break
        if first_day is None or e < first_day:
            continue
        if state == "SHAKEDOWN":
            emit("TESTING", "first_day", e, 0, 1.0, None)
            state = "TESTING"
        chk = promotion(rows, first_day, e, verdict_p7)
        E = chk["E"]
        ct = counted([r for r in rows if r["day"] <= e])
        sum_R = wsum(ct, lambda r: r["settled"]["outcome"]["R"])
        if state == "TESTING":
            if chk["all"]:
                emit("PROMOTED", "P1-P5,P7", e, chk["n"], E, 1.0)
                state, promoted_day = "PROMOTED", e
            elif chk["n"] >= 150 and sum_R <= 0:
                emit("RETIRED", "n150", e, chk["n"], E, None)
                state = "RETIRED"
            elif e >= clock.add_months(first_day, 12):
                emit("RETIRED", "age", e, chk["n"], E, None)
                state = "RETIRED"
        elif state == "PROMOTED":
            post = [r for r in ct if r["day"] > promoted_day]
            K = k_of(post)
            reason = None
            if K >= 10:
                reason = "D1"
            elif post and wsum(post, lambda r: r["settled"]["outcome"]["R"]) <= -5:
                reason = "D2"
            else:
                cw = cr = 0.0
                mm = 100
                for r in post:                     # D3 at the first trade whose cumulative weight reaches m
                    cw += weight(r)
                    cr += weight(r) * r["settled"]["outcome"]["R"]
                    if cw >= mm:
                        if cr <= 0:
                            reason = "D3"
                            break
                        while cw >= mm:
                            mm += 100
            if reason:
                emit("DEMOTED", reason, e, chk["n"], E, K)
                state, demoted_day = "DEMOTED", e
        elif state == "DEMOTED":
            if clock.busday_count(demoted_day + dt.timedelta(days=1), e + dt.timedelta(days=1)) >= 5:
                emit("RETIRED", "demoted", e, chk["n"], E, K)
                state = "RETIRED"
    rows_all, _ = collect(view, now.date(), overlays)
    ct = counted(rows_all)
    E = e_of(ct)
    # K over post-promotion counted trades; 1.0 before a promotion (R-AN-52 clarification A4, as lab R-V1-34)
    K = k_of([r for r in ct if r["day"] > promoted_day]) if promoted_day else 1.0
    term = [r for r in rows_all if not r["terminal"]["status"].startswith("skipped:")][-40:]
    bad = [r for r in term if r["overlay"] or r["terminal"]["status"].startswith(("missed:", "no_call:"))]
    share = len(bad) / len(term) if term else 0.0
    tot = sum(weight(r) for r in ct)
    up = sum(weight(r) for r in ct if r["settled"]["side"] == 1)
    side_share = max(up, tot - up) / tot if ct else None
    return {"schema": STATE_SCHEMA, "asof_utc": rec.iso(now), "state": state,
            "calls_enabled": state != "RETIRED" and not pauses, "pause_reasons": sorted(pauses),
            "paused_display": share > 0.15, "missed_share_40": share, "n": float(tot),
            "n_trades": len(ct), "n_clusters": len(clusters(ct)), "E": E, "E_star": E_STAR, "K": K,
            "first_day": first_day.isoformat() if first_day else None,
            "last_processed_day": max(processed_days).isoformat() if processed_days else
            (view.read_json(paths.STATE_PATH) or {}).get("last_processed_day"),
            "side_share": side_share, "side_label": bool(side_share is not None and side_share > 0.85),
            "judge_verdict": {"value": (verdict or {}).get("analyst_verdict"),
                              "asof_utc": (verdict or {}).get("asof_utc"), "fetched": verdict is not None,
                              "fresh": fresh is not None,
                              "last_fresh_asof_utc": rec.iso(last_fresh) if last_fresh else None},
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
    ct_all = counted(rows)
    ct_ids = {r["cid"] for r in ct_all}
    last_of_cluster = {cl[-1]["cid"] for cl in clusters(ct_all)}
    out = io.StringIO()
    wr = csv.writer(out, lineterminator="\n")
    wr.writerow(LEDGER_COLS)
    trade_no, n_w = 0, 0.0
    promo = [e for e in events(view) if e["event"] == "PROMOTED"]
    pday = dt.date.fromisoformat(promo[0]["model_day"]) if promo else None
    seen, post = [], []
    for r in rows:
        t, c, s, tw = r["terminal"], r["call"] or {}, r["settled"], r["twin"]
        o = (s or {}).get("outcome") or {}
        opp = (s or {}).get("opposite") or {}
        plan_ = c.get("plan") or {}
        cnt = r["cid"] in ct_ids
        tn = nw = E = K = None
        if cnt:
            trade_no += 1
            n_w += weight(r)
            tn, nw = trade_no, n_w
            seen.append(r)
            if pday and r["day"] > pday:
                post.append(r)
            if r["cid"] in last_of_cluster:                  # E and K on a cluster's last counted row (R-AN-58)
                E = e_of(seen)
                if pday and r["day"] > pday:
                    K = k_of(post)
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
                     _num((s or {}).get("weight")), (s or {}).get("cluster") or "", (s or {}).get("x_end_utc") or "",
                     (s or {}).get("closed_by") or "",
                     _num(tn), _num(nw), _num(E), _num(K), _num((tw or {}).get("d_W")), (tw or {}).get("status") or "",
                     _num(opp.get("net_usd")), _num(opp.get("R")), (s or {}).get("settled_utc") or "",
                     r["terminal_sha256"], r["call_sha256"] or "", r["settled_sha256"] or ""])
    return out.getvalue().encode("utf-8")
