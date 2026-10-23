"""E-processes, e-LOND, promotion, demotion and retirement (L2 §9 with SYSTEM_V1_PREREG.md §3 items 7-8). FROZEN.

- Promotion e-process (L2 §9.1): E(n) = mean over Λ of Π_{i<=n} (1 + λ·y_i), Λ = {0.025, 0.05, 0.1, 0.2, 0.4},
  y = clip(R, −2, 2), final trades from first_day on, in trade order. H0: E[y | past] <= 0.
- Demotion e-process: K(m) = mean over Λ of Π_{i<=m} (1 − λ·y_i), from the trade after promotion.
- e-LOND (L2 §9.2) with the lab-v1 level α_v1 = 0.04 (§3 item 7): candidate j arriving after D_j promotions gets
  α_j = α_v1·γ_j·(D_j + 1) and E*_j = 1/α_j. γ: S1 0.30; the k-th proposal Γ_P/(k(k+1)) with Γ_P = 0.70; REF 0.
  Future lab versions v >= 2 get α_v = 0.01·2^−(v−1) (Amendment A1 item 5), so the program total is
  0.04 + 0.01 = 0.05.
- Promotion (all required): P1 E >= E*; P2 n >= 40 and the first and last counted trades >= 56 calendar days apart;
  P3 Σ net$ > 0 and Σ stress net$ > 0 (+$0.25 a trade, §3 item 2); P4 Σ(R_i − side_i·μ_ℓ) > 0 with μ_ℓ the rule's
  long drift-twin mean; P5 Σ R − max R > 0; P7 lab RUNNING. P6 is replaced by the INTERPRETER_NEEDED flag (§8).
- Demotion (L2 §9.4): D1 K >= 10; D2 Σ R since promotion <= −5; D3 at post-promotion counts 100, 200, ... Σ R <= 0.
- Retirement of PAPER candidates (L2 §9.5): n >= 150 and Σ R <= 0, or 24 months after registered_utc.
- INTERPRETER_NEEDED (§8): any promotable rule with E >= E*/4 and n >= 20, or any promotion.
"""
import numpy as np

LAMBDAS = (0.025, 0.05, 0.1, 0.2, 0.4)
ALPHA_V1 = 0.04
GAMMA_S1 = 0.30
GAMMA_POOL = 0.70
MIN_TRADES = 40
MIN_SPAN_DAYS = 56
DEMOTE_K = 10.0
DEMOTE_CUM_R = -5.0
DEMOTE_EVERY = 100
RETIRE_N = 150
RETIRE_MONTHS = 24
MAX_ACTIVE_PROPOSALS = 10


def alpha_version(v):
    """Lifetime FDR level of lab version v: v1 = 0.04; v >= 2: 0.01·2^−(v−1) (A1 item 5), summing to 0.01."""
    if v < 1 or int(v) != v:
        raise ValueError(v)
    return ALPHA_V1 if v == 1 else 0.01 * 2.0 ** -(v - 1)


def gamma_proposal(k):
    return GAMMA_POOL / (k * (k + 1))


def alpha_j(gamma, D, alpha=ALPHA_V1):
    return alpha * gamma * (D + 1)


def e_star(gamma, D, alpha=ALPHA_V1):
    a = alpha_j(gamma, D, alpha)
    return float("inf") if a <= 0 else 1.0 / a


def e_path(y, lambdas=LAMBDAS, sign=1.0):
    """E (sign +1) or K (sign −1) after each trade: array of length len(y)."""
    y = np.asarray(y, dtype=float)
    if len(y) == 0:
        return np.zeros(0)
    f = 1.0 + sign * np.outer(np.asarray(lambdas), y)
    return np.cumprod(f, axis=1).mean(axis=0)


def e_value(y, lambdas=LAMBDAS):
    p = e_path(y, lambdas)
    return float(p[-1]) if len(p) else 1.0


def k_value(y, lambdas=LAMBDAS):
    p = e_path(y, lambdas, sign=-1.0)
    return float(p[-1]) if len(p) else 1.0


def promotion_checks(trades, E_star, mu_long, lab_running):
    """trades: list of dicts with day, side, y, R, net_usd, net_stress_usd (final, from first_day, in order).

    Returns {P1..P5, P7, all, n, E}."""
    n = len(trades)
    y = [t["y"] for t in trades]
    E = e_value(y)
    R = np.array([t["R"] for t in trades], dtype=float)
    out = {"n": n, "E": E, "E_star": E_star}
    out["P1"] = bool(E >= E_star)
    span = 0
    if n:
        import datetime as dt
        d0 = dt.date.fromisoformat(str(trades[0]["day"])[:10])
        d1 = dt.date.fromisoformat(str(trades[-1]["day"])[:10])
        span = (d1 - d0).days
    out["P2"] = bool(n >= MIN_TRADES and span >= MIN_SPAN_DAYS)
    net = np.array([t["net_usd"] for t in trades], dtype=float)
    stress = np.array([t["net_stress_usd"] for t in trades], dtype=float)
    out["P3"] = bool(n and net.sum() > 0 and stress.sum() > 0)
    side = np.array([t["side"] for t in trades], dtype=float)
    out["P4"] = bool(n and np.isfinite(mu_long) and (R - side * mu_long).sum() > 0)
    out["P5"] = bool(n and (R.sum() - R.max()) > 0)
    out["P7"] = bool(lab_running)
    out["all"] = all(out[k] for k in ("P1", "P2", "P3", "P4", "P5", "P7"))
    return out


def demotion_check(post_R, post_y):
    """Post-promotion trades in order. Returns (reason or None, K)."""
    K = k_value(post_y)
    R = np.asarray(post_R, dtype=float)
    if K >= DEMOTE_K:
        return "D1", K
    if len(R) and R.sum() <= DEMOTE_CUM_R:
        return "D2", K
    for m in range(DEMOTE_EVERY, len(R) + 1, DEMOTE_EVERY):
        if R[:m].sum() <= 0:
            return "D3", K
    return None, K


def retirement_check(n, sum_R, registered_utc, now_utc):
    import pandas as pd
    if n >= RETIRE_N and sum_R <= 0:
        return "n150"
    if registered_utc and pd.Timestamp(now_utc) >= pd.Timestamp(registered_utc) + pd.DateOffset(months=RETIRE_MONTHS):
        return "age"
    return None


def interpreter_needed(E, E_star, n, promotable, ever_promoted):
    return bool(ever_promoted or (promotable and n >= 20 and np.isfinite(E_star) and E >= E_star / 4))
