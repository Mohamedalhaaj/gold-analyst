"""Retirement by time, not by the order of runs (register R-AN-55 step 2, R-AN-62, R-AN-70; §16.18 A6 sixth review,
blocker R6-2; prereg §8: "a new VIOLATION after such a clear retires the analyst").

Before, "armed" meant that a RESUMED `clear:` event of a live integrity pause existed, and each run applied clears
before keys. So one late analyst-settle run that applied the owner's clear of K1 and also saw K2, which the judge had
published before that clear, retired the analyst, while a run in between would only have paused K2: GitHub's start
time of the run decided a permanent outcome. Now:
- the verdict carries `violation_first_seen` {key: the `first_seen_utc` of the key in the judge's FINDINGS.csv};
- a live integrity key with no PAUSED event retires the analyst only when it was first seen strictly after A, the
  arming time: the earliest clear time among the live integrity pauses the owner cleared (this run's clears
  included), a pause's clear time being the `utc` of its earliest clear row dated at or after its PAUSED event;
- otherwise it pauses. A key without a usable first-seen time (missing, not an R-C4 time, or later than the
  verdict's `asof_utc`) pauses and never retires.

The first two tests are the sixth review's probe (scratchpad verify5/r6/probes_ga/test_r6_ga.py), with each verdict
carrying the first-seen times the judge's FINDINGS.csv gives.
"""
import datetime as dt
import json

import pytest

from ga import paths, rec, state
from ga.repo import DictView

UTC = rec.UTC
JV = "judge_violation:"
K1 = "A1|call_bytes|analyst/calls/2026-12-01_W1.json|" + "a" * 40                 # live integrity key, paused
K2 = "A10|in_span_without_attempt|analyst/runs/2026-W49.json|" + "b" * 40        # found at the 23:40 judge run
K3 = "A3|agent_scope|inbox/refs/heads/claude/analyst-z|" + "e" * 40
MODE = {"mode": "live", "canary_from": "2026-11-23", "canary_to": "2026-11-27", "live_from": "2026-11-30"}


def T(*a):
    return dt.datetime(*a, tzinfo=UTC)


def V(*keys, asof, seen=None):
    """A verdict as the judge publishes it: `seen` maps keys to their FINDINGS.csv first_seen_utc."""
    return {"schema": "analyst-verdict/1", "asof_utc": rec.iso(asof),
            "analyst_verdict": "VIOLATION" if keys else "SOUND", "violation_keys": list(keys), "cleared_keys": [],
            "violation_first_seen": {k: (rec.iso(t) if isinstance(t, dt.datetime) else t)
                                     for k, t in (seen or {}).items()}}


class Book:
    def __init__(self, mode=MODE):
        self.view = DictView({paths.MODE_PATH: json.dumps(mode)})
        self.n = 0

    def row(self, utc, key):
        b = (self.view.read("analyst/ADJUDICATIONS.csv") or b"utc,ref,subject,decision,note\n").decode()
        self.view.files["analyst/ADJUDICATIONS.csv"] = (b + f"{utc},pause,{JV}{key},clear,owner\n").encode()

    def settle(self, now, v):
        self.n += 1
        new = []

        def write(p, b):
            self.view.files[p] = b
            new.append(json.loads(b))
        st = state.run_state(self.view, [], now, self.n, "h", v, write)
        self.view.files[paths.STATE_PATH] = rec.record_bytes(st)
        return st, [(e["event"], e["reason"]) for e in new]


K1_SEEN = T(2026, 12, 1, 11, 15)


def scenario(export_settle_starts_at, k2_seen=T(2026, 12, 2, 1, 30), row_at="2026-12-02T07:00:00Z"):
    """K1 paused on 12-01. The judge's 23:40 run (started 01:30) publishes K1 and K2. The owner adjudicates K1 as a
    false positive and writes its clear row at 07:00 on 12-02 (K2 still under review). The 05:10 export, and the
    settle it starts, run at `export_settle_starts_at`."""
    b = Book()
    b.settle(T(2026, 12, 1, 11, 20), V(K1, asof=K1_SEEN, seen={K1: K1_SEEN}))                     # K1 paused
    owner_row_at = rec.parse_iso(row_at)
    asof = max(k2_seen, T(2026, 12, 2, 1, 30))
    if export_settle_starts_at >= owner_row_at:
        b.row(row_at, K1)
    st, new = b.settle(export_settle_starts_at, V(K1, K2, asof=asof, seen={K1: K1_SEEN, K2: k2_seen}))
    if export_settle_starts_at < owner_row_at:
        b.row(row_at, K1)
    return b, st, new


