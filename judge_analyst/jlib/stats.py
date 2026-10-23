"""The test's arithmetic, recomputed independently: E_n and K (R-AN-52, lab L2 §9.1/§9.4 formulas), promotion
(R-AN-53), demotion (R-AN-54), retirement (R-AN-55) and the latched state events (R-AN-56)."""
from __future__ import annotations

import datetime as dt

from .clock import add_months_12, busday_count

LAMBDAS = (0.025, 0.05, 0.1, 0.2, 0.4)
E_STAR = 400.0


def e_path(ys, sign: float = 1.0):
    """E (sign +1) or K (sign −1) after each trade: mean over Λ of Π(1 + sign·λ·y_i)."""
    prods = [1.0] * len(LAMBDAS)
    out = []
    for y in ys:
        for j, lam in enumerate(LAMBDAS):
            prods[j] *= 1.0 + sign * lam * y
        out.append(sum(prods) / len(LAMBDAS))
    return out


def e_value(ys) -> float:
    p = e_path(ys)
    return p[-1] if p else 1.0


def k_value(ys) -> float:
    p = e_path(ys, -1.0)
    return p[-1] if p else 1.0


def promotion(trades, twins, first_day: dt.date, e: dt.date):
    """P1-P5 over `trades` (counted, model day <= e, in window order). `twins` = [(model_day, W, d_W)] of final twins.
    Returns dict with P1..P5, n, E."""
    n = len(trades)
    ys = [t["y"] for t in trades]
    E = e_value(ys)
    out = {"n": n, "E": E}
    out["P1"] = E >= E_STAR
    span = (trades[-1]["model_day"] - trades[0]["model_day"]).days if n else 0
    out["P2"] = n >= 60 and span >= 56
    out["P3"] = bool(n) and sum(t["stress"] for t in trades) > 0 and \
        sum(t["stress"] - 0.5 * t["cost"] for t in trades) > 0
    mu = {}
    for w in ("W1", "W2"):
        xs = [x for (d, ww, x) in twins if ww == w and first_day <= d <= e and x is not None]
        mu[w] = sum(xs) / len(xs) if xs else None
    if n and all(mu[t["w"]] is not None for t in trades):
        out["P4"] = sum(t["net"] - t["side"] * mu[t["w"]] for t in trades) > 0
    else:
        out["P4"] = False
    Rs = [t["R"] for t in trades]
    out["P5"] = bool(n) and (sum(Rs) - max(Rs)) > 0
    out["all"] = all(out[k] for k in ("P1", "P2", "P3", "P4", "P5"))
    out["mu"] = mu
    return out


def demotion(post):
    """(reason or None, K) over post-promotion trades in order."""
    K = k_value([t["y"] for t in post])
    Rs = [t["R"] for t in post]
    if K >= 10.0:
        return "D1", K
    if Rs and sum(Rs) <= -5.0:
        return "D2", K
    for m in range(100, len(Rs) + 1, 100):
        if sum(Rs[:m]) <= 0:
            return "D3", K
    return None, K


def weekdays_between(a: dt.date, b: dt.date) -> int:
    """R-V1-35: np.busday_count(a + 1 day, b + 1 day)."""
    return busday_count(a + dt.timedelta(days=1), b + dt.timedelta(days=1))


def replay_states(trades, twins, first_day: dt.date | None, processed_days):
    """Recompute the latched TESTING/PROMOTED/DEMOTED/RETIRED events (P7 assumed true; PAUSED/RESUMED and RETIRED
    `violation` are not replayed). Returns [(event, reason, model_day, n, E, K)]."""
    events = []
    if first_day is None:
        return events
    state = "SHAKEDOWN"
    promo_day = demo_day = None
    for e in sorted(processed_days):
        if e < first_day:
            continue
        upto = [t for t in trades if t["model_day"] <= e]
        n = len(upto)
        E = e_value([t["y"] for t in upto])
        post = [t for t in upto if promo_day is not None and t["model_day"] > promo_day]
        K = k_value([t["y"] for t in post]) if promo_day is not None else 1.0
        if state == "SHAKEDOWN":
            state = "TESTING"
            events.append(("TESTING", "first_day", e, n, E, K))
        if state == "TESTING":
            p = promotion(upto, twins, first_day, e)
            if p["all"]:
                state, promo_day = "PROMOTED", e
                events.append(("PROMOTED", "P1-P5,P7", e, n, E, 1.0))
            elif n >= 150 and sum(t["R"] for t in upto) <= 0:
                state = "RETIRED"
                events.append(("RETIRED", "n150", e, n, E, K))
            elif e >= add_months_12(first_day):
                state = "RETIRED"
                events.append(("RETIRED", "age", e, n, E, K))
        elif state == "PROMOTED":
            reason, K = demotion(post)
            if reason:
                state, demo_day = "DEMOTED", e
                events.append(("DEMOTED", reason, e, n, E, K))
        elif state == "DEMOTED":
            if weekdays_between(demo_day, e) >= 5:
                state = "RETIRED"
                events.append(("RETIRED", "demoted", e, n, E, K))
        if state == "RETIRED":
            break
    return events
