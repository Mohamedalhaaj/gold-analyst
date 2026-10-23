"""The mirrored system store (register R-AN-12, R-AN-13; R-V1-2, R-V1-11, R-V1-13). FROZEN.

- `mirror/system/days.csv` is a byte copy of gold-forward-watch `data/system/days.csv`
  (`day,side,status,bytes,sha256,records,vol_minutes,fetched_utc`); the raw day files are under
  `mirror/system/raw/YYYY/MM/DD/{BID,ASK}.bi5`.
- complete_utc (R-V1-11): the end (exclusive, UTC midnight) of the longest run of UTC days from 2026-09-01 whose BID
  and ASK both have a final row (ok or missing) with fetched_utc <= now, among days with now >= D + 26 h.
- Decoding (R-V1-2): LZMA-alone, 24-byte big-endian records (sec, open, close, low, high as int32 points of $0.001,
  volume float32); seconds in [0, 86400) and strictly increasing. Prices are kept as int points for exact
  comparisons; the mid is (bid/1000 + ask/1000)/2 as R-V1-13.
- A file is read only if its sha256 equals its row's (else StoreMismatch).
"""
import csv
import datetime as dt
import hashlib
import io
import lzma
import struct

from . import rec

DAYS_PATH = "mirror/system/days.csv"
SLOTS_PATH = "mirror/system/forecasts_slots.csv"
RAW_PREFIX = "mirror/system/raw/"
FIRST_DAY = dt.date(2026, 9, 1)
COMPLETE_AFTER = dt.timedelta(hours=26)
SIDES = ("BID", "ASK")
_REC = struct.Struct(">iiiiif")


class StoreMismatch(Exception):
    pass


def raw_path(day, side):
    return f"{RAW_PREFIX}{day.year:04d}/{day.month:02d}/{day.day:02d}/{side}.bi5"


def parse_days(text):
    return list(csv.DictReader(io.StringIO(text)))


def final_status(rows, now=None):
    """{(day, side): row}; with `now`, only rows with fetched_utc <= now (a later row wins)."""
    out = {}
    now_s = rec.iso(now) if now is not None else None
    for r in rows:
        if now_s is not None and r["fetched_utc"] > now_s:
            continue
        out[(r["day"], r["side"])] = r
    return out


def complete_utc(rows, now):
    st = final_status(rows, now)
    end = FIRST_DAY
    d = FIRST_DAY
    while dt.datetime(d.year, d.month, d.day, tzinfo=rec.UTC) + COMPLETE_AFTER <= now:
        key = d.isoformat()
        if all((key, s) in st and st[(key, s)]["status"] in ("ok", "missing") for s in SIDES):
            end = d + dt.timedelta(days=1)
            d = end
        else:
            break
    return dt.datetime(end.year, end.month, end.day, tzinfo=rec.UTC)


def decode_day(raw, day):
    """[(sec, open, close, low, high, volume)] of one UTC day file; an empty body has no records."""
    if len(raw) == 0:
        return []
    data = lzma.decompress(raw, format=lzma.FORMAT_ALONE)
    if len(data) % _REC.size:
        raise ValueError(f"{day}: {len(data)} bytes is not a whole number of 24-byte records")
    out = list(_REC.iter_unpack(data))
    prev = -1
    for r in out:
        if not (0 <= r[0] < 86400) or r[0] <= prev:
            raise ValueError(f"{day}: record seconds out of range or not increasing")
        prev = r[0]
    return out


class Minutes:
    """Minute rows by UTC epoch second of the minute start: side -> {t: (o, h, l, c, vol)} with int points."""

    def __init__(self):
        self.side = {"BID": {}, "ASK": {}}

    def add_day(self, day, side, records):
        base = int(dt.datetime(day.year, day.month, day.day, tzinfo=rec.UTC).timestamp())
        d = self.side[side]
        for sec, o, c, lo, hi, vol in records:
            t = base + sec
            if t not in d:                          # drop_duplicates(keep="first")
                d[t] = (o, hi, lo, c, float(vol))

    def bid(self, t):
        return self.side["BID"].get(t)

    def ask(self, t):
        return self.side["ASK"].get(t)

    def mid(self, t):
        b, a = self.bid(t), self.ask(t)
        if b is None or a is None:
            return None
        return tuple((b[i] / 1000.0 + a[i] / 1000.0) / 2 for i in range(4))


def load_days(view, rows_by_key, days):
    """Minutes of the given UTC days (dates). Only `ok` files, each checked against its row's sha256."""
    m = Minutes()
    used = []
    for day in sorted(days):
        if day < FIRST_DAY:
            raise AssertionError("no day before 2026-09-01 is ever loaded")
        for side in SIDES:
            row = rows_by_key.get((day.isoformat(), side))
            status = row["status"] if row else None
            sha = row["sha256"] if row else ""
            used.append({"day": day.isoformat(), "side": side, "status": status, "sha256": sha})
            if status != "ok":
                continue
            raw = view.read(raw_path(day, side))
            if raw is None or hashlib.sha256(raw).hexdigest() != sha:
                raise StoreMismatch(f"{day} {side}")
            m.add_day(day, side, decode_day(raw, day))
    return m, used
