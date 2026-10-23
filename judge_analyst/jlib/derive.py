"""What must follow from a plan and a context: plan validity (R-AN-26), S and T (R-AN-27), D_ref (R-AN-21)."""
from __future__ import annotations

import csv
import datetime as dt
import io
import math
import statistics

from .clock import busday_count

K_DREF = 1.0540339626212611
STOP_MULTS = (1.0, 1.5, 2.0)
TARGET_MULTS = (1.0, 1.5, 2.0, 3.0)
CONFIDENCE = ("low", "medium", "high")
TRADE_KEYS = {"action", "stop_mult", "target_mult", "confidence", "reason_ar", "key_risks_ar"}
STAY_KEYS = {"action", "confidence", "reason_ar", "key_risks_ar"}


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _bad_control(s: str) -> bool:
    for ch in s:
        o = ord(ch)
        if (o < 0x20 and o != 0x0A) or 0x80 <= o <= 0x9F:
            return True
    return False


def plan_errors(plan) -> list:
    """Enum reasons why `plan` is not a valid R-AN-26 plan (empty list = valid). Never returns plan text."""
    if not isinstance(plan, dict):
        return ["not_object"]
    errs = []
    action = plan.get("action")
    if action not in ("buy", "sell", "stay_out"):
        return ["action"]
    want = TRADE_KEYS if action in ("buy", "sell") else STAY_KEYS
    if set(plan.keys()) != want:
        errs.append("keys")
    if action in ("buy", "sell"):
        sm, tm = plan.get("stop_mult"), plan.get("target_mult")
        if not (_is_num(sm) and float(sm) in STOP_MULTS):
            errs.append("stop_mult")
        if not (_is_num(tm) and float(tm) in TARGET_MULTS):
            errs.append("target_mult")
    if plan.get("confidence") not in CONFIDENCE:
        errs.append("confidence")
    r = plan.get("reason_ar")
    if not (isinstance(r, str) and 1 <= len(r) <= 400) or (isinstance(r, str) and _bad_control(r)):
        errs.append("reason_ar")
    k = plan.get("key_risks_ar")
    if not (isinstance(k, str) and 0 <= len(k) <= 300) or (isinstance(k, str) and _bad_control(k)):
        errs.append("key_risks_ar")
    return errs


def side_of(action) -> int:
    return {"buy": 1, "sell": -1}.get(action, 0)


def st_dimes(d_ref_cents: int, stop_mult: float, target_mult: float):
    """R-AN-27: exact integer arithmetic, half up."""
    m_s = round(10 * float(stop_mult))
    m_t = round(10 * float(target_mult))
    s_d = (d_ref_cents * m_s + 50) // 100
    t_d = (d_ref_cents * m_t + 50) // 100
    return s_d, t_d


# ---------------------------------------------------------------- D_ref (R-AN-21)


def read_slots(data: bytes):
    """mirror/system/forecasts_slots.csv → list of row dicts (strings), in file order."""
    rd = csv.DictReader(io.StringIO(data.decode("utf-8")))
    return list(rd)


def _f_s(row):
    v = row.get("F_S", "")
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except ValueError:
        return None
    return x if (math.isfinite(x) and x > 0) else None


def dref_rows(slot_rows, slot: int, d: dt.date, asof_utc: str):
    """Eligible rows, sorted by day (stable), the last 20, as the stored row objects."""
    elig = []
    for r in slot_rows:
        try:
            if int(r["slot"]) != slot:
                continue
        except (KeyError, ValueError):
            continue
        if not (r.get("day", "") < d.isoformat()):
            continue
        if not (r.get("computed_utc", "") <= asof_utc):
            continue
        fs = _f_s(r)
        if fs is None:
            continue
        if not all(r.get(k) == "0" for k in ("nfp", "cpi", "fomc", "hol")):
            continue
        elig.append({"day": r["day"], "slot": slot, "F_S": fs, "computed_utc": r["computed_utc"]})
    elig.sort(key=lambda x: x["day"])
    return elig[-20:]


def dref_status(rows, d: dt.date):
    """None when D_ref may be computed, else the R-AN-21 detail (`rows`, `stale`)."""
    if len(rows) < 10:
        return "rows"
    newest = dt.date.fromisoformat(rows[-1]["day"])
    if busday_count(newest, d) > 5:
        return "stale"
    return None


def dref_values(rows, p_ref: float):
    """F_ref, D_raw, D_ref, D_ref_cents from the stored rows and P_ref."""
    f_ref = statistics.median([r["F_S"] for r in rows])
    d_raw = p_ref * (1 - math.exp(-K_DREF * f_ref / 1e4))
    d_ref = round(d_raw, 2)
    cents = round(d_ref * 100)
    return {"F_ref": f_ref, "D_raw": d_raw, "D_ref": d_ref, "D_ref_cents": cents}


def dref_object(slot: int, rows, p_ref: float):
    v = dref_values(rows, p_ref)
    return {"slot": slot, "rows": rows, "F_ref": v["F_ref"], "P_ref": p_ref, "D_raw": v["D_raw"], "D_ref": v["D_ref"],
            "D_ref_cents": v["D_ref_cents"], "k": K_DREF}