# ---------------------------------------------------------------- the sixth review's probe
def test_control_settle_on_time_k2_found_before_the_clear_only_pauses():
    b, st, new = scenario(T(2026, 12, 2, 5, 15))       # before the owner's 07:00 row
    assert new == [("PAUSED", JV + K2)] and st["state"] != "RETIRED"
    st, new = b.settle(T(2026, 12, 2, 11, 15), V(K1, K2, asof=T(2026, 12, 2, 5, 30), seen={K1: K1_SEEN,
                                                                                          K2: T(2026, 12, 2, 1, 30)}))
    assert new == [("RESUMED", f"clear:{JV}{K1}")] and st["state"] != "RETIRED"
    assert st["pause_reasons"] == [JV + K2]


def test_late_settle_k2_found_before_the_owners_clear_only_pauses():
    """Same verdicts, same owner row; only GitHub's start of the 05:10 export moved (to 08:35, as observed)."""
    b, st, new = scenario(T(2026, 12, 2, 8, 35))
    assert new == [("RESUMED", f"clear:{JV}{K1}"), ("PAUSED", JV + K2)]
    assert st["state"] != "RETIRED" and st["pause_reasons"] == [JV + K2]


@pytest.mark.parametrize("starts", [T(2026, 12, 2, 5, 15), T(2026, 12, 2, 7, 0), T(2026, 12, 2, 8, 35),
                                    T(2026, 12, 2, 13, 5), T(2026, 12, 3, 23, 20)])
def test_the_outcome_does_not_depend_on_when_settle_runs(starts):
    b, st, new = scenario(starts)
    assert st["state"] != "RETIRED"
    st, new = b.settle(starts + dt.timedelta(hours=1), V(K1, K2, asof=starts, seen={K1: K1_SEEN,
                                                                                    K2: T(2026, 12, 2, 1, 30)}))
    assert st["state"] != "RETIRED" and st["pause_reasons"] == [JV + K2]
    assert [e["event"] for e in state.events(b.view)].count("RETIRED") == 0


@pytest.mark.parametrize("starts", [T(2026, 12, 2, 7, 35), T(2026, 12, 2, 8, 35), T(2026, 12, 2, 13, 5)])
def test_a_key_first_seen_after_the_clear_retires_whenever_settle_runs(starts):
    """K2 first seen at 07:30, after the owner's 07:00 clear row: retired in any run that sees it, the one that
    applies the clear included."""
    b, st, new = scenario(starts, k2_seen=T(2026, 12, 2, 7, 30))
    assert new == [("RESUMED", f"clear:{JV}{K1}"), ("RETIRED", "violation")]
    assert st["state"] == "RETIRED" and st["calls_enabled"] is False


def test_on_time_runs_give_the_same_retirement():
    """A run at 07:05 applies the clear before K2 exists; the next one, after the judge's 07:30 run, retires."""
    b = Book()
    b.settle(T(2026, 12, 1, 11, 20), V(K1, asof=K1_SEEN, seen={K1: K1_SEEN}))
    b.row("2026-12-02T07:00:00Z", K1)
    st, new = b.settle(T(2026, 12, 2, 7, 5), V(K1, asof=T(2026, 12, 2, 5, 30), seen={K1: K1_SEEN}))
    assert new == [("RESUMED", f"clear:{JV}{K1}")]
    st, new = b.settle(T(2026, 12, 2, 7, 40), V(K1, K2, asof=T(2026, 12, 2, 7, 30),
                                                seen={K1: K1_SEEN, K2: T(2026, 12, 2, 7, 30)}))
    assert new == [("RETIRED", "violation")] and st["state"] == "RETIRED"


# ---------------------------------------------------------------- the arming time and the first-seen time
def test_first_seen_equal_to_the_clear_time_only_pauses():
    b, st, new = scenario(T(2026, 12, 2, 8, 35), k2_seen=T(2026, 12, 2, 7, 0))    # "after" is strict
    assert new == [("RESUMED", f"clear:{JV}{K1}"), ("PAUSED", JV + K2)] and st["state"] != "RETIRED"


@pytest.mark.parametrize("seen", [None, "2026-12-02 07:30", "", 5, "2026-12-02T09:00:00Z"])
def test_a_key_without_a_usable_first_seen_time_pauses_and_never_retires(seen):
    """Missing, not an R-C4 time, or later than the verdict's asof_utc (08:00): the key cannot be shown to be new."""
    b = Book()
    b.settle(T(2026, 12, 1, 11, 20), V(K1, asof=K1_SEEN, seen={K1: K1_SEEN}))
    b.row("2026-12-01T13:00:00Z", K1)
    b.settle(T(2026, 12, 1, 13, 20), V(K1, asof=T(2026, 12, 1, 13, 15), seen={K1: K1_SEEN}))
    v = V(K1, K2, asof=T(2026, 12, 2, 8, 0), seen={K1: K1_SEEN})
    if seen is not None:
        v["violation_first_seen"][K2] = seen
    st, new = b.settle(T(2026, 12, 2, 8, 5), v)
    assert new == [("PAUSED", JV + K2)] and st["state"] != "RETIRED"
    for bad in (None, [K2], "x"):                                       # the map itself missing or not a map
        b2 = Book()
        b2.settle(T(2026, 12, 1, 11, 20), V(K1, asof=K1_SEEN, seen={K1: K1_SEEN}))
        b2.row("2026-12-01T13:00:00Z", K1)
        v2 = V(K1, K3, asof=T(2026, 12, 2, 8, 0))
        if bad is None:
            del v2["violation_first_seen"]
        else:
            v2["violation_first_seen"] = bad
        st, new = b2.settle(T(2026, 12, 2, 8, 5), v2)
        assert ("PAUSED", JV + K3) in new and st["state"] != "RETIRED", bad


