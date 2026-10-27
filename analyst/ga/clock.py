"""Windows, the model clock and weekday arithmetic (prereg §1, register R-C1..R-C3, R-AN-4, R-AN-8). FROZEN.

- W1: entry 08:00, forced exit 12:00 Europe/London; W2: entry 09:30, forced exit 13:30 America/New_York; weekdays only.
  UTC times come from zoneinfo (the vendored tzdata), never typed.
- deadline = entry − 15 min; decision span = [entry − 45, entry − 15]; start interval = [entry − 45, entry − 15 − T_run].
- Model (platform server) clock = New York wall clock + 7 h (core.to_server); model_to_utc raises on a repeated or
  skipped New York time (weekday model times never hit one).
- §16.18 A14 (R-AN-4, R-AN-8, R-AN-89): W1b = W1 + 120 min and W2b = W2 + 120 min in UTC; trigger windows
  `<model_day>_T<hhmm>` (hhmm = the check's UTC time; a check at 22:00-23:59 UTC belongs to the next date's model
  day). Every new window: entry = check + 30 min, exit = entry + 240 min, deadline = entry − 15 min, span start =
  entry − 30 min. tday(x) = the UTC date of x + 2 h; G(d) = 22:30 UTC of d − 1 + k·30 min up to L_d =
  model_to_utc((d+1) 00:00) − 270 min. Window order is (model_day, entry_utc).
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

HALF = dt.timedelta(minutes=30)

WINDOWS = ("W1", "W2")
SECOND_LOOKS = ("W1b", "W2b")
SCHEDULED = WINDOWS + SECOND_LOOKS
WINDOW_TYPES = SCHEDULED + ("T",)
_SPEC = {"W1": (LONDON, (8, 0), (12, 0)), "W2": (NEW_YORK, (9, 30), (13, 30))}
EXCHANGE = {"W1": "LSE", "W2": "NYSE", "W1b": "LSE", "W2b": "NYSE", "T": None}
LOCAL_TZ = {"W1": LONDON, "W2": NEW_YORK, "W1b": LONDON, "W2b": NEW_YORK, "T": UTC}
KIND = {"W1": "fixed", "W2": "fixed", "W1b": "second_look", "W2b": "second_look", "T": "trigger"}
TAG_RE = r"W[12]b?|T(?:[01][0-9]|2[0-3])(?:00|30)"
CALL_ID_RE = re.compile(rf"([0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})_({TAG_RE})")


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
    """Window order (§16.18 A14, R-AN-4): model day, then entry_utc (W1 before W2, as before)."""
    w = window_of(cid)
    return (w.day, w.entry_utc)


def window_type(cid):
    """W1, W2, W1b, W2b, or T for every trigger window (R-AN-47)."""
    _d, tag = parse_call_id(cid)
    return "T" if tag.startswith("T") else tag


def tday(x):
    """The model day of an aware time: the UTC date of x + 2 h (R-AN-89)."""
    return (x.astimezone(UTC) + dt.timedelta(hours=2)).date()


def last_check(d):
    """L_d = model_to_utc((d+1) 00:00) − 270 min (R-AN-89)."""
    return model_to_utc(dt.datetime.combine(d + dt.timedelta(days=1), dt.time(0))) - dt.timedelta(minutes=270)


def check_grid(d):
    """G(d): the check times of model weekday d, 22:30 UTC of d − 1 day + k·30 min up to L_d; [] on a weekend."""
    if not is_weekday(d):
        return []
    out, t, last = [], dt.datetime(d.year, d.month, d.day, tzinfo=UTC) - dt.timedelta(minutes=90), last_check(d)
    while t <= last:
        out.append(t)
        t += HALF
    return out


def scheduled_checks(d):
    """{check_utc: tag} of W1, W2, W1b and W2b of model weekday d (check = entry − 30 min)."""
    if not is_weekday(d):
        return {}
    return {Window(d, t).check_utc: t for t in SCHEDULED}


def floor_half_hour(t):
    t = t.astimezone(UTC).replace(second=0, microsecond=0)
    return t.replace(minute=t.minute - t.minute % 30)


def trigger_tag(check):
    return "T" + check.astimezone(UTC).strftime("%H%M")


def trigger_check(d, tag):
    """The check time of trigger tag `T<hhmm>` of model day d (22:00-23:59 UTC: the day before)."""
    hh, mm = int(tag[1:3]), int(tag[3:5])
    base = d - dt.timedelta(days=1) if hh >= 22 else d
    return dt.datetime(base.year, base.month, base.day, hh, mm, tzinfo=UTC)


def valid_trigger(d, tag):
    """True when T<hhmm> is a check of G(d) that is not a scheduled check (R-AN-33)."""
    t = trigger_check(d, tag)
    return t in check_grid(d) and t not in scheduled_checks(d)


class Window:
    """One window of one model day. `tag` is the call_id's suffix (W1, W2, W1b, W2b or T<hhmm>); `name` is the
    window type that the call file and the context carry (W1, W2, W1b, W2b or T, R-AN-4); `kind` is fixed,
    second_look or trigger (R-AN-89)."""

    def __init__(self, day, tag):
        if tag in _SPEC:
            tz, e, x = _SPEC[tag]
            entry = dt.datetime(day.year, day.month, day.day, *e, tzinfo=tz).astimezone(UTC)
            exit_ = dt.datetime(day.year, day.month, day.day, *x, tzinfo=tz).astimezone(UTC)
            span = dt.timedelta(minutes=45)
        elif tag in SECOND_LOOKS:
            entry = Window(day, tag[:2]).entry_utc + dt.timedelta(minutes=120)
            exit_ = entry + dt.timedelta(minutes=240)
            span = HALF
        elif re.fullmatch(r"T(?:[01][0-9]|2[0-3])(?:00|30)", tag or ""):
            entry = trigger_check(day, tag) + HALF
            exit_ = entry + dt.timedelta(minutes=240)
            span = HALF
        else:
            raise ValueError(tag)
        self.day, self.tag = day, tag
        self.name = "T" if tag.startswith("T") else tag
        self.kind = KIND[self.name]
        self.call_id = call_id(day, tag)
        self.entry_utc, self.exit_utc = entry, exit_
        self.deadline_utc = self.entry_utc - dt.timedelta(minutes=15)
        self.span_start_utc = self.entry_utc - span
        self.check_utc = self.entry_utc - HALF
        self.exchange = EXCHANGE[self.name]
        self.local_tz = LOCAL_TZ[self.name]

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
        return (self.day, self.entry_utc)

    def __repr__(self):
        return f"Window({self.call_id})"


def windows_on(day, a14=False):
    """W1 and W2 of a weekday; with a14 (the day is >= MODE.json a14_from) also W1b and W2b, in window order."""
    if not is_weekday(day):
        return []
    return sorted((Window(day, w) for w in (SCHEDULED if a14 else WINDOWS)), key=lambda w: w.entry_utc)


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
