"""An older verdict is not fresh (register R-AN-70, R-AN-56; §16.18 A7 decision 15, the sixth review's note).

Freshness was judged against the run's t0 alone, so a verdict older than the one an earlier settle run used, but
still inside [t0 - 48 h, t0 + 10 min] (a stale cache, an old deploy), lifted a mirrored pause for one run, satisfied
P7, and, in the live period, retired the analyst on a key the owner had already cleared with the judge. Now a verdict
whose asof_utc is earlier than L_prev, the `judge_verdict.last_fresh_asof_utc` of STATE.json at the start of the run,
counts as no fresh verdict: it opens, lifts, retires and promotes nothing, and leaves `last_fresh_asof_utc` as it
was. The same verdict fetched again (asof_utc equal to L_prev) stays fresh.

The first test is the sixth review's probe `test_an_older_fresh_verdict_can_lift_or_retire_against_the_newer_one`
(scratchpad verify5/r7/probes_ga/test_r7_ga.py) turned round.
"""
import datetime as dt
import json

from ga import paths, rec, state
from ga.repo import DictView
from test_settle_state import records_view

UTC = rec.UTC
JV = "judge_violation:"
K1 = "A3|agent_scope|analyst/witness/000123.json|" + "a" * 40
K2 = "A10|in_span_without_attempt|analyst/runs/2026-W49.json|" + "b" * 40
KC = "A3|agent_scope|inbox/refs/heads/claude/analyst-c|" + "c" * 40
MODE = {"mode": "live", "canary_from": "2026-11-25", "canary_to": "2026-11-27", "live_from": "2026-11-30"}


def T(*a):
    return dt.datetime(*a, tzinfo=UTC)


class Book:
    """gold-analyst main as settle's state step sees it, with the judge's first-seen times (its FINDINGS.csv)."""

    def __init__(self, mode=MODE):
        self.view = DictView({paths.MODE_PATH: json.dumps(mode)})
        self.n = 0
        self.seen = {}

    def V(self, *keys, asof):
        for k in keys:
            self.seen.setdefault(k, rec.iso(asof))
        return {"schema": "analyst-verdict/1", "asof_utc": rec.iso(asof),
                "analyst_verdict": "VIOLATION" if keys else "SOUND", "violation_keys": list(keys),
                "cleared_keys": [], "violation_first_seen": {k: self.seen[k] for k in keys}}

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


def test_an_older_verdict_served_after_the_newer_one_retires_nothing():
    b = Book()
    b.settle(T(2026, 12, 1, 7), b.V(K1, asof=T(2026, 12, 1, 6)))           # K1, a live integrity key: paused
    b.row("2026-12-01T09:00:00Z", K1)                                       # the owner's clear: A = 09:00
    b.settle(T(2026, 12, 1, 13), b.V(K1, asof=T(2026, 12, 1, 12)))
    old = b.V(K1, K2, asof=T(2026, 12, 1, 18))                              # K2 found 18:00, after A
    new_v = b.V(K1, asof=T(2026, 12, 1, 19))                                # the owner cleared K2 with the judge
    st, new = b.settle(T(2026, 12, 1, 19, 5), new_v)
    assert new == [] and st["judge_verdict"]["last_fresh_asof_utc"] == "2026-12-01T19:00:00Z"
    st, new = b.settle(T(2026, 12, 1, 19, 6), old)                          # a stale cached copy, within 48 h
    assert new == [] and st["state"] != "RETIRED" and st["calls_enabled"] is True
    assert st["judge_verdict"] == {"value": "VIOLATION", "asof_utc": "2026-12-01T18:00:00Z", "fetched": True,
                                   "fresh": False, "last_fresh_asof_utc": "2026-12-01T19:00:00Z"}
    st, new = b.settle(T(2026, 12, 1, 19, 7), new_v)                        # the newer verdict fetched again
    assert new == [] and st["judge_verdict"]["fresh"] is True
    assert st["judge_verdict"]["last_fresh_asof_utc"] == "2026-12-01T19:00:00Z"


