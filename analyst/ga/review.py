"""Reviews: the open set, the options and the `review` block (register R-AN-92, §16.18 A15). FROZEN.

One rule, used by `context` (the block), `lock` (the options), analyst-settle (the managed exits, R-AN-93) and the
tests. For model days d >= MODE.json a15_from, in both modes, each mode's records read separately:
- a window w of d opens a trade iff its call status is `called` (plan action buy, sell, reverse or add);
- a window C whose call status is `review_close`, or `called` with plan action `reverse`, closes every trade of O(C)
  that its context's `review.open` names (a trade the analyst was not shown is never closed);
- O(R) = the trades w of d earlier than R in window order with eff_exit(w) > entry_utc(R), where eff_exit(w) is the
  entry of the window that closed w, else exit_utc(w). A stop, a target or a gap never shortens eff_exit (the
  session cannot see Dukascopy's minutes); a window whose entry equals eff_exit(w) is not a review of w;
- R is a review iff O(R) is not empty; options: one open trade hold, close, reverse, add; two hold, close; a window
  that is not a review has buy, sell, stay_out (R-AN-26);
- a review that ends `no_call:*`, `missed:*` or `skipped:*` changes nothing: its open trades run on, as under hold.

The `review` block (`analyst-context/5`, right after `wake`): {open, options, ref_px, ref_utc}, from M = the bars of
the `XAUUSD_M30R` request (empty when it was not made or is not `ok`); every value as recorded, each expression
evaluated as written in IEEE doubles, never rounded. These prices are Twelve Data's and indicative: settlement uses
Dukascopy minutes.

Window entries passed to open_sets are dicts in window order: {call_id, entry_utc, exit_utc (aware UTC), status (the
call status the reader sees: the terminal's, else the call file's; None without either), action (the plan's action
or None), side, S, T, stop_mult, target_mult (the call file's), names (the call_ids its context's review.open lists),
kind (the window's kind; absent means not a time window)}.

§16.18 A17 (A17-R2, A17-R5, A17-R7): eff_exit(w) = the entry of the window that closed w; else day_end(d) when w's
time window (kind `time`, entering at exit_utc(w)) has status `review_extend` and names w; else exit_utc(w). A time
window closes nothing, opens nothing and is a review of no trade (its subject is not in O(X): its eff_exit equals X's
entry until X decides); any other status of it changes nothing. time_subject gives the subject of a check T: the
trade of d with exit_utc = T + 30 min, eff_exit = exit_utc for a window entering at T + 30 min, and exit_utc <=
day_end(d) − 60 min. The `analyst-context/6` block is {kind, open, options, ref_px, ref_utc, extend_until_utc}:
kind `review` (as `/5`, extend_until_utc null) or `time` (open = [the subject], options extend and close,
extend_until_utc = day_end(d)); each open element gains `extended` after `exit_utc`, and its `exit_utc` and
`minutes_left` use the trade's current exit (day_end(d) when extended).
"""
import datetime as dt

from . import clock, rec

REQ = "XAUUSD_M30R"
HALF = dt.timedelta(minutes=30)
NORMAL_ACTIONS = ("buy", "sell", "stay_out")
ONE_OPEN = ("hold", "close", "reverse", "add")
TWO_OPEN = ("hold", "close")
REVIEW_ACTIONS = ONE_OPEN
TRADE_ACTIONS = ("buy", "sell", "reverse", "add")
REVIEW_STATUSES = ("review_hold", "review_close")
TIME_OPTIONS = ("extend", "close")                          # §16.18 A17-R5
EXTEND_STATUS = "review_extend"
MIN_EXTENSION = dt.timedelta(minutes=60)                    # A17-D3
OPEN_KEYS = ("call_id", "action", "side", "entry_utc", "exit_utc", "stop_usd", "target_usd", "stop_mult",
             "target_mult", "entry_px", "move_usd", "move_r", "high_since_entry", "low_since_entry", "stop_touched",
             "target_touched", "minutes_left")


def options(n_open):
    """The options of a window with `n_open` open trades, in R-AN-92's order."""
    if n_open <= 0:
        return NORMAL_ACTIONS
    return ONE_OPEN if n_open == 1 else TWO_OPEN


def is_time(entry):
    """True for a time window's entry (§16.18 A17)."""
    return entry.get("kind") == "time"


