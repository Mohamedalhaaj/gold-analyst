"""§16.18 A16 (R-AN-89, R-AN-39 step 2, R-AN-91): from MODE.json a16_from on the day cap is 10 instead of 6, in R(T)
(`run_window.py start`'s `day_cap` gate), in the closer's `skipped:day_cap` and in the watcher's fire guard (the
watcher's own tests cover it). Days before a16_from, and a MODE.json without it, keep 6."""
import datetime as dt
from types import SimpleNamespace

from ga import merge, paths, wake

A15 = "2026-11-09"
A16 = "2026-11-16"
BEFORE, ON = dt.date(2026, 11, 13), dt.date(2026, 11, 16)
UTC = dt.timezone.utc


def test_the_cap_of_a_day():
    m = {"mode": "canary", "a14_from": "2026-11-02", "a15_from": A15, "a16_from": A16}
    assert (wake.DAY_CAP, wake.DAY_CAP_A16) == (6, 10)
    assert paths.a16_from(m) == ON and paths.a16_from({"a15_from": A15}) is None
    assert paths.day_cap(m, BEFORE) == 6 and paths.day_cap(m, ON) == 10
    assert paths.day_cap(m, ON + dt.timedelta(days=3)) == 10
    assert paths.day_cap({"a15_from": A15}, ON) == 6 and paths.day_cap(None, ON) == 6


def test_start_gives_the_rule_the_cap_of_its_day(tmp_path, monkeypatch):
    """`run_window.py start` of a trigger window passes its day's cap to R(T): 10 from a16_from on, else 6."""
    from harness import Harness
    from test_a14_session import T1100, UP
    seen = []
    real = wake.rule

    def spy(*a, **k):
        seen.append(a[6] if len(a) > 6 else k.get("cap", wake.DAY_CAP))
        return real(*a, **k)
    monkeypatch.setattr(wake, "rule", spy)
    for a16, cap in (("2026-11-10", 10), ("2026-11-11", 6), (None, 6)):
        h = Harness(tmp_path / f"{a16}", a14_from="2026-11-10", a15_from="2026-11-10", a16_from=a16, m30=UP)
        h.at(2026, 11, 10, 11, 1, 10)
        assert h.run("start") == 0 and h.log[-1] == f"attempt_pushed {T1100} n=1 mode=canary"
        assert seen[-1] == cap, a16


def closer(mode_doc, n_earlier):
    """A Merge whose model day holds `n_earlier` earlier windows, each with an accepted attempt."""
    m = object.__new__(merge.Merge)
    m.mode_doc = mode_doc
    m.view = None
    m.earlier_of_day = lambda mode, w: [SimpleNamespace(call_id=f"x{i}", exit_utc=w.entry_utc) for i in
                                        range(n_earlier)]
    return m


def window(day):
    entry = dt.datetime(day.year, day.month, day.day, 16, 30, tzinfo=UTC)
    return SimpleNamespace(kind="second_look", call_id=f"{day}_W2b", day=day, entry_utc=entry)


def test_the_closer_skips_at_6_before_a16_from_and_at_10_from_it(monkeypatch):
    monkeypatch.setattr(merge.witness, "accepted_attempts", lambda view, mode, cid: [1])
    m = {"mode": "canary", "a14_from": "2026-11-02", "a15_from": A15, "a16_from": A16}
    assert closer(m, 6)._skip_status("canary", window(BEFORE)) == ("skipped:day_cap", "6")
    assert closer(m, 6)._skip_status("canary", window(ON)) is None
    assert closer(m, 9)._skip_status("canary", window(ON)) is None
    assert closer(m, 10)._skip_status("canary", window(ON)) == ("skipped:day_cap", "10")
    no16 = {"mode": "canary", "a14_from": "2026-11-02", "a15_from": A15}
    assert closer(no16, 6)._skip_status("canary", window(ON)) == ("skipped:day_cap", "6")
