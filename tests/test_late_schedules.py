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
