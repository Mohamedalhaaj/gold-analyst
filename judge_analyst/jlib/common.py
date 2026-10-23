"""Shared helpers of the analyst judge: record forms, hashes, time formats, findings, tolerances.

Spec: IMPLEMENTATION_REGISTER §16 (R-AN-3 serialization, R-AN-68 tolerances, R-JV-1 finding keys). Built blind from
the prereg and the register only; nothing here is taken from the analyst code.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
from dataclasses import dataclass, field

UTC = dt.timezone.utc
ZERO64 = "0" * 64

# ---------------------------------------------------------------- serialization (R-AN-3)


def record_form(obj) -> bytes:
    """The record form of a JSON record: the bytes a create-only record file must hold."""
    return (json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def compact_form(obj) -> str:
    """The compact form (context hash, witness comment payload)."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_compact(obj) -> str:
    return sha256_bytes(compact_form(obj).encode("ascii"))


def load_json_bytes(b: bytes):
    """Parse a record. NaN/Infinity literals are refused (allow_nan=False on write means they never appear)."""
    def _bad(x):
        raise ValueError(f"non-finite literal {x}")
    return json.loads(b.decode("utf-8"), parse_constant=_bad)


# ---------------------------------------------------------------- times (R-C4, R-AN-3)

_RE_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_RE_UTC_MS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_RE_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RE_SERVER = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")


def fmt_utc(t: dt.datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_utc_ms(t: dt.datetime) -> str:
    t = t.astimezone(UTC)
    return t.strftime("%Y-%m-%dT%H:%M:%S") + f".{t.microsecond // 1000:03d}Z"


def fmt_server(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M")


def fmt_day(d: dt.date) -> str:
    return d.isoformat()


def parse_utc(s: str) -> dt.datetime:
    """Parse an R-C4 time (also accepts the ms form and the store's `YYYY-MM-DD HH:MM` complete form)."""
    if not isinstance(s, str):
        raise ValueError(f"not a time string: {s!r}")
    if _RE_UTC.match(s):
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    if _RE_UTC_MS.match(s):
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    raise ValueError(f"not an R-C4 time: {s!r}")


def is_utc(s) -> bool:
    return isinstance(s, str) and bool(_RE_UTC.match(s))


def is_utc_ms(s) -> bool:
    return isinstance(s, str) and bool(_RE_UTC_MS.match(s))


def is_day(s) -> bool:
    return isinstance(s, str) and bool(_RE_DAY.match(s))


def is_server(s) -> bool:
    return isinstance(s, str) and bool(_RE_SERVER.match(s))


def norm_api_time(s: str) -> str:
    """GitHub API timestamps → R-C4: fractional seconds truncated, offset converted to Z (R-AN-3)."""
    if s is None:
        return None
    x = s.strip()
    if x.endswith("Z"):
        x = x[:-1] + "+00:00"
    t = dt.datetime.fromisoformat(x)
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return fmt_utc(t.replace(microsecond=0))


def utc_ms(t: dt.datetime) -> int:
    return int(round(t.timestamp() * 1000))


def add_s(s: str, seconds: int) -> str:
    return fmt_utc(parse_utc(s) + dt.timedelta(seconds=seconds))


# ---------------------------------------------------------------- tolerances (R-AN-68)

REL = 1e-9          # floating fields (R-J5-2 analogue)
GRID = 1e-12        # "exact" prices built from integer points (judge decision: grid-exact, see README)


def rel_eq(a, b, rel: float = REL) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    try:
        a = float(a)
        b = float(b)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(a) and math.isfinite(b)):
        return False
    return abs(a - b) <= rel * max(abs(a), abs(b))


def price_eq(a, b) -> bool:
    """A price made from integer points: equal on its grid. Judge decision: |a − b| <= 1e-12·max(1, |a|, |b|), far
    below any grid step ($0.0005 for a mid), so a last-bit difference between two decimal-to-double routes is not a
    finding while every real difference is."""
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool) or not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return False
    if not (math.isfinite(a) and math.isfinite(b)):
        return False
    return abs(a - b) <= GRID * max(1.0, abs(a), abs(b))


def exact_eq(a, b) -> bool:
    """Strings, enums, integers, times (R-AN-68 exact). An int and a float of the same value are NOT equal for integer
    fields; the caller uses num_exact for numeric fields that may be stored as float."""
    if type(a) is bool or type(b) is bool:
        return a is b
    return type(a) is type(b) and a == b


def num_exact(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return False
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return False
    return float(a) == float(b)


# ---------------------------------------------------------------- findings (R-JV-1)

VIOLATION = "VIOLATION"
WARNING = "WARNING"


@dataclass
class Finding:
    check: str            # A0 (inputs) or A1..A11 (R-AN-67)
    severity: str         # VIOLATION | WARNING
    code: str             # the judge's reason code
    path: str             # repository path concerned (or a ref / an anchor)
    commit: str = "-"     # the commit that introduced it, or "-"
    detail: str = ""      # enums, field names, hashes and numbers only: never model text
    owner_attested: bool = False

    @property
    def key(self) -> str:
        return f"{self.check}|{self.code}|{self.path}|{self.commit}"


@dataclass
class Findings:
    items: list = field(default_factory=list)

    def add(self, check, severity, code, path, commit="-", detail="", owner_attested=False):
        f = Finding(check, severity, code, path, commit or "-", detail, owner_attested)
        if all(x.key != f.key for x in self.items):
            self.items.append(f)
        return f

    def v(self, check, code, path, commit="-", detail="", owner_attested=False):
        return self.add(check, VIOLATION, code, path, commit, detail, owner_attested)

    def w(self, check, code, path, commit="-", detail="", owner_attested=False):
        return self.add(check, WARNING, code, path, commit, detail, owner_attested)

    def by_check(self, check):
        return [f for f in self.items if f.check == check]

    def keys(self, severity=None):
        return [f.key for f in self.items if severity is None or f.severity == severity]


def safe_detail(*parts) -> str:
    """Join detail parts, keeping only printable ASCII so no model text can slip into a log or report."""
    s = "; ".join(str(p) for p in parts if p is not None and p != "")
    return "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in s)[:400]
