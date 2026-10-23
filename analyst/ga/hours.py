"""Dukascopy hour tick files and their admission (prereg R2; register R-AN-14..R-AN-16). FROZEN.

- URL `https://datafeed.dukascopy.com/datafeed/{SYM}/{YYYY}/{MM0:02d}/{DD:02d}/{HH:02d}h_ticks.bi5` (MM0 = month − 1).
- Body: LZMA-alone; 20-byte big-endian records (>u4 ms, >u4 ask, >u4 bid, >f4 ask_volume, >f4 bid_volume).
- Status: `present` (HTTP 200, a non-empty body that decodes, a length that is a multiple of 20, >= 1 record, every ms
  in [0, 3,600,000), ms non-decreasing, every ask and bid > 0); `empty` (404, or 200 with a 0-byte body); `invalid`
  (200 with a non-empty body that fails a `present` rule); `error` (a connection error, 429 or 5xx after the tries,
  another 4xx, or not fetched within the budget).
- Transport: one request at a time; >= 1.5 s from the end of a response to the next request; <= 3 tries, retrying only
  a connection error, 429 or 5xx; 20 s per request; a budget of 120 s from the start of the first request (no request
  starts once it is spent); User-Agent `Mozilla/5.0 (gold-analyst context)`.
- Provenance per file: sym, hour_utc, url, http (0 = connection error), bytes, sha256 ("" without a body), ticks,
  status, fetched_utc, tries.
"""
import hashlib
import lzma
import struct
import time
import urllib.error
import urllib.request

from . import rec

URL = "https://datafeed.dukascopy.com/datafeed/{sym}/{y:04d}/{m0:02d}/{d:02d}/{h:02d}h_ticks.bi5"
UA = "Mozilla/5.0 (gold-analyst context)"
POINTS = {"XAUUSD": 1000, "EURUSD": 100000, "USDJPY": 1000, "GBPUSD": 100000, "USDCAD": 100000, "USDCHF": 100000}
FX_PAIRS = ("EURUSD", "USDJPY", "GBPUSD", "USDCAD", "USDCHF")
FX_EXPONENTS = (-0.576 / 0.958, 0.136 / 0.958, -0.119 / 0.958, 0.091 / 0.958, 0.036 / 0.958)
PACE_S = 1.5
TRIES = 3
TIMEOUT_S = 20
BUDGET_S = 120.0
HOUR_MS = 3_600_000
_TICK = struct.Struct(">IIIff")


def url(sym, hour):
    return URL.format(sym=sym, y=hour.year, m0=hour.month - 1, d=hour.day, h=hour.hour)


def parse_ticks(body):
    """[(ms, ask, bid, ask_vol, bid_vol)] or raise ValueError (any `present` rule broken)."""
    data = lzma.decompress(body, format=lzma.FORMAT_ALONE)
    if len(data) % _TICK.size:
        raise ValueError("length is not a multiple of 20")
    ticks = list(_TICK.iter_unpack(data))
    if not ticks:
        raise ValueError("no record")
    prev = -1
    for ms, ask, bid, _av, _bv in ticks:
        if not (0 <= ms < HOUR_MS) or ms < prev:
            raise ValueError("ms out of range or decreasing")
        if ask <= 0 or bid <= 0:
            raise ValueError("non-positive price")
        prev = ms
    return ticks


def classify(http, body):
    """(status, ticks) for one final response."""
    if http == 404 or (http == 200 and body is not None and len(body) == 0):
        return "empty", []
    if http == 200:
        try:
            return "present", parse_ticks(body)
        except Exception:  # noqa: BLE001 - any decode failure is `invalid`
            return "invalid", []
    return "error", []


def tick_mid(sym, ask, bid):
    return (ask + bid) / (2 * POINTS[sym])


def urllib_get(u, timeout):
    """(status, body); status 0 = a connection error (body None)."""
    try:
        req = urllib.request.Request(u, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        return 0, None


class Fetcher:
    """Sequential, paced, budgeted fetch of hour files. `get(url, timeout) -> (http, body)`; `clock()` monotonic
    seconds; `sleep(s)`; `now()` aware UTC (the runner clock, for fetched_utc)."""

    def __init__(self, get=urllib_get, clock=time.monotonic, sleep=time.sleep, now=None, budget_s=BUDGET_S):
        self.get, self.clock, self.sleep = get, clock, sleep
        self.now = now
        self.budget_s = budget_s
        self.t_start = None
        self.last_end = None

    def spent(self):
        return self.t_start is not None and self.clock() - self.t_start >= self.budget_s

    def fetch(self, sym, hour):
        u = url(sym, hour)
        prov = {"sym": sym, "hour_utc": rec.iso(hour), "url": u, "http": 0, "bytes": 0, "sha256": "", "ticks": 0,
                "status": "error", "fetched_utc": None, "tries": 0}
        http, body = 0, None
        for _ in range(TRIES):
            if self.spent():
                break
            if self.last_end is not None:
                wait = self.last_end + PACE_S - self.clock()
                if wait > 0:
                    self.sleep(wait)
                if self.spent():
                    break
            if self.t_start is None:
                self.t_start = self.clock()
            prov["tries"] += 1
            http, body = self.get(u, TIMEOUT_S)
            self.last_end = self.clock()
            prov["fetched_utc"] = rec.iso(self.now()) if self.now else None
            if not (http == 0 or http == 429 or 500 <= http <= 599):
                break
        prov["http"] = int(http)
        if body:
            prov["bytes"] = len(body)
            prov["sha256"] = hashlib.sha256(body).hexdigest()
        if prov["tries"] == 0:
            return prov, []
        status, ticks = classify(http, body)
        prov["status"] = status
        prov["ticks"] = len(ticks)
        return prov, ticks
