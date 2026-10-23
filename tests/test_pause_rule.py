"""The pause rule by period and by key, and the fresh verdict (register R-AN-55, R-AN-56, R-AN-62, R-AN-70, R-AN-74
C7; §16.18 A6 fourth review: J-4, N2).

- P0 is 00:00Z of the earliest set date of `canary_from` and `live_from`; LS is 00:00Z of `live_from`. Before P0 the
  verdict makes no PAUSED, RESUMED or RETIRED event: the owner's C7 tamper pushes and the probe session never reach
  `analyst/state/`, and need no analyst-side clear.
- Operational keys (every `A0|` key, every `system_error:` entry, A12 `probe_failed`, `app_scope`,
  `routine_connectors`, `inbox_identity`, `inbox_config`) and every key before LS are mirrored: paused while a fresh
  verdict carries them, lifted (`lifted:`) when one does not; an analyst-side row does nothing for them, and they
  never retire the analyst. So an earlier clear or lift never mutes a later episode.
- In the live period an integrity key pauses until an analyst-side clear dated at or after its PAUSED event; a new
  integrity key after such a live clear retires the analyst. A clear of a canary pause, or a row dated before the
  pause, arms nothing.
- A verdict counts only when fresh: valid, with `asof_utc` no more than 48 h before the run and no more than 10 min
  after it. Otherwise P7 is false and the verdict opens, lifts and retires nothing; after 48 h with no fresh verdict
  the analyst is paused `judge_silent` (from P0 on), and the next fresh verdict lifts it.

The two Harness tests at the end are the fourth review's probe (scratchpad verify4/probes_ga/test_v4_ga.py), adapted
to this rule as the register's build list says: the C7 verdict of 11-06 (before canary_from 11-10) leaves no event;
the blind episodes fall on or after P0, each with its own `<L>` key; no retirement, and a later episode pauses again.
"""
import datetime as dt
import json

import pytest

from ga import paths, rec, settle_run, state
from ga.repo import DictView
from harness import Harness
from test_settle_state import records_view

UTC = rec.UTC
C7_KEY = "A3|agent_scope|inbox/refs/heads/claude/analyst-x|0123456789abcdef0123456789abcdef01234567"
INTEG = "A1|call_bytes|analyst/calls/2026-11-10_W1.json|" + "a" * 40
INTEG2 = "A3|agent_scope|inbox/refs/heads/claude/analyst-y|" + "b" * 40
PROBE = "A12|probe_failed|analyst/canary/calls/2026-11-10_W1.json|" + "c" * 40
SYSERR = "system_error:2026-11-12T09:00:00Z"
CONFIG = "A12|inbox_config|analyst/runs/2026-W46.json|" + "d" * 40
BLIND1 = "A0|inbox_blind|api|2026-11-09T23:40:00Z"
BLIND2 = "A0|inbox_blind|api|2026-11-19T09:00:00Z"
JV = "judge_violation:"


def T(*a):
    return dt.datetime(*a, tzinfo=UTC)


def verdict(*keys, asof, value=None, schema="analyst-verdict/1"):
    return {"schema": schema, "asof_utc": rec.iso(asof) if isinstance(asof, dt.datetime) else asof,
            "analyst_verdict": value or ("VIOLATION" if keys else "SOUND"), "violation_keys": list(keys),
            "cleared_keys": []}


