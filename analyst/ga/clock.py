"""Windows, the model clock and weekday arithmetic (prereg §1, register R-C1..R-C3, R-AN-4, R-AN-8). FROZEN.

- W1: entry 08:00, forced exit 12:00 Europe/London; W2: entry 09:30, forced exit 13:30 America/New_York; weekdays only.
  UTC times come from zoneinfo (the vendored tzdata), never typed.
- deadline = entry − 15 min; decision span = [entry − 45, entry − 15]; start interval = [entry − 45, entry − 15 − T_run].
- Model (platform server) clock = New York wall clock + 7 h (core.to_server); model_to_utc raises on a repeated or
  skipped New York time (weekday model times never hit one).
- busday_count(a, b) = numpy.busday_count with the Monday-Friday mask and no holidays (holidays not removed, R-C2).
"""
import datetime as dt
import re
from zoneinfo import ZoneInfo

from . import rec

UTC = rec.UTC
LONDON = ZoneInfo("Europe/London")
NEW_YORK = ZoneInfo("America/New_York")
TRIPOLI = ZoneInfo("Africa/Tripoli")
SEVEN = dt.timedelta(hours=7)
MIN = dt.timedelta(minutes=1)
HOUR = dt.timedelta(hours=1)

WINDOWS = ("W1", "W2")
_SPEC = {"W1": (LONDON, (8, 0), (12, 0)), "W2": (NEW_YORK, (9, 30), (13, 30))}
EXCHANGE = {"W1": "LSE", "W2": "NYSE"}
LOCAL_TZ = {"W1": LONDON, "W2": NEW_YORK}
CALL_ID_RE = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})_(W[12])")


def to_server(t):
    """Aware time -> naive model-clock time (New York + 7 h)."""
    return t.astimezone(NEW_YORK).replace(tzinfo=None) + SEVEN


def model_to_utc(naive):
    """Naive model-clock time -> aware UTC; raises on an ambiguous or nonexistent New York time."""
    ny = naive - SEVEN
    a, b = ny.replace(tzinfo=NEW_YORK, fold=0), ny.replace(tzinfo=NEW_YORK, fold=1)
    if a.utcoffset() != b.utcoffset():
        raise ValueError(f"model time {naive} maps to an ambiguous or nonexistent New York time")
    return a.astimezone(UTC)


def is_weekday(d):
    return d.weekday() < 5


def busday_count(a, b):
    """numpy.busday_count(a, b) for the Monday-Friday mask: weekdays in [a, b); if b < a, minus the weekdays in
    (b, a] (numpy's convention)."""
    if b < a:
        one = dt.timedelta(days=1)
        return -busday_count(b + one, a + one)
    n = (b - a).days
    full, rest = divmod(n, 7)
    count = full * 5
    wd = a.weekday()
    for i in range(rest):
        if (wd + i) % 7 < 5:
            count += 1
    return count


def weekday_before(d, n):
    """The n-th weekday strictly before d (n >= 1)."""
    k, x = 0, d
    while k < n:
        x -= dt.timedelta(days=1)
        if is_weekday(x):
            k += 1
    return x


def next_weekday(d):
    x = d + dt.timedelta(days=1)
    while not is_weekday(x):
        x += dt.timedelta(days=1)
    return x


def iso_week(d):
    y, w, _ = d.isocalendar()
    return f"{y:04d}-W{w:02d}"


def call_id(day, name):
    return f"{day.isoformat()}_{name}"


def parse_call_id(cid):
    m = CALL_ID_RE.fullmatch(cid or "")
    if not m:
        raise ValueError(f"bad call_id {cid!r}")
    return dt.date.fromisoformat(m.group(1)), m.group(2)


def window_key(cid):
    """Window order: model day, then W1 before W2."""
    d, w = parse_call_id(cid)
    return (d, w)


class Window:
    """One window of one model day (its local date, which equals its UTC and model-clock date, R-AN-4)."""

    def __init__(self, day, name):
        if name not in _SPEC:
            raise ValueError(name)
        tz, e, x = _SPEC[name]
        self.day, self.name = day, name
        self.call_id = call_id(day, name)
        self.entry_utc = dt.datetime(day.year, day.month, day.day, *e, tzinfo=tz).astimezone(UTC)
        self.exit_utc = dt.datetime(day.year, day.month, day.day, *x, tzinfo=tz).astimezone(UTC)
        self.deadline_utc = self.entry_utc - dt.timedelta(minutes=15)
        self.span_start_utc = self.entry_utc - dt.timedelta(minutes=45)
        self.exchange = EXCHANGE[name]
        self.local_tz = tz

    def start_interval(self, T_run_min):
        return self.span_start_utc, self.deadline_utc - dt.timedelta(minutes=T_run_min)

    def in_start_interval(self, now, T_run_min):
        a, b = self.start_interval(T_run_min)
        return a <= now <= b

    @property
    def entry_server(self):
        return to_server(self.entry_utc)

    @property
    def exit_server(self):
        return to_server(self.exit_utc)

    @property
    def slot(self):
        """R-AN-21 / R-C3: the model hour containing the entry minute, minus 1."""
        return self.entry_server.hour - 1

    @property
    def path_minutes(self):
        n = int((self.exit_utc - self.entry_utc) / MIN)
        return [self.entry_utc + i * MIN for i in range(n)]

    def key(self):
        return (self.day, self.name)

    def __repr__(self):
        return f"Window({self.call_id})"


def windows_on(day):
    return [Window(day, w) for w in WINDOWS] if is_weekday(day) else []


def window_of(cid):
    d, w = parse_call_id(cid)
    return Window(d, w)


def floor_hour(t):
    return t.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def hours_between(a, b):
    """UTC hour starts h with a <= h <= b (a, b hour-aligned)."""
    out, h = [], a
    while h <= b:
        out.append(h)
        h += HOUR
    return out


def days_range(a, b):
    """Dates a..b inclusive."""
    out, x = [], a
    while x <= b:
        out.append(x)
        x += dt.timedelta(days=1)
    return out


def add_months(d, months):
    """pandas.DateOffset(months=n) on a date: same day-of-month, clamped to the month's last day."""
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    last = (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)).day
    return dt.date(y, m, min(d.day, last))
