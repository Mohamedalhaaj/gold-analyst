"""Dukascopy hour tick files (R-AN-16), decoded independently by the judge.

A summary keeps only integer points, so every price the context derives from a file can be recomputed exactly:
the first and last tick (ms, ask, bid), the max and min of ask + bid, and the tick count.
"""
from __future__ import annotations

import datetime as dt
import lzma
import struct

from .common import sha256_bytes

BASE_URL = "https://datafeed.dukascopy.com/datafeed"
POINTS = {"XAUUSD": 1000, "USDJPY": 1000, "EURUSD": 100000, "GBPUSD": 100000, "USDCAD": 100000, "USDCHF": 100000}
FX_PAIRS = ("EURUSD", "USDJPY", "GBPUSD", "USDCAD", "USDCHF")
FX_EXPONENTS = (-0.576 / 0.958, 0.136 / 0.958, -0.119 / 0.958, 0.091 / 0.958, 0.036 / 0.958)
_TICK = struct.Struct(">IIIff")  # ms, ask, bid, ask_volume, bid_volume


def hour_url(sym: str, hour: dt.datetime) -> str:
    return f"{BASE_URL}/{sym}/{hour.year:04d}/{hour.month - 1:02d}/{hour.day:02d}/{hour.hour:02d}h_ticks.bi5"


def classify(http: int, body: bytes | None):
    """Return (status, summary|None) for one fetch result, by R-AN-16's rules.
    summary = {ticks, first_ms, first_ask, first_bid, last_ms, last_ask, last_bid, max_sum, min_sum}."""
    if http == 404 or (http == 200 and (body is None or len(body) == 0)):
        return "empty", None
    if http != 200:
        return "error", None
    try:
        raw = lzma.decompress(body, format=lzma.FORMAT_ALONE)
    except Exception:  # noqa: BLE001
        return "invalid", None
    if len(raw) == 0 or len(raw) % _TICK.size:
        return "invalid", None
    prev = -1
    n = 0
    first = last = None
    mx = None
    mn = None
    for ms, ask, bid, _av, _bv in _TICK.iter_unpack(raw):
        if not (0 <= ms < 3_600_000) or ms < prev or ask <= 0 or bid <= 0:
            return "invalid", None
        prev = ms
        n += 1
        s = ask + bid
        if first is None:
            first = (ms, ask, bid)
        last = (ms, ask, bid)
        mx = s if mx is None or s > mx else mx
        mn = s if mn is None or s < mn else mn
    return "present", {"ticks": n, "first_ms": first[0], "first_ask": first[1], "first_bid": first[2],
                       "last_ms": last[0], "last_ask": last[1], "last_bid": last[2], "max_sum": mx, "min_sum": mn}


def tick_mid(ask: int, bid: int, sym: str) -> float:
    """R-AN-16: (ask_pts + bid_pts) / (2·points), as a double."""
    return (ask + bid) / (2 * POINTS[sym])


def sum_mid(s: int, sym: str) -> float:
    return s / (2 * POINTS[sym])


def summary_sha(body: bytes) -> str:
    return sha256_bytes(body)
