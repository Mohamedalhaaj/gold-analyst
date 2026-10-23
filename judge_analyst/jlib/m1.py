"""The system store as mirrored into gold-analyst (`mirror/system/`), read independently.

Spec: R-V1-2 (decode), R-V1-3 (1,440 records), R-V1-5 (layout and days.csv), R-V1-11 (complete_utc), R-V1-13 (mid M1),
R-AN-12 (mirror), R-AN-13 (store reading: a file is used only if its sha256 equals its row's).
Prices are kept as int32 points (1 point = $0.001); a mid is (bid/1000.0 + ask/1000.0)/2 as R-V1-13 states.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import lzma
import struct

from .common import UTC, fmt_utc, sha256_bytes

STORE_FIRST_DAY = dt.date(2026, 9, 1)
DAYS_HEADER = ["day", "side", "status", "bytes", "sha256", "records", "vol_minutes", "fetched_utc"]
SIDES = ("BID", "ASK")
_REC = struct.Struct(">iiiiif")  # sec, open, close, low, high, volume (R-V1-2 field order)


def raw_path(prefix: str, day: dt.date, side: str) -> str:
    return f"{prefix}raw/{day.year:04d}/{day.month:02d}/{day.day:02d}/{side}.bi5"


class DaysTable:
    """days.csv: the last row per (day, side) wins (R-V1-9)."""

    def __init__(self, data: bytes):
        self.bytes = data
        self.sha256 = sha256_bytes(data)
        self.last = {}
        text = data.decode("utf-8")
        rd = csv.reader(io.StringIO(text))
        header = next(rd, None)
        if header != DAYS_HEADER:
            raise ValueError("days.csv header differs from R-V1-5")
        for r in rd:
            if not r:
                continue
            row = dict(zip(DAYS_HEADER, r))
            self.last[(row["day"], row["side"])] = row

    def row(self, day: dt.date, side: str):
        return self.last.get((day.isoformat(), side))

    def final(self, day: dt.date, side: str, now: str) -> bool:
        r = self.row(day, side)
        return r is not None and r["status"] in ("ok", "missing") and r["fetched_utc"] <= now

    def complete_utc(self, now: str) -> str:
        """R-V1-11: end (exclusive UTC midnight) of the run of days from 2026-09-01 whose BID and ASK both have a final
        row with fetched_utc <= now, among days with now >= D + 26 h."""
        d = STORE_FIRST_DAY
        while True:
            day_start = dt.datetime(d.year, d.month, d.day, tzinfo=UTC)
            if fmt_utc(day_start + dt.timedelta(hours=26)) > now:
                break
            if not (self.final(d, "BID", now) and self.final(d, "ASK", now)):
                break
            d += dt.timedelta(days=1)
        return fmt_utc(dt.datetime(d.year, d.month, d.day, tzinfo=UTC))

    def ok_days(self, now: str):
        out = set()
        for (day, side), r in self.last.items():
            if r["status"] == "ok" and r["fetched_utc"] <= now:
                out.add(day)
        return sorted(out)


def decode_day(body: bytes, day: dt.date):
    """Decode one UTC day file (R-V1-2). Returns {epoch_minute_s: (o, h, l, c, vol)} with int points.
    Raises ValueError on an invalid body. An empty body decodes to zero rows."""
    if len(body) == 0:
        return {}
    raw = lzma.decompress(body, format=lzma.FORMAT_ALONE)
    if len(raw) % _REC.size:
        raise ValueError("decompressed length is not a multiple of 24")
    base = int(dt.datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())
    out = {}
    prev = -1
    for sec, o, c, lo, hi, vol in _REC.iter_unpack(raw):
        if not (0 <= sec < 86400) or sec <= prev:
            raise ValueError("seconds outside [0, 86400) or not strictly increasing")
        prev = sec
        out[base + sec] = (o, hi, lo, c, float(vol))
    return out


class Store:
    """Minute rows of the store for a set of UTC days, per side, read through a blob reader and verified against the
    days table. `blob(path) -> bytes | None` reads the raw file (at the commit the caller chose)."""

    def __init__(self, days: DaysTable, blob, prefix: str = "mirror/system/"):
        self.days = days
        self.blob = blob
        self.prefix = prefix
        self.side_rows = {"BID": {}, "ASK": {}}
        self.loaded = set()
        self.mismatch = []      # (day, side, reason)
        self.file_info = {}     # (day, side) -> (status, sha256)

    def load_day(self, day: dt.date, now: str | None = None, require_final: bool = False):
        """Load both sides of a UTC day if their rows are ok (and fetched <= now when now is given)."""
        if day in self.loaded:
            return
        self.loaded.add(day)
        for side in SIDES:
            r = self.days.row(day, side)
            if r is None:
                self.file_info[(day, side)] = (None, None)
                continue
            if now is not None and not (r["fetched_utc"] <= now):
                self.file_info[(day, side)] = (None, None)
                continue
            self.file_info[(day, side)] = (r["status"], r["sha256"] or None)
            if r["status"] != "ok":
                continue
            p = raw_path(self.prefix, day, side)
            body = self.blob(p)
            if body is None:
                self.mismatch.append((day.isoformat(), side, "absent"))
                continue
            if sha256_bytes(body) != r["sha256"]:
                self.mismatch.append((day.isoformat(), side, "sha256"))
                continue
            try:
                rows = decode_day(body, day)
            except Exception:  # noqa: BLE001
                self.mismatch.append((day.isoformat(), side, "decode"))
                continue
            self.side_rows[side].update(rows)

    # ---- minute access (epoch seconds of the minute start)
    def side(self, side: str, t: int):
        return self.side_rows[side].get(t)

    def both(self, t: int):
        b = self.side_rows["BID"].get(t)
        a = self.side_rows["ASK"].get(t)
        if b is None or a is None:
            return None
        return b, a

    @staticmethod
    def mid(b: int, a: int) -> float:
        """R-V1-13: mid_x = (bid_x + ask_x)/2 on the doubles bid = points/1000.0."""
        return (b / 1000.0 + a / 1000.0) / 2

    def mid_bar(self, t: int):
        """The mid o, h, l, c of minute t, or None when either side is absent."""
        x = self.both(t)
        if x is None:
            return None
        (bo, bh, bl, bc, _), (ao, ah, al, ac, _) = x
        return (self.mid(bo, ao), self.mid(bh, ah), self.mid(bl, al), self.mid(bc, ac))