class Book:
    """gold-analyst main as analyst-settle's state step sees it, run after run: MODE.json, the owner's
    ADJUDICATIONS.csv, the state events and STATE.json (carried over, as the settle commit does)."""

    def __init__(self, canary_from="2026-11-10", canary_to=None, live_from=None, mode=None):
        mode = mode or ("live" if live_from else "canary")
        self.view = DictView({paths.MODE_PATH: json.dumps({"mode": mode, "canary_from": canary_from,
                                                           "canary_to": canary_to, "live_from": live_from})})
        self.run_id = 0
        self.seen = {}                   # the judge's FINDINGS.csv: key -> the asof of the first verdict carrying it

    def with_first_seen(self, v):
        """The verdict as the judge publishes it since the sixth review of §16.18 A6 (R-AN-70, R6-2): each key with
        its first-seen time, here the `asof_utc` of the first verdict that carried it (A0 keys and `system_error:`
        entries have none)."""
        if not isinstance(v, dict) or "violation_first_seen" in v or not isinstance(v.get("asof_utc"), str) or \
                not isinstance(v.get("violation_keys"), list):
            return v
        for k in v["violation_keys"]:
            if isinstance(k, str) and not k.startswith(("A0|", "system_error:")):
                self.seen.setdefault(k, v["asof_utc"])
        return dict(v, violation_first_seen={k: self.seen[k] for k in v["violation_keys"]
                                             if isinstance(k, str) and k in self.seen})

    def clear(self, utc, key):
        b = (self.view.read("analyst/ADJUDICATIONS.csv") or b"utc,ref,subject,decision,note\n").decode()
        self.view.files["analyst/ADJUDICATIONS.csv"] = (b + f"{utc},pause,{JV}{key},clear,owner\n").encode()

    def settle(self, now, v):
        """One state evaluation at `now` with the verdict `v` (fetched as given, None for a failed fetch)."""
        self.run_id += 1
        new = []

        def write(p, b):
            self.view.files[p] = b
            new.append(json.loads(b))
        st = state.run_state(self.view, [], now, self.run_id, "h", self.with_first_seen(v), write)
        self.view.files[paths.STATE_PATH] = rec.record_bytes(st)
        return st, [(e["event"], e["reason"]) for e in new]


# ---------------------------------------------------------------- before P0
def test_no_event_before_p0():
    b = Book(canary_from="2026-11-10")
    for now, v in [(T(2026, 11, 6, 12), verdict(C7_KEY, asof=T(2026, 11, 6, 11, 55))),
                   (T(2026, 11, 6, 14), verdict(C7_KEY, PROBE, BLIND1, asof=T(2026, 11, 6, 13, 50))),
                   (T(2026, 11, 7, 23, 15), None),                     # no verdict, then 48 h of silence
                   (T(2026, 11, 9, 23, 59), verdict(INTEG, asof=T(2026, 11, 3)))]:
        st, new = b.settle(now, v)
        assert new == [] and st["calls_enabled"] is True and st["pause_reasons"] == []
    assert state.events(b.view) == []
    nob = Book(canary_from=None)                                       # no P0 at all
    st, new = nob.settle(T(2026, 11, 16, 12), verdict(INTEG, asof=T(2026, 11, 16, 11)))
    assert new == [] and st["calls_enabled"] is True


def test_the_first_canary_run_pauses_on_a_key_still_carried_at_p0():
    b = Book(canary_from="2026-11-10")
    b.settle(T(2026, 11, 9, 12), verdict(C7_KEY, asof=T(2026, 11, 9, 11)))
    st, new = b.settle(T(2026, 11, 10, 0, 5), verdict(C7_KEY, asof=T(2026, 11, 10)))
    assert new == [("PAUSED", JV + C7_KEY)] and st["calls_enabled"] is False


# ---------------------------------------------------------------- the canary: every key only pauses
def test_a_canary_integrity_key_pauses_and_is_lifted_with_no_analyst_row():
    b = Book(canary_from="2026-11-10")
    st, new = b.settle(T(2026, 11, 10, 8), verdict(INTEG, asof=T(2026, 11, 10, 7, 50)))
    assert new == [("PAUSED", JV + INTEG)] and st["calls_enabled"] is False
    assert st["pause_reasons"] == [JV + INTEG]
    b.clear("2026-11-10T09:00:00Z", INTEG)                             # an analyst-side row does nothing here
    st, new = b.settle(T(2026, 11, 10, 12), verdict(INTEG, asof=T(2026, 11, 10, 11, 50)))
    assert new == [] and st["calls_enabled"] is False
    st, new = b.settle(T(2026, 11, 11, 8), verdict(asof=T(2026, 11, 11, 7, 50)))   # the judge cleared its key
    assert new == [("RESUMED", f"lifted:{JV}{INTEG}")] and st["calls_enabled"] is True
    st, new = b.settle(T(2026, 11, 12, 8), verdict(INTEG2, asof=T(2026, 11, 12, 7, 50)))
    assert new == [("PAUSED", JV + INTEG2)] and st["state"] != "RETIRED"   # a new key after a lift: a pause
    st, new = b.settle(T(2026, 11, 13, 8), verdict(asof=T(2026, 11, 13, 7, 50)))
    assert new == [("RESUMED", f"lifted:{JV}{INTEG2}")] and st["calls_enabled"] is True
    assert "RETIRED" not in [e["event"] for e in state.events(b.view)]


# ---------------------------------------------------------------- the live period
LIVE = dict(canary_from="2026-11-02", canary_to="2026-11-06", live_from="2026-11-09")


