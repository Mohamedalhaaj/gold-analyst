"""D_ref against fixture forecast rows (prereg §2; register R-AN-21)."""
import datetime as dt
import math
import statistics

from ga import dref
from synth import slots_text

D = dt.date(2026, 11, 10)
ASOF = "2026-11-10T07:20:00Z"


def rows(n=25, slot=9, start=dt.date(2026, 10, 1), fs=lambda i: 50.0 + i, comp="2026-11-01T00:00:00Z"):
    out, d, i = [], start, 0
    while len(out) < n:
        if d.weekday() < 5:
            out.append((d.isoformat(), slot, repr(fs(i)), (0, 0, 0, 0), comp))
            i += 1
        d += dt.timedelta(days=1)
    return out


def test_last_20_median_and_formula():
    r = rows(25, start=dt.date(2026, 10, 6))       # newest 2026-11-07..: weekdays up to 11-09
    text = slots_text(r)
    block, detail = dref.compute(text, 9, D, ASOF, 4012.345)
    assert detail is None
    kept = [x for x in r if x[0] < D.isoformat()][-20:]
    assert [x["day"] for x in block["rows"]] == [x[0] for x in kept]
    f = statistics.median(float(x[2]) for x in kept)
    assert block["F_ref"] == f
    raw = 4012.345 * (1 - math.exp(-1.0540339626212611 * f / 1e4))
    assert block["D_raw"] == raw and block["D_ref"] == round(raw, 2)
    assert block["D_ref_cents"] == round(round(raw, 2) * 100) and isinstance(block["D_ref_cents"], int)
    assert block["k"] == 1.0540339626212611 and block["slot"] == 9


def test_even_count_median_is_mean_of_middle_two():
    r = rows(12, start=dt.date(2026, 10, 22), fs=lambda i: [10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120][i])
    block, detail = dref.compute(slots_text(r), 9, D, ASOF, 4000.0)
    assert detail is None and block["F_ref"] == 65.0


def test_eligibility_filters():
    base = rows(14, start=dt.date(2026, 10, 19))
    noise = [
        ("2026-11-02", 15, "999.0", (0, 0, 0, 0), "2026-11-03T00:00:00Z"),       # other slot
        ("2026-11-10", 9, "999.0", (0, 0, 0, 0), "2026-11-03T00:00:00Z"),        # day == d
        ("2026-11-03", 9, "999.0", (0, 0, 0, 0), "2026-11-10T07:20:01Z"),        # computed after asof
        ("2026-11-04", 9, "", (0, 0, 0, 0), "2026-11-05T00:00:00Z"),             # empty F_S
        ("2026-11-04", 9, "nan", (0, 0, 0, 0), "2026-11-05T00:00:00Z"),          # not finite
        ("2026-11-04", 9, "-1.0", (0, 0, 0, 0), "2026-11-05T00:00:00Z"),         # not > 0
        ("2026-11-06", 9, "999.0", (1, 0, 0, 0), "2026-11-07T00:00:00Z"),        # NFP
        ("2026-11-05", 9, "999.0", (0, 0, 1, 0), "2026-11-06T00:00:00Z"),        # FOMC
        ("2026-11-05", 9, "999.0", (0, 0, 0, 1), "2026-11-06T00:00:00Z"),        # holiday
    ]
    block, detail = dref.compute(slots_text(base + noise), 9, D, ASOF, 4000.0)
    assert detail is None
    assert all(r["F_S"] != 999.0 and r["F_S"] > 0 for r in block["rows"])
    assert len(block["rows"]) == 14


def test_fewer_than_10_rows():
    block, detail = dref.compute(slots_text(rows(9, start=dt.date(2026, 10, 26))), 9, D, ASOF, 4000.0)
    assert detail == "rows" and block["D_ref"] is None


def test_stale_is_busday_count_over_5():
    # newest row on Monday 2026-11-02 -> busday_count(11-02, 11-10) = 6 -> stale
    r = [x for x in rows(60, start=dt.date(2026, 9, 1)) if x[0] <= "2026-11-02"]
    block, detail = dref.compute(slots_text(r), 9, D, ASOF, 4000.0)
    assert detail == "stale"
    # newest Tuesday 11-03 -> 5 -> not stale
    r = [x for x in rows(60, start=dt.date(2026, 9, 1)) if x[0] <= "2026-11-03"]
    assert dref.compute(slots_text(r), 9, D, ASOF, 4000.0)[1] is None
    # Friday -> Monday is 1
    from ga import clock
    assert clock.busday_count(dt.date(2026, 11, 6), dt.date(2026, 11, 9)) == 1
    assert clock.busday_count(dt.date(2026, 11, 2), dt.date(2026, 11, 9)) == 5


def test_nonpositive():
    block, detail = dref.compute(slots_text(rows(12, start=dt.date(2026, 10, 22), fs=lambda i: 0.001)), 9, D, ASOF,
                                 1.0)
    assert detail == "nonpositive"
