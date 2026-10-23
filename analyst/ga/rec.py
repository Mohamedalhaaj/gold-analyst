"""Serialization and time formats (register R-AN-3, R-AN-4). FROZEN."""
import datetime as dt
import hashlib
import json
import math
import re

UTC = dt.timezone.utc
ZERO64 = "0" * 64


def record_bytes(obj):
    """Record form: indent 1, sorted keys, UTF-8 (not escaped), no NaN, final LF."""
    return (json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def compact(obj):
    """Compact form (context hash, witness comments): ASCII, sorted keys, no spaces."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def compact_sha256(obj):
    return sha256_bytes(compact(obj).encode("ascii"))


def loads(b):
    if isinstance(b, bytes):
        b = b.decode("utf-8")
    return json.loads(b)


# ---------------------------------------------------------------- times
def utc(y, m, d, hh=0, mm=0, ss=0):
    return dt.datetime(y, m, d, hh, mm, ss, tzinfo=UTC)


def floor_s(t):
    return t.astimezone(UTC).replace(microsecond=0)


def iso(t):
    """R-C4 `YYYY-MM-DDTHH:MM:SSZ` (seconds floored)."""
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_ms(t):
    t = t.astimezone(UTC)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def server_str(naive):
    return naive.strftime("%Y-%m-%d %H:%M")


def parse_iso(s):
    """`YYYY-MM-DDTHH:MM:SSZ` -> aware UTC."""
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


_GH = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})")


def normalize_gh(s):
    """A GitHub API timestamp -> R-C4 (fractional seconds truncated, offset converted to Z)."""
    m = _GH.fullmatch(s or "")
    if not m:
        raise ValueError(f"not an ISO timestamp: {s!r}")
    base = dt.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
    off = m.group(3)
    if off == "Z":
        t = base.replace(tzinfo=UTC)
    else:
        sign = 1 if off[0] == "+" else -1
        t = base.replace(tzinfo=dt.timezone(sign * dt.timedelta(hours=int(off[1:3]), minutes=int(off[4:6]))))
    return iso(t)


def epoch_ms(t):
    return int(round(t.astimezone(UTC).timestamp() * 1000))


def day_str(d):
    return d.isoformat()


def finite(x):
    return x is not None and isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