def arm(b):
    """A live integrity pause the owner cleared (R-AN-55 step 2: armed)."""
    b.settle(T(2026, 11, 10, 8), verdict(INTEG, asof=T(2026, 11, 10, 7, 50)))
    b.clear("2026-11-10T13:00:00Z", INTEG)
    st, new = b.settle(T(2026, 11, 10, 14), verdict(INTEG, asof=T(2026, 11, 10, 13, 50)))
    assert new == [("RESUMED", f"clear:{JV}{INTEG}")] and st["calls_enabled"] is True
    return st


def test_a_live_integrity_key_pauses_and_after_its_clear_a_new_one_retires():
    b = Book(**LIVE)
    st, new = b.settle(T(2026, 11, 10, 8), verdict(INTEG, asof=T(2026, 11, 10, 7, 50)))
    assert new == [("PAUSED", JV + INTEG)] and st["calls_enabled"] is False
    st, new = b.settle(T(2026, 11, 10, 10), verdict(asof=T(2026, 11, 10, 9, 50)))     # the verdict drops it:
    assert new == [] and st["calls_enabled"] is False                                 # no lift for a live key
    b.clear("2026-11-10T13:00:00Z", INTEG)
    st, new = b.settle(T(2026, 11, 10, 14), verdict(INTEG, asof=T(2026, 11, 10, 13, 50)))
    assert new == [("RESUMED", f"clear:{JV}{INTEG}")] and st["calls_enabled"] is True
    st, new = b.settle(T(2026, 11, 10, 16), verdict(INTEG, asof=T(2026, 11, 10, 15, 50)))
    assert new == []                                                   # a cleared live key stays cleared
    st, new = b.settle(T(2026, 11, 11, 8), verdict(INTEG2, asof=T(2026, 11, 11, 7, 50)))
    assert new == [("RETIRED", "violation")] and st["state"] == "RETIRED" and st["calls_enabled"] is False


@pytest.mark.parametrize("key", [PROBE, SYSERR, CONFIG, BLIND1, "A12|app_scope|analyst/runs/2026-W46.json|x",
                                 "A12|routine_connectors|analyst/ROUTINE_CONFIG.json|x",
                                 "A12|inbox_identity|inbox|404"])
def test_an_operational_key_in_the_live_period_pauses_is_lifted_and_never_retires(key):
    b = Book(**LIVE)
    arm(b)
    st, new = b.settle(T(2026, 11, 11, 8), verdict(key, asof=T(2026, 11, 11, 7, 50)))
    assert new == [("PAUSED", JV + key)] and st["calls_enabled"] is False and st["state"] != "RETIRED"
    b.clear("2026-11-11T09:00:00Z", key)                               # a row does nothing for it
    st, new = b.settle(T(2026, 11, 11, 10), verdict(key, asof=T(2026, 11, 11, 9, 50)))
    assert new == [] and st["calls_enabled"] is False
    st, new = b.settle(T(2026, 11, 11, 12), verdict(asof=T(2026, 11, 11, 11, 50)))
    assert new == [("RESUMED", f"lifted:{JV}{key}")] and st["calls_enabled"] is True
    st, new = b.settle(T(2026, 11, 12, 12), verdict(key, asof=T(2026, 11, 12, 11, 50)))   # it recurs: paused again
    assert new == [("PAUSED", JV + key)] and st["state"] != "RETIRED"


def test_a_clear_of_a_canary_pause_arms_and_lifts_nothing():
    b = Book(**LIVE)
    st, new = b.settle(T(2026, 11, 3, 8), verdict(INTEG, asof=T(2026, 11, 3, 7, 50)))
    assert new == [("PAUSED", JV + INTEG)]
    b.clear("2026-11-03T09:00:00Z", INTEG)
    st, new = b.settle(T(2026, 11, 3, 10), verdict(INTEG, asof=T(2026, 11, 3, 9, 50)))
    assert new == [] and st["calls_enabled"] is False                 # the canary pause is mirrored
    st, new = b.settle(T(2026, 11, 4, 8), verdict(asof=T(2026, 11, 4, 7, 50)))
    assert new == [("RESUMED", f"lifted:{JV}{INTEG}")]
    st, new = b.settle(T(2026, 11, 10, 8), verdict(INTEG2, asof=T(2026, 11, 10, 7, 50)))
    assert new == [("PAUSED", JV + INTEG2)] and st["state"] != "RETIRED"   # not armed