def closes(entry):
    """True when this window closes the trades of its open set that its context named (review_close, or a called
    reverse); a time window closes nothing (§16.18 A17-R7)."""
    if is_time(entry):
        return False
    return entry.get("status") == "review_close" or (entry.get("status") == "called"
                                                     and entry.get("action") == "reverse")


def open_state(entries):
    """(O, eff, closed_by, extended_by) over `entries` (one model day, window order): {call_id: O(call_id)},
    {trade call_id: eff_exit}, {trade call_id: closing call_id}, {trade call_id: the time window that extended it}
    (§16.18 A17-R7). O(R) lists the open trades' entries in window order."""
    trades, eff, closed_by, extended_by, out = [], {}, {}, {}, {}
    for r in entries:
        e = r["entry_utc"]
        o = [t for t in trades if eff[t["call_id"]] > e]
        out[r["call_id"]] = [] if is_time(r) else o          # a time window is a review of no trade (A17-R7)
        if closes(r):
            names = set(r.get("names") or ())
            for t in o:
                if t["call_id"] in names:
                    eff[t["call_id"]] = e
                    closed_by[t["call_id"]] = r["call_id"]
        if is_time(r):
            if r.get("status") == EXTEND_STATUS:
                names = set(r.get("names") or ())
                for t in trades:
                    cid = t["call_id"]
                    if cid in names and cid not in extended_by and eff[cid] == e and t["exit_utc"] == e:
                        eff[cid] = clock.day_end(clock.tday(e))
                        extended_by[cid] = r["call_id"]
            continue                                     # a time window opens nothing
        if r.get("status") == "called":
            trades.append(r)
            eff[r["call_id"]] = r["exit_utc"]
    return out, eff, closed_by, extended_by


def open_sets(entries):
    """({call_id: O(call_id)}, {trade call_id: eff_exit}, {trade call_id: closing call_id}) over `entries` (one model
    day, window order). O(R) lists the open trades' entries in window order."""
    out, eff, closed_by, _ext = open_state(entries)
    return out, eff, closed_by


def time_subject(entries, T, d):
    """The subject of check T of model day d (§16.18 A17-R2): the trade entry of `entries` (the day's windows in
    window order) with exit_utc = T + 30 min whose eff_exit, for a window entering at T + 30 min, equals its exit_utc,
    if that exit is at most day_end(d) − 60 min; else None."""
    e = T + HALF
    before = [r for r in entries if r["entry_utc"] < e]
    _o, eff, _c, _x = open_state(before)
    for t in before:
        if not is_time(t) and t.get("status") == "called" and t["exit_utc"] == e and eff.get(t["call_id"]) == e \
                and e <= clock.day_end(d) - MIN_EXTENSION:
            return t
    return None


def reviews_of(entries, o_sets):
    """{trade call_id: [the entries C of the day with the trade in O(C)], in window order}."""
    out = {}
    for r in entries:
        for t in o_sets.get(r["call_id"], []):
            out.setdefault(t["call_id"], []).append(r)
    return out


def side_word(side):
    return "buy" if side > 0 else "sell"


def side_value(word):
    return {"buy": 1, "sell": -1}[word]


def needs_request(open_, asof):
    """The `XAUUSD_M30R` request is made iff O(R) is not empty and some trade has entry_utc + 30 min <= asof_utc."""
    return any(t["entry_utc"] + HALF <= asof for t in open_)