def test_the_clear_time_is_the_earliest_row_dated_at_or_after_the_pause():
    """K1 paused at 12-01 11:20. A row dated before the pause (09:00) clears and arms nothing; the clear time is
    the 12:00 row, not a later duplicate (15:00) nor the settle run's time (16:00)."""
    b = Book()
    b.row("2026-12-01T09:00:00Z", K1)
    b.settle(T(2026, 12, 1, 11, 20), V(K1, asof=K1_SEEN, seen={K1: K1_SEEN}))
    b.row("2026-12-01T12:00:00Z", K1)
    b.row("2026-12-01T15:00:00Z", K1)
    st, new = b.settle(T(2026, 12, 1, 16, 0), V(K1, K2, K3, asof=T(2026, 12, 1, 15, 50),
                                                seen={K1: K1_SEEN, K2: T(2026, 12, 1, 11, 50),
                                                      K3: T(2026, 12, 1, 12, 30)}))
    # keys in sorted order: K2 ("A10|", first seen 11:50, before 12:00) pauses; K3 (12:30, after it) retires
    assert new == [("RESUMED", f"clear:{JV}{K1}"), ("PAUSED", JV + K2), ("RETIRED", "violation")]
    b = Book()
    b.settle(T(2026, 12, 1, 11, 20), V(K1, asof=K1_SEEN, seen={K1: K1_SEEN}))
    b.row("2026-12-01T12:00:00Z", K1)
    st, new = b.settle(T(2026, 12, 1, 16, 0), V(K1, K2, asof=T(2026, 12, 1, 15, 50),
                                                seen={K1: K1_SEEN, K2: T(2026, 12, 1, 11, 50)}))
    assert new == [("RESUMED", f"clear:{JV}{K1}"), ("PAUSED", JV + K2)]           # K2 before 12:00


def test_the_arming_time_is_the_earliest_live_clear():
    """Live pauses K1 and K2 cleared at 12:00 and 18:00, applied by the 12:20 and 18:20 runs; K3, first seen at 13:00
    and first carried to the 18:20 run, came after the earliest clear: retired, whichever clear that run applied."""
    b = Book()
    b.settle(T(2026, 12, 1, 11, 20), V(K1, K2, asof=K1_SEEN, seen={K1: K1_SEEN, K2: K1_SEEN}))
    b.row("2026-12-01T12:00:00Z", K1)
    st, new = b.settle(T(2026, 12, 1, 12, 20), V(K1, K2, asof=T(2026, 12, 1, 12, 15), seen={K1: K1_SEEN, K2: K1_SEEN}))
    assert new == [("RESUMED", f"clear:{JV}{K1}")]
    b.row("2026-12-01T18:00:00Z", K2)
    st, new = b.settle(T(2026, 12, 1, 18, 20), V(K1, K2, K3, asof=T(2026, 12, 1, 18, 15),
                                                 seen={K1: K1_SEEN, K2: K1_SEEN, K3: T(2026, 12, 1, 13, 0)}))
    assert new == [("RESUMED", f"clear:{JV}{K2}"), ("RETIRED", "violation")]


def test_canary_and_operational_clears_still_never_arm():
    """Only a live integrity pause's clear sets the arming time (R-AN-55): a canary pause, mirrored, has none."""
    b = Book()
    b.settle(T(2026, 11, 24, 8), V(K1, asof=T(2026, 11, 24, 7, 50), seen={K1: T(2026, 11, 24, 7, 50)}))
    b.row("2026-11-24T09:00:00Z", K1)
    b.settle(T(2026, 11, 25, 8), V(asof=T(2026, 11, 25, 7, 50)))               # lifted by the verdict
    st, new = b.settle(T(2026, 12, 1, 8), V(K2, asof=T(2026, 12, 1, 7, 50), seen={K2: T(2026, 12, 1, 7, 50)}))
    assert new == [("PAUSED", JV + K2)] and st["state"] != "RETIRED"