def test_a_canary_pause_still_open_at_ls_stays_mirrored():
    b = Book(**LIVE)
    b.settle(T(2026, 11, 5, 8), verdict(INTEG, asof=T(2026, 11, 5, 7, 50)))
    st, new = b.settle(T(2026, 11, 10, 8), verdict(INTEG, asof=T(2026, 11, 10, 7, 50)))
    assert new == [] and st["calls_enabled"] is False
    b.clear("2026-11-10T09:00:00Z", INTEG)
    st, new = b.settle(T(2026, 11, 10, 10), verdict(INTEG, asof=T(2026, 11, 10, 9, 50)))
    assert new == []                                                   # no analyst-side clear of a canary pause
    st, new = b.settle(T(2026, 11, 10, 12), verdict(asof=T(2026, 11, 10, 11, 50)))
    assert new == [("RESUMED", f"lifted:{JV}{INTEG}")] and st["calls_enabled"] is True
    st, new = b.settle(T(2026, 11, 11, 8), verdict(INTEG2, asof=T(2026, 11, 11, 7, 50)))
    assert new == [("PAUSED", JV + INTEG2)] and st["state"] != "RETIRED"


def test_a_clear_dated_before_its_pause_clears_and_arms_nothing():
    b = Book(**LIVE)
    b.clear("2026-11-10T07:00:00Z", INTEG)                             # written before the pause exists
    st, new = b.settle(T(2026, 11, 10, 8), verdict(INTEG, asof=T(2026, 11, 10, 7, 50)))
    assert new == [("PAUSED", JV + INTEG)] and st["calls_enabled"] is False
    st, new = b.settle(T(2026, 11, 10, 10), verdict(INTEG, asof=T(2026, 11, 10, 9, 50)))
    assert new == [] and st["calls_enabled"] is False
    st, new = b.settle(T(2026, 11, 11, 8), verdict(INTEG, INTEG2, asof=T(2026, 11, 11, 7, 50)))
    assert new == [("PAUSED", JV + INTEG2)] and st["state"] != "RETIRED"


def test_a_model_pause_needs_a_clear_dated_at_or_after_it():
    runs = {"analyst/runs/2026-W47.json": json.dumps({"runs": [
        {"routine": "analyst-w1", "run_id": "r1", "session_id": "s-2026-11-16_W1", "created_utc": "x",
         "model": "claude-sonnet-x"}]}),
        "analyst/ADJUDICATIONS.csv": "utc,ref,subject,decision,note\n2026-11-20T00:00:00Z,pause,model:r1,clear,x\n"}
    view, days = records_view([0.5] * 5, extra=runs)
    out = []

    def write(p, b):
        view.files[p] = b
        out.append(json.loads(b))
    now = T(2026, 11, 21, 12)
    st = state.run_state(view, days, now, 1, "h", None, write)
    assert ("PAUSED", "model:r1") in [(e["event"], e["reason"]) for e in out] and st["calls_enabled"] is False
    view.files["analyst/ADJUDICATIONS.csv"] += b"2026-11-21T12:30:00Z,pause,model:r1,clear,ok\n"
    st = state.run_state(view, days, T(2026, 11, 21, 13), 2, "h", None, write)
    assert out[-1]["event"] == "RESUMED" and out[-1]["reason"] == "clear:model:r1" and st["calls_enabled"] is True


# ---------------------------------------------------------------- the fresh verdict (N2)
def stale_and_invalid(now):
    return [verdict(INTEG2, asof=now - dt.timedelta(hours=48, seconds=1)),
            verdict(INTEG2, asof=now + dt.timedelta(minutes=10, seconds=1)),
            verdict(INTEG2, asof=now, schema="analyst-verdict/2"),
            verdict(INTEG2, asof="2026-11-10 07:50"),
            verdict(INTEG2, asof=None),
            dict(verdict(INTEG2, asof=now), violation_keys="A1|x"),
            dict(verdict(INTEG2, asof=now), violation_keys=[1]),
            dict(verdict(INTEG2, asof=now), analyst_verdict="BAD"),
            None]


