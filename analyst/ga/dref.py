"""D_ref, the volatility unit of a call (prereg §2 D_ref, register R-AN-21). FROZEN.

slot s = the model hour containing the entry minute, minus 1 (W1: 9, or 10 in the mismatch weeks; W2: 15). From
`mirror/system/forecasts_slots.csv` at repo_head_sha, eligible rows have slot == s, day < d, computed_utc <= asof_utc
(string comparison), F_S non-empty, finite and > 0, and nfp, cpi, fomc and hol all equal to "0". The last 20 by day
are kept; fewer than 10 -> `rows`; busday_count(newest_day, d) > 5 -> `stale`. F_ref = statistics.median of their F_S;
D_raw = P_ref·(1 − exp(−k·F_ref/1e4)) with k = 1.0540339626212611; D_ref = round(D_raw, 2);
D_ref_cents = round(D_ref·100) (<= 0 -> `nonpositive`).
"""
import csv
import datetime as dt
import io
import math
import statistics

from . import clock

K = 1.0540339626212611
KEEP = 20
MIN_ROWS = 10
MAX_STALE_WEEKDAYS = 5


def eligible_rows(slots_text, slot, day, asof_utc_s):
    out = []
    for r in csv.DictReader(io.StringIO(slots_text)):
        try:
            if int(r["slot"]) != slot:
                continue
        except ValueError:
            continue
        if not (r["day"] < day.isoformat()):
            continue
        if not (r["computed_utc"] <= asof_utc_s):
            continue
        if r["F_S"] == "":
            continue
        f = float(r["F_S"])
        if not (math.isfinite(f) and f > 0):
            continue
        if not all(r[k] == "0" for k in ("nfp", "cpi", "fomc", "hol")):
            continue
        out.append({"day": r["day"], "slot": int(r["slot"]), "F_S": f, "computed_utc": r["computed_utc"]})
    out.sort(key=lambda x: x["day"])
    return out[-KEEP:]


def compute(slots_text, slot, day, asof_utc_s, p_ref):
    """(d_ref block, status detail or None)."""
    rows = eligible_rows(slots_text, slot, day, asof_utc_s)
    block = {"slot": slot, "rows": rows, "F_ref": None, "P_ref": p_ref, "D_raw": None, "D_ref": None,
             "D_ref_cents": None, "k": K}
    if len(rows) < MIN_ROWS:
        return block, "rows"
    newest = dt.date.fromisoformat(rows[-1]["day"])
    if clock.busday_count(newest, day) > MAX_STALE_WEEKDAYS:
        return block, "stale"
    f_ref = statistics.median([r["F_S"] for r in rows])
    d_raw = p_ref * (1.0 - math.exp(-K * f_ref / 1e4))
    d_ref = round(d_raw, 2)
    cents = int(round(d_ref * 100))
    block.update(F_ref=f_ref, D_raw=d_raw, D_ref=d_ref, D_ref_cents=cents)
    if cents <= 0:
        return block, "nonpositive"
    return block, None
