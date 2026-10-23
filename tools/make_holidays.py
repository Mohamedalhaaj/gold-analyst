"""Generate analyst/tables/holidays.csv (register R-AN-9). Build-time tool; never run by a session or a workflow.

pandas_market_calendars 5.4.0 (the version locked in gold-forward-watch uv.lock), calendars "NYSE" and "LSE",
2026-10-01..2027-12-31. `full` = weekdays absent from schedule(); `early` = the rows of early_closes(schedule),
close_utc = that row's market_close (UTC, R-C4). Header `date,exchange,kind,close_utc`, rows sorted by
(date, exchange). Appending later dates is an OPS change logged in analyst/ops_changes.csv.

Usage: uv run --group dev python tools/make_holidays.py [--start 2026-10-01] [--end 2027-12-31] [--out PATH]
"""
import argparse
import sys
from pathlib import Path

HEADER = "date,exchange,kind,close_utc"


def rows(start, end):
    import pandas as pd
    import pandas_market_calendars as mcal

    if mcal.__version__ != "5.4.0":
        raise SystemExit(f"pandas_market_calendars {mcal.__version__}, expected 5.4.0")
    out = []
    for exch in ("NYSE", "LSE"):
        cal = mcal.get_calendar(exch)
        sched = cal.schedule(start_date=start, end_date=end)
        have = {d.date() for d in sched.index}
        for d in pd.date_range(start, end, freq="D"):
            if d.dayofweek < 5 and d.date() not in have:
                out.append((d.date().isoformat(), exch, "full", ""))
        early = cal.early_closes(sched)
        for d, r in early.iterrows():
            close = pd.Timestamp(r["market_close"]).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
            out.append((pd.Timestamp(d).date().isoformat(), exch, "early", close))
    out.sort()
    return out


def render(start, end):
    return HEADER + "\n" + "".join(",".join(r) + "\n" for r in rows(start, end))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-10-01")
    ap.add_argument("--end", default="2027-12-31")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "analyst" / "tables" / "holidays.csv"))
    a = ap.parse_args(argv)
    Path(a.out).write_text(render(a.start, a.end), encoding="utf-8", newline="\n")
    print(a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
