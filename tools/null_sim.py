"""Null simulation of the analyst's e-process (prereg §10: P(max_n E_n >= 400) <= 0.0025 under H0).

E_n = mean over Λ = {0.025, 0.05, 0.1, 0.2, 0.4} of Π_{i<=n} (1 + λ·y_i), the vendored lab e-process; y is clipped to
[−2, 2]. Nulls (each with E[y] <= 0): two-point stop/target (−1 w.p. 2/3, +2 w.p. 1/3), clipped normal, clipped
Student t3 (fat tails), and a slightly negative drift. Reports the exact count of paths that ever reach 400 and an
exact (Clopper-Pearson) 95% upper bound on the probability.

Usage: uv run python -I tools/null_sim.py [--paths-per-null 50000] [--length 500] [--seed 20261023]
"""
import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analyst"))
import ga  # noqa: E402,F401
import eproc  # noqa: E402  (analyst/vendor/eproc.py)

E_STAR = 400.0
ALPHA = 0.0025
NULLS = ("two_point", "normal_clipped", "t3_clipped", "negative_drift")


def draw(name, rng, shape):
    if name == "two_point":
        return np.where(rng.random(shape) < 1 / 3, 2.0, -1.0)
    if name == "normal_clipped":
        return np.clip(rng.normal(0.0, 1.0, shape), -2, 2)
    if name == "t3_clipped":
        return np.clip(rng.standard_t(3, shape) / math.sqrt(3.0), -2, 2)
    if name == "negative_drift":
        return np.clip(rng.normal(-0.05, 1.0, shape), -2, 2)
    raise ValueError(name)


def max_e(y):
    """max over n of E_n, for each row of y (paths x length)."""
    tot = np.zeros(y.shape)
    for lam in eproc.LAMBDAS:
        tot += np.exp(np.cumsum(np.log1p(lam * y), axis=1))
    return (tot / len(eproc.LAMBDAS)).max(axis=1)


def binom_upper(k, n, conf=0.95):
    """Exact one-sided upper confidence bound for a binomial proportion (bisection on the CDF)."""
    if k >= n:
        return 1.0

    def cdf(p):
        s = 0.0
        for i in range(k + 1):
            s += math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
                          + i * math.log(p) + (n - i) * math.log1p(-p))
        return s

    lo, hi = 0.0, 1.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if cdf(mid) > 1 - conf:
            lo = mid
        else:
            hi = mid
    return hi


def run(paths_per_null=50000, length=500, seed=20261023, chunk=5000):
    rng = np.random.default_rng(seed)
    out = {"paths_per_null": paths_per_null, "length": length, "seed": seed, "E_star": E_STAR, "alpha": ALPHA,
           "lambdas": list(eproc.LAMBDAS), "nulls": {}}
    total = hits = 0
    for name in NULLS:
        k, mx = 0, 0.0
        done = 0
        while done < paths_per_null:
            m = min(chunk, paths_per_null - done)
            y = draw(name, rng, (m, length))
            if done == 0:                                   # the vectorised path equals eproc.e_path
                for j in range(3):
                    ref = eproc.e_path(y[j])
                    tot = sum(np.exp(np.cumsum(np.log1p(lam * y[j]))) for lam in eproc.LAMBDAS) / 5
                    assert np.allclose(tot, ref, rtol=1e-10, atol=0)
            e = max_e(y)
            k += int((e >= E_STAR).sum())
            mx = max(mx, float(e.max()))
            done += m
        out["nulls"][name] = {"paths": paths_per_null, "hits": k, "rate": k / paths_per_null,
                              "upper95": binom_upper(k, paths_per_null), "max_E": mx}
        total += paths_per_null
        hits += k
    out["total_paths"] = total
    out["total_hits"] = hits
    out["rate"] = hits / total
    out["upper95"] = binom_upper(hits, total)
    out["pass"] = out["upper95"] <= ALPHA and all(v["rate"] <= ALPHA for v in out["nulls"].values())
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--paths-per-null", type=int, default=50000)
    ap.add_argument("--length", type=int, default=500)
    ap.add_argument("--seed", type=int, default=20261023)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    res = run(a.paths_per_null, a.length, a.seed)
    text = json.dumps(res, indent=1, sort_keys=True) + "\n"
    if a.out:
        Path(a.out).write_text(text)
    print(text)
    return 0 if res["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
