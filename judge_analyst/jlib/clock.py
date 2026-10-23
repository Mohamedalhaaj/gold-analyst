"""Clocks, windows, holidays and event calendars for the analyst judge.

Spec: R-C1 (model clock = New York + 7 h), R-C3 (slot), R-AN-4 (ids), R-AN-8 (windows), R-AN-9 (holidays, embedded
here from the register so the judge does not trust the analyst's table), R-AN-10 (event calendars, the judge's own
byte copies, sha256-pinned), R-AN-11 (expected windows).
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import os
from zoneinfo import ZoneInfo

from .common import UTC, fmt_server, fmt_utc, sha256_bytes

LONDON = ZoneInfo("Europe/London")
NEW_YORK = ZoneInfo("America/New_York")
MODEL_SHIFT = dt.timedelta(hours=7)

WINDOW_DEF = {
    # window: (zone, entry (h, m), forced exit (h, m), exchange)
    "W1": (LONDON, (8, 0), (12, 0), "LSE"),
    "W2": (NEW_YORK, (9, 30), (13, 30), "NYSE"),
}

# ---------------------------------------------------------------- model clock (R-C1)


def to_server(t: dt.datetime) -> dt.datetime:
    """UTC (aware) → model clock (naive): New York wall clock + 7 h."""
    return t.astimezone(NEW_YORK).replace(tzinfo=None) + MODEL_SHIFT


def model_to_utc(m: dt.datetime) -> dt.datetime:
    """Model clock (naive) → UTC (aware). Ambiguous or non-existent New York times raise (R-C1)."""
    x = m - MODEL_SHIFT
    a = x.replace(tzinfo=NEW_YORK, fold=0)
    b = x.replace(tzinfo=NEW_YORK, fold=1)
    if a.utcoffset() != b.utcoffset():
        raise ValueError(f"ambiguous or non-existent New York time {x}")
    return a.astimezone(UTC)


def slot_of(t_utc: dt.datetime) -> int:
    """R-C3: H1 slot = model hour − 1."""
    return to_server(t_utc).hour - 1


# ---------------------------------------------------------------- weekdays


def is_weekday(d: dt.date) -> bool:
    return d.weekday() < 5


def busday_count(a: dt.date, b: dt.date) -> int:
    """numpy.busday_count(a, b) with the default Mon-Fri week and no holidays."""
    if a == b:
        return 0
    sign = 1
    if b < a:
        a, b, sign = b, a, -1
    n = 0
    d = a
    while d < b:
        if d.weekday() < 5:
            n += 1
        d += dt.timedelta(days=1)
    return sign * n


def weekday_before(d: dt.date, k: int) -> dt.date:
    """The k-th weekday before d (k >= 1)."""
    x = d
    n = 0
    while n < k:
        x -= dt.timedelta(days=1)
        if x.weekday() < 5:
            n += 1
    return x


def next_weekday(d: dt.date) -> dt.date:
    x = d + dt.timedelta(days=1)
    while x.weekday() >= 5:
        x += dt.timedelta(days=1)
    return x


def iso_week(d: dt.date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def add_months_12(d: dt.date) -> dt.date:
    """(d + pandas.DateOffset(months=12)).date(): same month and day next year, Feb 29 → Feb 28."""
    try:
        return d.replace(year=d.year + 1)
    except ValueError:
        return d.replace(year=d.year + 1, day=28)


# ---------------------------------------------------------------- windows (R-AN-4, R-AN-8)


class Window:
    __slots__ = ("day", "w", "entry", "exit", "deadline", "span_lo", "call_id", "exchange", "entry_server",
                 "exit_server", "slot")

    def __init__(self, day: dt.date, w: str):
        if w not in WINDOW_DEF:
            raise ValueError(w)
        zone, (eh, em), (xh, xm), exch = WINDOW_DEF[w]
        self.day = day
        self.w = w
        self.entry = dt.datetime(day.year, day.month, day.day, eh, em, tzinfo=zone).astimezone(UTC)
        self.exit = dt.datetime(day.year, day.month, day.day, xh, xm, tzinfo=zone).astimezone(UTC)
        self.deadline = self.entry - dt.timedelta(minutes=15)
        self.span_lo = self.entry - dt.timedelta(minutes=45)
        self.call_id = f"{day.isoformat()}_{w}"
        self.exchange = exch
        self.entry_server = fmt_server(to_server(self.entry))
        self.exit_server = fmt_server(to_server(self.exit))
        self.slot = slot_of(self.entry)

    @property
    def order(self):
        return (self.day, self.w)

    def start_hi(self, t_run_min: int) -> dt.datetime:
        return self.deadline - dt.timedelta(minutes=t_run_min)

    def times(self) -> dict:
        return {"entry_utc": fmt_utc(self.entry), "exit_utc": fmt_utc(self.exit), "deadline_utc": fmt_utc(self.deadline)}

    def __repr__(self):
        return f"Window({self.call_id})"


def parse_call_id(call_id: str):
    try:
        day_s, w = call_id.split("_")
        d = dt.date.fromisoformat(day_s)
    except Exception:
        return None
    if w not in WINDOW_DEF or not is_weekday(d) or d.isoformat() != day_s:
        return None
    return d, w


def window_of(call_id: str):
    p = parse_call_id(call_id)
    return None if p is None else Window(*p)


def windows_between(d0: dt.date, d1: dt.date):
    """Every W1 and W2 of the weekdays in [d0, d1], in window order."""
    out = []
    d = d0
    while d <= d1:
        if d.weekday() < 5:
            out.append(Window(d, "W1"))
            out.append(Window(d, "W2"))
        d += dt.timedelta(days=1)
    return out


# ---------------------------------------------------------------- holidays (R-AN-9)

HOLIDAY_HORIZON = dt.date(2027, 12, 31)
_NYSE_FULL = ["2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
              "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"]
_NYSE_EARLY = ["2026-11-27", "2026-12-24", "2027-11-26"]            # close 18:00 UTC
_LSE_FULL = ["2026-12-25", "2026-12-28", "2027-01-01", "2027-03-26", "2027-03-29", "2027-05-03", "2027-05-31",
             "2027-08-30", "2027-12-27", "2027-12-28"]
_LSE_EARLY = ["2026-12-24", "2026-12-31", "2027-12-24", "2027-12-31"]  # close 12:30 UTC


def register_holiday_rows():
    """The rows R-AN-9 lists (generated 2026-10-23), as (date, exchange, kind, close_utc) tuples, sorted."""
    rows = []
    for d in _NYSE_FULL:
        rows.append((d, "NYSE", "full", ""))
    for d in _NYSE_EARLY:
        rows.append((d, "NYSE", "early", f"{d}T18:00:00Z"))
    for d in _LSE_FULL:
        rows.append((d, "LSE", "full", ""))
    for d in _LSE_EARLY:
        rows.append((d, "LSE", "early", f"{d}T12:30:00Z"))
    return sorted(rows)


class Holidays:
    """The judge's holiday view: the register's rows up to the horizon, plus rows the analyst table appends after it
    (an OPS append, R-AN-9). `table_rows` are the analyst table's rows as read (for the comparison finding)."""

    def __init__(self, table_bytes: bytes | None):
        self.rows = {}
        for d, ex, kind, close in register_holiday_rows():
            self.rows[(d, ex)] = (kind, close)
        self.table_rows = []
        self.horizon = HOLIDAY_HORIZON
        self.table_error = None
        if table_bytes is not None:
            try:
                rd = csv.reader(io.StringIO(table_bytes.decode("utf-8")))
                header = next(rd)
                if header != ["date", "exchange", "kind", "close_utc"]:
                    raise ValueError("header")
                for r in rd:
                    if not r:
                        continue
                    self.table_rows.append(tuple(r))
            except Exception as e:  # noqa: BLE001
                self.table_error = f"unreadable table ({type(e).__name__})"
        for r in self.table_rows:
            if len(r) == 4:
                try:
                    d = dt.date.fromisoformat(r[0])
                except ValueError:
                    continue
                if d > HOLIDAY_HORIZON:
                    self.rows[(r[0], r[1])] = (r[2], r[3])
                    if d > self.horizon:
                        self.horizon = d

    def mismatch_up_to_horizon(self):
        """Rows <= the register's horizon that differ from the register (both directions)."""
        reg = set(register_holiday_rows())
        tab = {r for r in self.table_rows if len(r) == 4 and r[0] <= HOLIDAY_HORIZON.isoformat()}
        return sorted(reg - tab), sorted(tab - reg)

    def is_holiday(self, day: dt.date, exchange: str) -> bool:
        return (day.isoformat(), exchange) in self.rows

    def is_nyse_full(self, day: dt.date) -> bool:
        r = self.rows.get((day.isoformat(), "NYSE"))
        return r is not None and r[0] == "full"

    def covers(self, day: dt.date) -> bool:
        return day <= self.horizon

    def window_holiday(self, win: Window) -> bool:
        return self.is_holiday(win.day, win.exchange)