def test_a_stale_or_invalid_verdict_changes_no_pause():
    b = Book(canary_from="2026-11-10")
    b.settle(T(2026, 11, 10, 8), verdict(INTEG, asof=T(2026, 11, 10, 7, 50)))
    now = T(2026, 11, 10, 20)
    for v in stale_and_invalid(now):
        st, new = b.settle(now, v)                                     # opens nothing, lifts nothing
        assert new == [] and st["pause_reasons"] == [JV + INTEG], v
        assert st["judge_verdict"]["fresh"] is False
        assert st["judge_verdict"]["last_fresh_asof_utc"] == "2026-11-10T07:50:00Z"
    edge = [verdict(asof=now - dt.timedelta(hours=48)), verdict(asof=now + dt.timedelta(minutes=10))]
    st, new = b.settle(now, edge[0])                                   # exactly 48 h old: fresh, so it lifts
    assert new == [("RESUMED", f"lifted:{JV}{INTEG}")] and st["judge_verdict"]["fresh"] is True
    st, _ = b.settle(now, edge[1])
    assert st["judge_verdict"]["fresh"] is True
    assert st["judge_verdict"]["last_fresh_asof_utc"] == "2026-11-10T20:10:00Z"


def test_a_stale_or_invalid_verdict_leaves_p7_false():
    ys = [1.0] * 70
    view, days = records_view(ys)
    now = dt.datetime.combine(days[-1] + dt.timedelta(days=1), dt.time(12), tzinfo=UTC)

    def promoted(v):
        vw, ds = records_view(ys)
        st = state.run_state(vw, ds, now, 1, "h", v, lambda p, b: vw.files.__setitem__(p, b))
        return st["state"] == "PROMOTED"
    assert promoted(verdict(asof=now - dt.timedelta(hours=1)))
    assert promoted(verdict(asof=now - dt.timedelta(hours=1), value="WARNING"))
    for v in [verdict(asof=now - dt.timedelta(hours=48, seconds=1)),
              verdict(asof=now + dt.timedelta(minutes=10, seconds=1)),
              verdict(asof=now, schema="analyst-verdict/2"),
              verdict(asof="2026-11-10 07:50"),
              verdict(asof=None),
              dict(verdict(asof=now), violation_keys=None),
              dict(verdict(asof=now), analyst_verdict="BAD"),
              verdict(INTEG, asof=now),                                # fresh, but VIOLATION
              None]:
        assert not promoted(v), v


def test_48_h_without_a_fresh_verdict_pauses_judge_silent_and_a_fresh_one_lifts_it():
    b = Book(canary_from="2026-11-10")
    st, _ = b.settle(T(2026, 11, 9, 12), verdict(asof=T(2026, 11, 9, 11)))
    assert st["judge_verdict"]["fresh"] is True and st["judge_verdict"]["last_fresh_asof_utc"] == "2026-11-09T11:00:00Z"
    for now in (T(2026, 11, 10, 8), T(2026, 11, 11, 11)):              # 48 h exactly: not more than 48 h
        st, new = b.settle(now, None)
        assert new == [] and st["calls_enabled"] is True
        assert st["judge_verdict"]["last_fresh_asof_utc"] == "2026-11-09T11:00:00Z"
    st, new = b.settle(T(2026, 11, 11, 11, 1), verdict(asof=T(2026, 11, 9, 10)))      # a stale one counts as none
    assert new == [("PAUSED", "judge_silent")] and st["calls_enabled"] is False
    assert st["pause_reasons"] == ["judge_silent"]
    st, new = b.settle(T(2026, 11, 11, 12), None)
    assert new == []
    st, new = b.settle(T(2026, 11, 11, 13), verdict(asof=T(2026, 11, 11, 12, 50)))
    assert new == [("RESUMED", "lifted:judge_silent")] and st["calls_enabled"] is True


def test_judge_silent_from_p0_when_no_fresh_verdict_was_ever_fetched():
    b = Book(canary_from="2026-11-10")
    st, new = b.settle(T(2026, 11, 9, 23), None)
    assert new == []                                                   # before P0: nothing
    st, new = b.settle(T(2026, 11, 10, 0, 1), None)
    assert new == [("PAUSED", "judge_silent")]


def test_judge_silent_never_retires():
    b = Book(**LIVE)
    arm(b)
    st, new = b.settle(T(2026, 11, 13, 8), None)
    assert new == [("PAUSED", "judge_silent")] and st["state"] != "RETIRED"


