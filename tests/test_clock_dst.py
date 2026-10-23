"""Windows, the model clock and the DST changes of 2026-10-25 (UK) and 2026-11-01 (US) (register R-AN-8)."""
import datetime as dt

import numpy as np
import pytest

from ga import clock, rec

D = dt.date
TABLE = [  # (first weekday, last weekday, W1 entry, W1 exit, W1 model entry, W1 slot, W2 entry, W2 exit, W2 slot)
    (D(2026, 10, 19), D(2026, 10, 23), "07:00", "11:00", "10:00", 9, "13:30", "17:30", 15),
    (D(2026, 10, 26), D(2026, 10, 30), "08:00", "12:00", "11:00", 10, "13:30", "17:30", 15),
    (D(2026, 11, 2), D(2027, 3, 12), "08:00", "12:00", "10:00", 9, "14:30", "18:30", 15),
    (D(2027, 3, 15), D(2027, 3, 26), "08:00", "12:00", "11:00", 10, "13:30", "17:30", 15),
    (D(2027, 3, 29), D(2027, 10, 29), "07:00", "11:00", "10:00", 9, "13:30", "17:30", 15),
    (D(2027, 11, 1), D(2027, 11, 5), "08:00", "12:00", "11:00", 10, "13:30", "17:30", 15),
    (D(2027, 11, 8), D(2027, 12, 31), "08:00", "12:00", "10:00", 9, "14:30", "18:30", 15),
]


def hm(t):
    return t.strftime("%H:%M")


@pytest.mark.parametrize("row", TABLE)
def test_register_window_table(row):
    a, b, e1, x1, m1, s1, e2, x2, s2 = row
    for d in clock.days_range(a, b):
        if not clock.is_weekday(d):
            continue
        w1, w2 = clock.windows_on(d)
        assert (hm(w1.entry_utc), hm(w1.exit_utc), hm(w1.entry_server), w1.slot) == (e1, x1, m1, s1), d
        assert (hm(w2.entry_utc), hm(w2.exit_utc), hm(w2.entry_server), w2.slot) == (e2, x2, "16:30", s2), d
        for w in (w1, w2):
            assert w.entry_utc.date() == d == clock.to_server(w.entry_utc).date()
            assert w.deadline_utc == w.entry_utc - dt.timedelta(minutes=15)
            assert len(w.path_minutes) == 240


def test_uk_change_2026_10_25_and_us_change_2026_11_01():
    # Friday before the UK change: BST; Monday after: GMT while New York is still EDT (the mismatch week)
    assert hm(clock.Window(D(2026, 10, 23), "W1").entry_utc) == "07:00"
    assert hm(clock.Window(D(2026, 10, 26), "W1").entry_utc) == "08:00"
    assert clock.Window(D(2026, 10, 26), "W1").slot == 10
    # W2 moves only with the US change
    assert hm(clock.Window(D(2026, 10, 30), "W2").entry_utc) == "13:30"
    assert hm(clock.Window(D(2026, 11, 2), "W2").entry_utc) == "14:30"
    assert clock.Window(D(2026, 11, 2), "W1").slot == 9


def cron_fires(day, minute, hrs):
    return [dt.datetime(day.year, day.month, day.day, h, minute, tzinfo=rec.UTC) for h in hrs]


def test_each_window_has_exactly_one_cron_fire_in_its_span():
    """R1 crons: W1 `20 6,7 * * 1-5`, W2 `50 12,13 * * 1-5`, every weekday 2026-10-19..2027-12-31."""
    for d in clock.days_range(D(2026, 10, 19), D(2027, 12, 31)):
        if not clock.is_weekday(d):
            continue
        for w, (mi, hrs) in zip(clock.windows_on(d), ((20, (6, 7)), (50, (12, 13)))):
            inside = [f for f in cron_fires(d, mi, hrs) if w.span_start_utc <= f <= w.deadline_utc]
            assert len(inside) == 1, (d, w.name)
            # with T_run up to 20 the in-span fire stays in the start interval
            assert w.in_start_interval(inside[0], 20)


def test_model_clock_round_trip_and_raise():
    for d in clock.days_range(D(2026, 10, 19), D(2026, 11, 13)):
        if clock.is_weekday(d):
            naive = dt.datetime.combine(d, dt.time(0))
            u = clock.model_to_utc(naive)
            assert clock.to_server(u) == naive
            assert u.hour in (21, 22)
    with pytest.raises(ValueError):          # New York 01:30 on 2026-11-01 is repeated
        clock.model_to_utc(dt.datetime(2026, 11, 1, 8, 30))
    with pytest.raises(ValueError):          # New York 02:30 on 2027-03-14 does not exist
        clock.model_to_utc(dt.datetime(2027, 3, 14, 9, 30))


def test_busday_count_is_numpys():
    rng = np.random.default_rng(1)
    base = D(2026, 1, 1)
    for _ in range(3000):
        a = base + dt.timedelta(days=int(rng.integers(0, 700)))
        b = base + dt.timedelta(days=int(rng.integers(0, 700)))
        assert clock.busday_count(a, b) == int(np.busday_count(a, b)), (a, b)


def test_weekday_before_and_iso_week_and_months():
    assert clock.weekday_before(D(2026, 11, 9), 10) == D(2026, 10, 26)
    assert clock.weekday_before(D(2026, 11, 9), 1) == D(2026, 11, 6)
    assert clock.iso_week(D(2026, 11, 14)) == "2026-W46"
    import pandas as pd
    for d in (D(2027, 1, 31), D(2027, 2, 28), D(2028, 2, 29), D(2026, 11, 9)):
        assert clock.add_months(d, 12) == (pd.Timestamp(d) + pd.DateOffset(months=12)).date()


def test_call_ids():
    assert clock.call_id(D(2026, 11, 9), "W1") == "2026-11-09_W1"
    assert clock.parse_call_id("2026-11-09_W2") == (D(2026, 11, 9), "W2")
    for bad in ("2026-11-9_W1", "2026-11-09_W3", "x", "../2026-11-09_W1"):
        with pytest.raises(ValueError):
            clock.parse_call_id(bad)
