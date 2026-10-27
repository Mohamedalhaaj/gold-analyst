"""Twelve Data requests of the session context (register R-AN-87, §16.18 A9). FROZEN.

The session's only market-data requests, made inside `run_window.py context`: XAUUSD (when the XAU hour list is not
empty), the five pairs of the dollar proxy, AUDJPY (the risk proxy, symbol AUD/JPY, §16.18 A12: the pairs' URL form,
transport, classification and provenance), then `fresh`, one at a time: at most 8 requests a window.

- URLs: `https://api.twelvedata.com/time_series?symbol=<SYM>&interval=1h&start_date=<A>&end_date=<B>&timezone=UTC
  &order=asc&outputsize=5000&dp=5` (A, B as `YYYY-MM-DDTHH:MM:SS` UTC, the symbol's `/` as it is) and FRESH_URL.
- No request carries an `apikey` parameter or an `Authorization` header, and this code never reads, holds or prints
  a key: the agent proxy adds `Authorization: apikey <key>` after the request leaves the VM (R-AN-61). The only header
  set is the User-Agent.
- Transport: one request at a time; >= 8.0 s from the start of one try to the start of the next, whatever the
  request; 10 s per try; at most 2 tries, the second only after td_code 0 or 500-599; a budget of 120 s from the
  start of the first try (no try starts once it is spent).
- td_code: the body's integer `code` when the body is a UTF-8 JSON object whose `status` is "error" (Twelve Data can
  send its errors with HTTP 200); otherwise the HTTP status, 0 for a connection error or a timeout.
- Classification of the last try: `ok` iff HTTP 200, a body of at most 262,144 bytes that is a UTF-8 JSON object with
  `status` "ok", `values` a list, each element with a strict `datetime` (minute and second 00 for 1h, second 00 for
  1min) and `open`, `high`, `low`, `close` decimal strings, all > 0, l <= min(o, c), h >= max(o, c), no repeated
  datetime; otherwise `error` with a reason (key, plan, credits, server, invalid, other; budget when no try started).
- Provenance per request: src, req, url, http, td_code, status, reason, bytes, sha256, values_n, bars (filled by the
  context with the bars it uses), fetched_utc, tries. No raw body, `meta` or `message` is kept (decision A9-D6).

§16.18 A14 (R-AN-90): a trigger window's session makes one more request in `start`, `XAUUSD_M30` (m30_url, interval
30min: an element's `datetime` has minute 00 or 30 and second 00), with the same transport, classification and
provenance; at most 9 requests a window. `start` keeps the monotonic time of the request's last try (`last_start`)
in the session's scratch state so that `context` keeps the 8.0 s pacing across the two steps.

Interpretations (logged for the register): a price must also be finite (a decimal string too long for a double
overflows to inf, which a record cannot hold, R-AN-3); a body is read up to 262,145 bytes, so `bytes` and `sha256` of a
longer body are those of its first 262,145 bytes, and it is classified `invalid`; any exception of the transport
(not only URLError) is a connection error, td_code 0.
"""
import datetime as dt
import hashlib
import json
import math
import re
import time
import urllib.error
import urllib.request

from . import rec

BASE_URL = "https://api.twelvedata.com/time_series"
FRESH_URL = BASE_URL + "?symbol=XAU/USD&interval=1min&outputsize=1&timezone=UTC&dp=5"
UA = "Mozilla/5.0 (gold-analyst context)"
SYMBOLS = {"XAUUSD": "XAU/USD", "EURUSD": "EUR/USD", "USDJPY": "USD/JPY", "GBPUSD": "GBP/USD", "USDCAD": "USD/CAD",
           "USDCHF": "USD/CHF", "AUDJPY": "AUD/JPY"}
PACE_S = 8.0
TIMEOUT_S = 10
TRIES = 2
BUDGET_S = 120.0
MAX_BYTES = 262_144
PRICE_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")
DATETIME_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}$")
REASONS = {401: "key", 403: "plan", 429: "credits"}


def _stamp(t):
    return t.astimezone(rec.UTC).strftime("%Y-%m-%dT%H:%M:%S")


def series_url(sym, a, b):
    """The 1h URL of instrument `sym` (an R-AN-19 name or XAUUSD) from `a` to `b` (aware UTC)."""
    return (f"{BASE_URL}?symbol={SYMBOLS[sym]}&interval=1h&start_date={_stamp(a)}&end_date={_stamp(b)}"
            f"&timezone=UTC&order=asc&outputsize=5000&dp=5")


def m30_url(a, b):
    """The XAUUSD_M30 URL (R-AN-90) from `a` to `b` (aware UTC)."""
    return (f"{BASE_URL}?symbol=XAU/USD&interval=30min&start_date={_stamp(a)}&end_date={_stamp(b)}"
            f"&timezone=UTC&order=asc&outputsize=5000&dp=5")


