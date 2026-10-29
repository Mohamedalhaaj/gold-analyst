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
or None), side, S, T, stop_mult, target_mult (the call file's), names (the call_ids its context's review.open lists)}.
"""
import datetime as dt

from . import rec

REQ = "XAUUSD_M30R"
HALF = dt.timedelta(minutes=30)
NORMAL_ACTIONS = ("buy", "sell", "stay_out")
ONE_OPEN = ("hold", "close", "reverse", "add")
TWO_OPEN = ("hold", "close")
REVIEW_ACTIONS = ONE_OPEN
TRADE_ACTIONS = ("buy", "sell", "reverse", "add")
REVIEW_STATUSES = ("review_hold", "review_close")
OPEN_KEYS = ("call_id", "action", "side", "entry_utc", "exit_utc", "stop_usd", "target_usd", "stop_mult",
             "target_mult", "entry_px", "move_usd", "move_r", "high_since_entry", "low_since_entry", "stop_touched",
             "target_touched", "minutes_left")


def options(n_open):
    """The options of a window with `n_open` open trades, in R-AN-92's order."""
    if n_open <= 0:
        return NORMAL_ACTIONS
    return ONE_OPEN if n_open == 1 else TWO_OPEN


def closes(entry):
    """True when this window closes the trades of its open set that its context named (review_close, or a called
    reverse)."""
    return entry.get("status") == "review_close" or (entry.get("status") == "called"
                                                     and entry.get("action") == "reverse")


def open_sets(entries):
    """({call_id: O(call_id)}, {trade call_id: eff_exit}, {trade call_id: closing call_id}) over `entries` (one model
    day, window order). O(R) lists the open trades' entries in window order."""
    trades, eff, closed_by, out = [], {}, {}, {}
    for r in entries:
        e = r["entry_utc"]
        o = [t for t in trades if eff[t["call_id"]] > e]
        out[r["call_id"]] = o
        if closes(r):
            names = set(r.get("names") or ())
            for t in o:
                if t["call_id"] in names:
                    eff[t["call_id"]] = e
                    closed_by[t["call_id"]] = r["call_id"]
        if r.get("status") == "called":
            trades.append(r)
            eff[r["call_id"]] = r["exit_utc"]
    return out, eff, closed_by


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


def block(open_, e_r, bars):
    """The `review` block of a window entering at `e_r` with open set `open_`; `bars` = M as (t, o, h, l, c)."""
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
        out.append({"call_id": t["call_id"], "action": t["action"], "side": side_word(s), "entry_utc": rec.iso(e),
                    "exit_utc": rec.iso(t["exit_utc"]), "stop_usd": t["S"], "target_usd": t["T"],
                    "stop_mult": t["stop_mult"], "target_mult": t["target_mult"], "entry_px": entry_px,
                    "move_usd": move, "move_r": mr, "high_since_entry": hi, "low_since_entry": lo,
                    "stop_touched": st, "target_touched": tg,
                    "minutes_left": int((t["exit_utc"] - e_r).total_seconds() // 60)})
    return {"open": out, "options": list(options(len(open_))), "ref_px": ref_px,
            "ref_utc": rec.iso(ref_t) if ref_t is not None else None}


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
           "stop_mult": call.get("stop_mult"), "target_mult": call.get("target_mult"), "names": names}
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