def request_range(open_, asof):
    """(A, B): the earliest entry_utc of O(R), and asof floored to the half hour."""
    a = min(t["entry_utc"] for t in open_)
    b = asof.replace(minute=(asof.minute // 30) * 30, second=0, microsecond=0)
    return a, b


def used_bars(values, a, asof):
    """The bars of M: every element with A <= t_utc and t_utc + 30 min <= asof_utc, in time order."""
    return sorted((v for v in values if a <= v[0] and v[0] + HALF <= asof), key=lambda v: v[0])


def _touch(side, hi, lo, px, stop, target):
    if None in (hi, lo, px):
        return None, None
    if side > 0:
        return lo <= px - stop, hi >= px + target
    return hi >= px + stop, lo <= px - target


def block(open_, e_r, bars, kind=None, cur=None, extend_until=None):
    """The `review` block of a window entering at `e_r` with open set `open_`; `bars` = M as (t, o, h, l, c). `kind`
    None gives `analyst-context/5`'s block; `review` or `time` gives `/6`'s (§16.18 A17-R5), with `cur` = {call_id:
    (current exit, extended)} and, for `time`, `extend_until` = day_end(d)."""
    if not open_:
        return None
    ref_px = bars[-1][4] if bars else None
    ref_t = bars[-1][0] + HALF if bars else None
    out = []
    for t in open_:
        s = t["side"]
        e = t["entry_utc"]
        entry_px = next((b[1] for b in bars if b[0] == e), None)
        move = mr = None
        if entry_px is not None and ref_px is not None and ref_t > e:
            move = s * (ref_px - entry_px)
            mr = move / t["S"]
        since = [b for b in bars if b[0] >= e]
        hi = max(b[2] for b in since) if since else None
        lo = min(b[3] for b in since) if since else None
        st, tg = _touch(s, hi, lo, entry_px, t["S"], t["T"])
        x, ext = (cur or {}).get(t["call_id"], (t["exit_utc"], False))
        el = {"call_id": t["call_id"], "action": t["action"], "side": side_word(s), "entry_utc": rec.iso(e),
              "exit_utc": rec.iso(x)}
        if kind is not None:
            el["extended"] = bool(ext)
        el.update({"stop_usd": t["S"], "target_usd": t["T"], "stop_mult": t["stop_mult"],
                   "target_mult": t["target_mult"], "entry_px": entry_px, "move_usd": move, "move_r": mr,
                   "high_since_entry": hi, "low_since_entry": lo, "stop_touched": st, "target_touched": tg,
                   "minutes_left": int((x - e_r).total_seconds() // 60)})
        out.append(el)
    opts = list(TIME_OPTIONS) if kind == "time" else list(options(len(open_)))
    body = {"open": out, "options": opts, "ref_px": ref_px, "ref_utc": rec.iso(ref_t) if ref_t is not None else None}
    if kind is None:
        return body
    return {"kind": kind, **body, "extend_until_utc": rec.iso(extend_until) if kind == "time" else None}


def allowed_actions(ctx):
    """The actions `lock` accepts for a context: one of review.options in a review, else buy, sell, stay_out."""
    rv = (ctx or {}).get("review")
    return tuple(rv["options"]) if isinstance(rv, dict) else NORMAL_ACTIONS


def entry_from_call(cid, w, status, call):
    """One open_sets entry from a window's call status and call file (None when the call cannot say its side)."""
    call = call if isinstance(call, dict) else {}
    plan_ = call.get("plan") if isinstance(call.get("plan"), dict) else {}
    ctx = call.get("context") if isinstance(call.get("context"), dict) else {}
    rv = ctx.get("review") if isinstance(ctx.get("review"), dict) else {}
    names = [o.get("call_id") for o in (rv.get("open") or []) if isinstance(o, dict)]
    out = {"call_id": cid, "entry_utc": w.entry_utc, "exit_utc": w.exit_utc, "status": status,
           "action": plan_.get("action"), "side": call.get("side"), "S": call.get("S"), "T": call.get("T"),
           "stop_mult": call.get("stop_mult"), "target_mult": call.get("target_mult"), "names": names,
           "kind": w.kind}
    if status == "called" and (call.get("side") not in (1, -1) or not isinstance(call.get("S"), (int, float))):
        out["status"] = None                 # a trade whose call file cannot be read opens nothing (R-AN-92 reading)
    return out


def weights(trades):
    """R-AN-94: {call_id: (weight, cluster)} over one mode's settled trades [(call_id, entry_utc, x_end_utc)] in window
    order. Two trades overlap iff entry(a) < x_end(b) and entry(b) < x_end(a); a trade that overlaps another weighs
    0.5, else 1.0; clusters are the connected components of the overlap relation, named by their first trade's
    call_id."""
    n = len(trades)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    overlaps = [False] * n
    for i in range(n):
        for j in range(i + 1, n):
            a, b = trades[i], trades[j]
            if a[1] < b[2] and b[1] < a[2]:
                overlaps[i] = overlaps[j] = True
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[max(ri, rj)] = min(ri, rj)
    return {trades[i][0]: (0.5 if overlaps[i] else 1.0, trades[find(i)][0]) for i in range(n)}