def urllib_get(u, timeout):
    """(status, body); status 0 = a connection error or a timeout (body None). An HTTP error keeps its body, where
    Twelve Data puts its error code."""
    try:
        req = urllib.request.Request(u, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        try:
            body = e.read(MAX_BYTES + 1)
        except Exception:  # noqa: BLE001 - the status alone is kept
            body = b""
        return e.code, body
    except Exception:  # noqa: BLE001 - any transport failure is a connection error
        return 0, None


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _doc(body):
    if not body:
        return None
    try:
        doc = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def td_code(http, body):
    doc = _doc(body)
    if doc is not None and doc.get("status") == "error" and _is_int(doc.get("code")):
        return doc["code"]
    return int(http or 0)


def _price(v):
    if not isinstance(v, str) or not PRICE_RE.match(v):
        raise ValueError("price")
    x = float(v)
    if not (math.isfinite(x) and x > 0):
        raise ValueError("price")
    return x


def parse_values(values, interval):
    """[(t aware UTC, o, h, l, c)] in the response's order, or raise ValueError (any `ok` rule broken)."""
    if not isinstance(values, list):
        raise ValueError("values")
    out, seen = [], set()
    for v in values:
        if not isinstance(v, dict) or not isinstance(v.get("datetime"), str) or not DATETIME_RE.match(v["datetime"]):
            raise ValueError("datetime")
        t = dt.datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=rec.UTC)
        bad_min = (interval == "1h" and t.minute != 0) or (interval == "30min" and t.minute not in (0, 30))
        if t.second != 0 or bad_min:
            raise ValueError("datetime")
        if t in seen:
            raise ValueError("duplicate")
        seen.add(t)
        o, h, l, c = (_price(v.get(k)) for k in ("open", "high", "low", "close"))
        if not (l <= min(o, c) and h >= max(o, c)):
            raise ValueError("range")
        out.append((t, o, h, l, c))
    return out


def classify(http, body, interval):
    """(status, reason, td_code, values) for one final response; `interval` is "1h", "30min" or "1min"."""
    code = td_code(http, body)
    if http == 200 and code == 200 and body is not None and len(body) <= MAX_BYTES:
        doc = _doc(body)
        if doc is not None and doc.get("status") == "ok":
            try:
                return "ok", None, 200, parse_values(doc.get("values"), interval)
            except ValueError:
                pass
    if code in REASONS:
        reason = REASONS[code]
    elif code == 0 or 500 <= code <= 599:
        reason = "server"
    elif http == 200 and code == 200:
        reason = "invalid"
    else:
        reason = "other"
    return "error", reason, code, []


def outcome(status, reason, code):
    """The probe outcome of a classification (R-AN-84 `net.twelvedata`): 200 for `ok`, −1 for `invalid`, else
    td_code."""
    if status == "ok":
        return 200
    if reason == "invalid":
        return -1
    return int(code)


def prov_outcome(prov):
    return outcome(prov["status"], prov["reason"], prov["td_code"])


def bar_obj(v):
    t, o, h, l, c = v
    return {"t_utc": rec.iso(t), "o": o, "h": h, "l": l, "c": c}


class Fetcher:
    """Sequential, paced, budgeted Twelve Data requests. `get(url, timeout) -> (http, body)`; `clock()` monotonic
    seconds; `sleep(s)`; `now()` aware UTC (the session clock, for fetched_utc)."""

    def __init__(self, get=urllib_get, clock=time.monotonic, sleep=time.sleep, now=None, budget_s=BUDGET_S):
        self.get, self.clock, self.sleep = get, clock, sleep
        self.now = now
        self.budget_s = budget_s
        self.t_start = None
        self.last_start = None

    def spent(self):
        return self.t_start is not None and self.clock() - self.t_start >= self.budget_s

    def fetch(self, req, u, interval):
        """(provenance, values) of request `req` (XAUUSD, a pair, AUDJPY or `fresh`) at URL `u`; values [] unless
        ok."""
        prov = {"src": "td", "req": req, "url": u, "http": 0, "td_code": 0, "status": "error", "reason": "budget",
                "bytes": 0, "sha256": "", "values_n": 0, "bars": [], "fetched_utc": None, "tries": 0}
        http, body = 0, None
        for _ in range(TRIES):
            if self.spent():
                break
            if self.last_start is not None:
                wait = self.last_start + PACE_S - self.clock()
                if wait > 0:
                    self.sleep(wait)
                if self.spent():
                    break
            self.last_start = self.clock()
            if self.t_start is None:
                self.t_start = self.last_start
            prov["tries"] += 1
            http, body = self.get(u, TIMEOUT_S)
            prov["fetched_utc"] = rec.iso(self.now()) if self.now else None
            code = td_code(http, body)
            if not (code == 0 or 500 <= code <= 599):
                break
        if prov["tries"] == 0:
            return prov, []
        status, reason, code, values = classify(int(http or 0), body, interval)
        prov.update(http=int(http or 0), td_code=int(code), status=status, reason=reason)
        if body:
            prov["bytes"] = len(body)
            prov["sha256"] = hashlib.sha256(body).hexdigest()
        prov["values_n"] = len(values)
        return prov, values