def test_an_older_verdict_lifts_no_mirrored_pause():
    b = Book()
    st, new = b.settle(T(2026, 11, 25, 7, 10), b.V(KC, asof=T(2026, 11, 25, 7)))     # canary: a mirrored pause
    assert new == [("PAUSED", JV + KC)]
    st, new = b.settle(T(2026, 11, 25, 7, 20), b.V(asof=T(2026, 11, 25, 6)))         # an older SOUND copy
    assert new == [] and st["calls_enabled"] is False and st["pause_reasons"] == [JV + KC]
    st, new = b.settle(T(2026, 11, 25, 7, 30), b.V(KC, asof=T(2026, 11, 25, 7)))     # the same verdict again
    assert new == [] and st["judge_verdict"]["fresh"] is True
    st, new = b.settle(T(2026, 11, 25, 13, 10), b.V(asof=T(2026, 11, 25, 13)))       # a newer verdict lifts it
    assert new == [("RESUMED", f"lifted:{JV}{KC}")] and st["calls_enabled"] is True


def test_an_older_verdict_opens_no_pause_and_does_not_count_against_silence():
    b = Book()
    b.settle(T(2026, 11, 25, 7, 10), b.V(asof=T(2026, 11, 25, 7)))
    st, new = b.settle(T(2026, 11, 26, 7, 10), b.V(KC, asof=T(2026, 11, 25, 6)))     # older, with a key
    assert new == [] and st["calls_enabled"] is True and st["judge_verdict"]["fresh"] is False
    st, new = b.settle(T(2026, 11, 27, 7, 11), b.V(asof=T(2026, 11, 25, 6, 30)))     # older; 48 h after L_prev
    assert new == [("PAUSED", "judge_silent")]
    assert st["judge_verdict"]["last_fresh_asof_utc"] == "2026-11-25T07:00:00Z"


def test_an_older_sound_verdict_does_not_promote():
    ys = [1.0] * 70
    _v, days = records_view(ys)
    now = dt.datetime.combine(days[-1] + dt.timedelta(days=1), dt.time(12), tzinfo=UTC)
    l_prev = now - dt.timedelta(hours=1)

    def promoted(asof, value="SOUND"):
        vw, ds = records_view(ys, extra={paths.STATE_PATH: json.dumps(
            {"judge_verdict": {"last_fresh_asof_utc": rec.iso(l_prev)}})})
        v = {"schema": "analyst-verdict/1", "asof_utc": rec.iso(asof), "analyst_verdict": value,
             "violation_keys": [] if value != "VIOLATION" else [K1], "cleared_keys": []}
        st = state.run_state(vw, ds, now, 1, "h", v, lambda p, b: vw.files.__setitem__(p, b))
        return st["state"] == "PROMOTED"
    assert promoted(l_prev)                                     # the verdict L_prev came from: fresh, P7 holds
    assert promoted(l_prev + dt.timedelta(minutes=30))
    assert not promoted(l_prev - dt.timedelta(seconds=1))       # older than L_prev: no fresh verdict, P7 false


def test_fresh_verdict_against_l_prev():
    now = T(2026, 12, 1, 12)
    v = {"schema": "analyst-verdict/1", "asof_utc": "2026-12-01T10:00:00Z", "analyst_verdict": "SOUND",
         "violation_keys": [], "cleared_keys": []}
    assert state.fresh_verdict(v, now) is v                                  # no L_prev: as before
    assert state.fresh_verdict(v, now, None) is v
    assert state.fresh_verdict(v, now, T(2026, 12, 1, 10)) is v              # equal: the same verdict again
    assert state.fresh_verdict(v, now, T(2026, 12, 1, 9, 59, 59)) is v
    assert state.fresh_verdict(v, now, T(2026, 12, 1, 10, 0, 1)) is None     # older than the newest fresh one
