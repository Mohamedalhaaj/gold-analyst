"""Frozen tables: holidays (R-AN-9) and the event calendars (R-AN-10, R-AN-20). FROZEN.

- holidays.csv `date,exchange,kind,close_utc`: a window is `skipped:holiday` iff its date has a row (full or early) for
  its own exchange (W1 LSE, W2 NYSE). A window after the table's last date has no table row (`no_table`).
- An NYSE `full` row also makes the XAU trading hours of that model date optional (R-AN-15).
- Events: BLS `ts_utc` (NFP, CPI) and FOMC `utc_timestamp` (all rows), each mapped to its model date
  (to_server(ts).date()). Flags for model day d: `today` = events whose model date is d, `next` = the next weekday.
  After a kind's last event, that kind's flags are null for later model days.
- §16.18 A12-D17/D18: the macro calendar (`ts_utc`, all rows; PPI, JOLTS, GDP, PCE, RETAIL, ISM_MFG, ISM_SVC,
  CLAIMS, ECB, kind = event lower-cased) gives nine more kinds, after nfp, cpi, fomc. Each flag is 1 iff an event of
  that kind has model date d, else 0, or null after the kind's last event. `today_times_utc` / `next_times_utc` list
  `{"kind", "utc"}` once per (kind, time), sorted by utc then kind. The macro file is data outside the frozen manifest,
  refreshed only by appending (A12-D19); the judge reads it at each call's repo_head_sha.
"""
import csv
import datetime as dt
import io

from . import clock, rec

HOLIDAYS_PATH = "analyst/tables/holidays.csv"
BLS_PATH = "analyst/calendars/bls_2026_2027.csv"
FOMC_PATH = "analyst/calendars/fomc_2026_2027.csv"
MACRO_PATH = "analyst/calendars/macro_2026_2027.csv"
MACRO_KINDS = ("ppi", "jolts", "gdp", "pce", "retail", "ism_mfg", "ism_svc", "claims", "ecb")
KINDS = ("nfp", "cpi", "fomc") + MACRO_KINDS


class Holidays:
    def __init__(self, text):
        self.rows = list(csv.DictReader(io.StringIO(text)))
        self.by = {}
        for r in self.rows:
            self.by[(r["date"], r["exchange"])] = r
        self.horizon = max(dt.date.fromisoformat(r["date"]) for r in self.rows)

    def has_table(self, day):
        return day <= self.horizon

    def row(self, day, exchange):
        return self.by.get((day.isoformat(), exchange))

    def is_holiday(self, window):
        """W1, W1b: an LSE row; W2, W2b: an NYSE row; a trigger window: a row of either exchange (§16.18 A14,
        R-AN-9)."""
        if window.exchange is None:
            return self.any_row(window.day)
        return self.row(window.day, window.exchange) is not None

    def any_row(self, day):
        return self.row(day, "LSE") is not None or self.row(day, "NYSE") is not None

    def nyse_full(self, day):
        r = self.row(day, "NYSE")
        return r is not None and r["kind"] == "full"


def _ts_utc(s):
    return dt.datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S+00:00").replace(tzinfo=rec.UTC)


class Events:
    def __init__(self, bls_text, fomc_text, macro_text):
        self.events = {k: [] for k in KINDS}
        for r in csv.DictReader(io.StringIO(bls_text)):
            ev = r["event"].strip().lower()
            if ev in ("nfp", "cpi"):
                self.events[ev].append(_ts_utc(r["ts_utc"]))
        for r in csv.DictReader(io.StringIO(fomc_text)):
            self.events["fomc"].append(rec.parse_iso(r["utc_timestamp"].strip()))
        for r in csv.DictReader(io.StringIO(macro_text)):
            ev = r["event"].strip().lower()
            if ev in MACRO_KINDS:
                self.events[ev].append(_ts_utc(r["ts_utc"]))
        for k in KINDS:
            self.events[k] = sorted(set(self.events[k]))
        self.last_model_date = {k: clock.to_server(v[-1]).date() for k, v in self.events.items() if v}

    def _flags(self, day):
        flags, times = {}, []
        for k in KINDS:
            last = self.last_model_date.get(k)
            if last is None or day > last:
                flags[k] = None
                continue
            hit = [t for t in self.events[k] if clock.to_server(t).date() == day]
            flags[k] = 1 if hit else 0
            times.extend((rec.iso(t), k) for t in hit)
        return flags, [{"kind": k, "utc": u} for u, k in sorted(set(times))]

    def context_block(self, day):
        """R-AN-20 (A12-D18): {today, next (12 flags each), today_times_utc, next_times_utc ([{kind, utc}]),
        next_day}."""
        nd = clock.next_weekday(day)
        tf, tt = self._flags(day)
        nf, nt = self._flags(nd)
        return {"today": tf, "next": nf, "today_times_utc": tt, "next_times_utc": nt, "next_day": nd.isoformat()}


def load_holidays(view):
    return Holidays(view.read(HOLIDAYS_PATH).decode("utf-8"))


def load_events(view):
    return Events(view.read(BLS_PATH).decode("utf-8"), view.read(FOMC_PATH).decode("utf-8"),
                  view.read(MACRO_PATH).decode("utf-8"))