# ---------------------------------------------------------------- event calendars (R-AN-10, R-V1-15)

BLS_SHA256 = "7e829d6c5acd861c276552d4086a2c639cbec03fc66242b859cd9d4fabdef45d"
FOMC_SHA256 = "dd709aee9821d0e1b4a1e9e0a8939422e92bb9520961090e31e96f4d30dc10f5"
CAL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "calendars")


class Events:
    def __init__(self, bls_bytes: bytes, fomc_bytes: bytes):
        if sha256_bytes(bls_bytes) != BLS_SHA256 or sha256_bytes(fomc_bytes) != FOMC_SHA256:
            raise ValueError("event calendar sha256 differs from R-V1-15")
        self.by_kind = {"nfp": [], "cpi": [], "fomc": []}
        for r in csv.DictReader(io.StringIO(bls_bytes.decode("utf-8"))):
            ts = dt.datetime.fromisoformat(r["ts_utc"]).astimezone(UTC)
            kind = {"NFP": "nfp", "CPI": "cpi"}.get(r["event"])
            if kind:
                self.by_kind[kind].append(ts)
        for r in csv.DictReader(io.StringIO(fomc_bytes.decode("utf-8"))):
            ts = dt.datetime.strptime(r["utc_timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            self.by_kind["fomc"].append(ts)
        for k in self.by_kind:
            self.by_kind[k] = sorted(set(self.by_kind[k]))
        self.last_model_day = {k: max(to_server(t).date() for t in v) for k, v in self.by_kind.items()}

    @classmethod
    def judge_copies(cls):
        with open(os.path.join(CAL_DIR, "bls_2026_2027.csv"), "rb") as f:
            b = f.read()
        with open(os.path.join(CAL_DIR, "fomc_2026_2027.csv"), "rb") as f:
            fo = f.read()
        return cls(b, fo)

    def flags(self, model_day: dt.date):
        out = {}
        for k, ts in self.by_kind.items():
            if model_day > self.last_model_day[k]:
                out[k] = None
            else:
                out[k] = 1 if any(to_server(t).date() == model_day for t in ts) else 0
        return out

    def times(self, model_day: dt.date):
        ts = sorted({t for v in self.by_kind.values() for t in v if to_server(t).date() == model_day})
        return [fmt_utc(t) for t in ts]

    def context_block(self, d: dt.date) -> dict:
        nd = next_weekday(d)
        return {"today": self.flags(d), "next": self.flags(nd), "today_times_utc": self.times(d),
                "next_times_utc": self.times(nd), "next_day": nd.isoformat()}
