"""Late schedules (register R-AN-24, R-AN-32; §16.18 A6 fourth review, N4). GitHub starts scheduled runs in this
account hours late, so an attempt can stay unwitnessed for hours. The in-flight refusal (`concurrent`) concerns the
same draw only: a later window's scheduled call is never refused by an earlier window's unwitnessed attempt, and a
lock merged hours late keeps its lock time (tests/test_inbox_outage.py covers the lock)."""
from harness import Harness

W1, W2 = "2026-11-10_W1", "2026-11-10_W2"


def test_an_unwitnessed_attempt_never_refuses_another_windows_call(tmp_path):
    h = Harness(tmp_path)
    assert h.run("start") == 0                                         # W1's attempt, 07:20; no merge run since
    h.register_pushes()
    h.at(2026, 11, 10, 7, 30)
    other = h.new_session_dir("second")
    assert h.run("start", sess_dir=other) == 0 and h.log[-1] == "refused concurrent"   # the same draw: refused
    h.at(2026, 11, 10, 13, 50)                                         # W2's routine; W1's attempt still unwitnessed
    third = h.new_session_dir("third")
    assert h.run("start", sess_dir=third) == 0
    assert h.log[-1] == f"attempt_pushed {W2} n=1 mode=canary"


BLIND = "A0|inbox_blind|api|2026-11-09T23:40:00Z"


def _verdict(*keys, asof):
    return {"schema": "analyst-verdict/1", "asof_utc": asof, "analyst_verdict": "VIOLATION" if keys else "SOUND",
            "violation_keys": list(keys), "cleared_keys": []}


def test_a_fire_after_a_lift_waits_for_the_inbox_mirror(tmp_path):
    """§16.18 A6 decision 33 (fifth review, J-5) and A7 R7-1: the reviewer's probe
    (scratchpad verify5/probes_ga/test_v5_session.py) turned round. `start` reads STATE.json from inbox main, which
    only analyst-inbox-mirror updates; so a W2 fire after analyst-settle's lift but before the mirror is refused
    `calls_disabled` (the judge counts no in-span run while inbox main showed the pause, R-AN-60), and the same fire
    after the mirror pushes its attempt. A regression test of existing behaviour: no analyst code changes for it."""
    from ga import settle_run

    def settle(i, v):
        return settle_run.run_settle(h.workspace(f"settle{i}"), h.api, h.now,
                                     {"workflow": "analyst-settle", "id": 900 + i, "attempt": 1, "event": "push"},
                                     verdict=v, dispatch=False)
    h = Harness(tmp_path)                                      # canary_from 2026-11-10: P0 is 00:00Z that day
    h.auto_mirror = False
    h.at(2026, 11, 10, 0, 5)
    st, _ = settle(1, _verdict(BLIND, asof="2026-11-10T00:00:00Z"))   # an operational key: a mirrored pause
    assert st["calls_enabled"] is False
    h.world.mirror()                                           # inbox main shows the pause
    h.at(2026, 11, 10, 13, 47)                                 # W2 (EST): entry 14:30, start interval 13:45-14:15
    st, _ = settle(2, _verdict(asof="2026-11-10T13:40:00Z"))   # the verdict drops the key: lifted on main
    assert st["calls_enabled"] is True
    last = h.main_json(sorted(h.world.main_list("analyst/state/"))[-1])
    assert (last["event"], last["reason"]) == ("RESUMED", f"lifted:judge_violation:{BLIND}")
    h.at(2026, 11, 10, 13, 50)                                 # the 13:50 fire, before analyst-inbox-mirror ran
    assert h.run("start") == 0 and h.log[-1] == "refused calls_disabled"
    assert h.register_pushes() == []                           # nothing pushed
    h.world.mirror()                                           # the mirror brings the lift to inbox main
    assert h.run("start") == 0 and h.log[-1] == f"attempt_pushed {W2} n=1 mode=canary"
    assert [ref for ref, _sha in h.register_pushes()] == [f"refs/heads/claude/analyst-canary-attempt-{W2}-1"]