def test_a_fresh_verdict_with_new_keys_and_a_lift_in_one_run_keeps_the_order():
    b = Book(canary_from="2026-11-10")
    b.settle(T(2026, 11, 10, 8), verdict(INTEG, asof=T(2026, 11, 10, 7, 50)))
    b.settle(T(2026, 11, 12, 9), None)                                 # judge_silent
    st, new = b.settle(T(2026, 11, 12, 10), verdict(BLIND2, asof=T(2026, 11, 12, 9, 50)))
    assert new == [("PAUSED", JV + BLIND2), ("RESUMED", f"lifted:{JV}{INTEG}"), ("RESUMED", "lifted:judge_silent")]
    assert st["pause_reasons"] == [JV + BLIND2]


# ---------------------------------------------------------------- the fourth review's probe, adapted
def settle(h, i, v):
    st, _done = settle_run.run_settle(h.workspace(f"settle{i}"), h.api, h.now,
                                      {"workflow": "analyst-settle", "id": 900 + i, "attempt": 1,
                                       "event": "schedule"}, verdict=v, dispatch=False)
    h.world.mirror()
    return st


def fresh_at(h, *keys):
    return verdict(*keys, asof=h.now() - dt.timedelta(minutes=5))


def test_c7_tamper_verdicts_leave_no_event_and_a_blind_episode_only_pauses(tmp_path):
    h = Harness(tmp_path)                                              # canary_from 2026-11-10
    h.at(2026, 11, 6, 12, 0)                                           # C7: the owner's tamper push, before P0
    settle(h, 1, fresh_at(h, C7_KEY))
    assert h.world.main_list("analyst/state/") == []
    assert h.main_json("analyst/STATE.json")["calls_enabled"] is True
    h.at(2026, 11, 6, 14, 0)                                           # cleared in the judge's file only
    settle(h, 2, fresh_at(h))
    h.at(2026, 11, 10, 6, 0)                                           # the newest verdict is empty at P0
    settle(h, 3, fresh_at(h))
    assert h.world.main_list("analyst/state/") == []
    h.at(2026, 11, 11, 2, 0)                                           # a blind episode, after P0
    settle(h, 4, fresh_at(h, BLIND1))
    st = h.main_json("analyst/STATE.json")
    assert st["calls_enabled"] is False and st["pause_reasons"] == [JV + BLIND1] and st["state"] != "RETIRED"
    h.at(2026, 11, 11, 6, 0)                                           # the judge reads again
    settle(h, 5, fresh_at(h))
    st = h.main_json("analyst/STATE.json")
    assert st["calls_enabled"] is True and st["state"] != "RETIRED"
    h.at(2026, 11, 11, 7, 20)
    assert h.run("start") == 0 and h.log[-1].startswith("attempt_pushed 2026-11-11_W1")
    evs = [(e["event"], e["reason"]) for e in (h.main_json(p) for p in h.world.main_list("analyst/state/"))]
    assert evs == [("PAUSED", JV + BLIND1), ("RESUMED", f"lifted:{JV}{BLIND1}")]


def test_a_second_blind_episode_pauses_again_whatever_the_owner_wrote(tmp_path):
    h = Harness(tmp_path)
    h.at(2026, 11, 10, 12, 0)
    settle(h, 1, fresh_at(h, BLIND1))
    assert h.main_json("analyst/STATE.json")["calls_enabled"] is False
    h.world.owner_commit({"analyst/ADJUDICATIONS.csv":                 # the old C7 habit: an analyst-side row
                          f"utc,ref,subject,decision,note\n2026-11-10T13:00:00Z,pause,{JV}{BLIND1},clear,outage\n"},
                         "clear")
    h.at(2026, 11, 10, 14, 0)
    settle(h, 2, fresh_at(h, BLIND1))
    assert h.main_json("analyst/STATE.json")["calls_enabled"] is False  # does nothing for an operational key
    h.at(2026, 11, 10, 18, 0)
    settle(h, 3, fresh_at(h))
    assert h.main_json("analyst/STATE.json")["calls_enabled"] is True   # lifted by the verdict
    h.at(2026, 11, 20, 12, 0)                                          # a later, separate blind episode
    settle(h, 4, fresh_at(h, BLIND2))
    st = h.main_json("analyst/STATE.json")
    assert st["calls_enabled"] is False and st["pause_reasons"] == [JV + BLIND2] and st["state"] != "RETIRED"
    h.at(2026, 11, 20, 14, 0)
    settle(h, 5, fresh_at(h))
    h.at(2026, 11, 21, 12, 0)                                          # even the first episode's key, again
    settle(h, 6, fresh_at(h, BLIND1))
    st = h.main_json("analyst/STATE.json")
    assert st["calls_enabled"] is False and st["state"] != "RETIRED"
