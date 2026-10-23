"""The e-process is the lab's, unchanged (prereg §6; register R-AN-52), and its null behaviour (prereg §10)."""
import hashlib
import importlib.util
import math
from pathlib import Path

import numpy as np

import ga  # noqa: F401
import eproc

ROOT = Path(__file__).resolve().parents[1]


def test_reuse_equality_with_the_lab():
    assert hashlib.sha256((ROOT / "analyst/vendor/eproc.py").read_bytes()).hexdigest() == \
        "ee8d456ae3b5ddd1112bad92d12fffa95d2b12e0559335233c6a9b3a68067973"
    assert eproc.LAMBDAS == (0.025, 0.05, 0.1, 0.2, 0.4)
    assert eproc.alpha_version(3) == 0.0025 and 1 / eproc.alpha_version(3) == 400.0
    rng = np.random.default_rng(7)
    for n in (1, 2, 17, 300):
        y = np.clip(rng.normal(0, 1, n), -2, 2)
        direct = np.mean([np.prod(1 + lam * y) for lam in eproc.LAMBDAS])
        assert math.isclose(eproc.e_value(y), direct, rel_tol=1e-12)
        assert math.isclose(eproc.k_value(y), np.mean([np.prod(1 - lam * y) for lam in eproc.LAMBDAS]),
                            rel_tol=1e-12)
    assert eproc.e_value([]) == 1.0
    assert math.isclose(min(1 + lam * -2 for lam in eproc.LAMBDAS), 0.2)   # every factor >= 0.2
    # the state module uses exactly this e-process
    from ga import state
    assert state._eproc() is eproc and state.E_STAR == 400.0


def load_tool():
    spec = importlib.util.spec_from_file_location("null_sim", ROOT / "tools" / "null_sim.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_null_simulation_ville_bound():
    """200,000 null paths of length 500 (50,000 for each of 4 nulls, fat-tailed and clipped included):
    the exact share that ever reaches E = 400 and its exact 95% upper bound are both <= 0.0025."""
    sim = load_tool()
    res = sim.run(paths_per_null=50000, length=500, seed=20261023)
    print("NULL_SIM", {k: (v["hits"], v["paths"], round(v["upper95"], 6)) for k, v in res["nulls"].items()},
          "total", res["total_hits"], "/", res["total_paths"], "upper95", res["upper95"])
    assert res["total_paths"] == 200000
    assert res["pass"]
    assert res["upper95"] <= 0.0025


def test_the_simulation_has_power_under_a_large_edge():
    sim = load_tool()
    rng = np.random.default_rng(3)
    y = np.clip(rng.normal(0.45, 1.0, (2000, 60)), -2, 2)
    assert (sim.max_e(y) >= 400).mean() > 0.2
