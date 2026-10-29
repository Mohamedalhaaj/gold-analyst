"""Python reference for the watcher's rule (register R-AN-89, R-AN-91), written apart from worker.js.

It follows wake_freq.py (the measurement behind §16.18 A14) day by day, with the A14 rule as recorded: checks G(d) to
L_d, scheduled checks from the DST table (zoneinfo), R20 over 20 prior model weekdays, the open-trade rule exit >
T + 30 min (A14-D4), wakes = windows with an attempt (A14-D5), every condition (move too) consumed once a day, the
holiday rule (A14-D7). legacy_wakes() is wake_freq.simulate itself (p_enter = 0) in plain Python, so that
wake_freq_replay.py can show it equals wake_freq.simulate on the same bars.

§16.18 A15 (R-AN-89, R-AN-91): from a15_from on, R(T) has no step 1: open trades never gate a check, so a window
during an open trade is fired like any other (it becomes a review in its session).

Standard library only.

usage:
  python3 ref_wakes.py expected <bars.json> <first_day> <last_day> [<a15_from>]
                                                                     -> expected watcher decisions and wakes (stdout)
  python3 ref_wakes.py synthetic <seed>                               -> the synthetic bar fixture (stdout)
"""
import datetime as dt
import json
import math
import random
import statistics
import sys
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc
LONDON = ZoneInfo("Europe/London")
NEW_YORK = ZoneInfo("America/New_York")
M = dt.timedelta(minutes=1)
CONDS = ("asia_up", "asia_dn", "y_high", "y_low", "move")
HOLIDAYS = {  # analyst/tables/holidays.csv (R-AN-9): date -> exchanges
    "2026-11-26": {"NYSE"}, "2026-11-27": {"NYSE"}, "2026-12-24": {"NYSE", "LSE"}, "2026-12-25": {"NYSE", "LSE"},
    "2026-12-28": {"LSE"}, "2026-12-31": {"LSE"}, "2027-01-01": {"NYSE", "LSE"}, "2027-01-18": {"NYSE"},
    "2027-02-15": {"NYSE"}, "2027-03-26": {"NYSE", "LSE"}, "2027-03-29": {"LSE"}, "2027-05-03": {"LSE"},
    "2027-05-31": {"NYSE", "LSE"}, "2027-06-18": {"NYSE"}, "2027-07-05": {"NYSE"}, "2027-08-30": {"LSE"},
    "2027-09-06": {"NYSE"}, "2027-11-25": {"NYSE"}, "2027-11-26": {"NYSE"}, "2027-12-24": {"NYSE", "LSE"},
    "2027-12-27": {"LSE"}, "2027-12-28": {"LSE"}, "2027-12-31": {"LSE"},
}


def z(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_z(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def tday(t):
    return (t + dt.timedelta(hours=2)).date()


def local(d, hh, mm, tz):
    return dt.datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz).astimezone(UTC)


def windows(d):
    """name -> (kind, exchange, check, entry, exit)."""
    w1, w2 = local(d, 8, 0, LONDON), local(d, 9, 30, NEW_YORK)
    out = {}
    for name, entry, kind, ex in (("W1", w1, "fixed", "LSE"), ("W1b", w1 + 120 * M, "second_look", "LSE"),
                                  ("W2", w2, "fixed", "NYSE"), ("W2b", w2 + 120 * M, "second_look", "NYSE")):
        out[name] = (kind, ex, entry - 30 * M, entry, entry + 240 * M)
    return out


def last_check(d):
    return local(d, 17, 0, NEW_YORK) - 270 * M  # model_to_utc((d+1) 00:00) - 270 min


def grid(d):
    t = dt.datetime(d.year, d.month, d.day, tzinfo=UTC) - 90 * M
    out = []
    while t <= last_check(d):
        out.append(t)
        t += 30 * M
    return out


def trades(call_id):
    """The fixture's outcome of a woken window: a buy or a sell for about 2 in 5 call_ids."""
    h = 0
    for ch in call_id:
        h = (h * 31 + ord(ch)) % 1000003
    return h % 5 < 2


def conditions(T, d, bars):
    """bars: [(t, o, h, l, c)] as returned by the request (t >= A); only t <= T - 30 min are used."""
    used = [b for b in bars if b[0] <= T - 30 * M]
    by_day = {}
    for b in used:
        by_day.setdefault(tday(b[0]), []).append(b)
    none = dict.fromkeys(CONDS, False)
    prior = sorted(e for e in by_day if e < d and e.weekday() < 5)
    if len(prior) < 20:
        return none
    ranges = [max(b[2] for b in by_day[e]) - min(b[3] for b in by_day[e]) for e in prior[-20:]]
    r20 = statistics.median(ranges)
    yb = by_day[prior[-1]]
    yh, yl = max(b[2] for b in yb), min(b[3] for b in yb)
    today = sorted(by_day.get(d, []))
    bar = [b for b in today if b[0] == T - 30 * M]
    if not bar:
        return none
    _, o, h, l, c = bar[0]
    a_end = dt.datetime(d.year, d.month, d.day, 7, tzinfo=UTC)
    asia = [b for b in today if b[0] < a_end]
    before = [b for b in today if b[0] <= T - 90 * M]
    after = T >= a_end
    out = {
        "asia_up": after and bool(asia) and c > max(b[2] for b in asia),
        "asia_dn": after and bool(asia) and c < min(b[3] for b in asia),
        "move": bool(before) and abs(c - before[-1][4]) >= 0.3 * r20,
    }
    out["y_high"] = h >= yh - 0.1 * r20
    out["y_low"] = l <= yl + 0.1 * r20
    return out


def replay(bars, days, outcome=None, a15_from=None):
    """The watcher's decision at every check of each day and the wakes, with every woken window attempted (unless
    `start` refuses it as a holiday of its own exchange) and its outcome from `outcome` (default trades()). For days
    >= a15_from (a date or None) open trades are not formed (A15)."""
    outcome = outcome or trades
    out = {}
    for d in days:
        if d.weekday() >= 5:
            continue
        ds = d.isoformat()
        sched = windows(d)
        a = dt.datetime(d.year, d.month, d.day, 22, tzinfo=UTC) - dt.timedelta(days=33)
        req = [b for b in bars if b[0] >= a]
        woken = []  # (check, kind, reason, exit, called, call_id)
        checks = []
        fires = 0
        for T in grid(d):
            E = [w for w in woken if w[0] < T]
            O = [] if (a15_from and d >= a15_from) else [w for w in E if w[4] and w[3] > T + 30 * M]
            win = None
            if O:
                dec = "trade_open"
            elif len(E) >= 6:
                dec = "day_cap"
            else:
                hit = [n for n, v in sched.items() if v[2] == T]
                if hit:
                    n = hit[0]
                    kind, ex, check, entry, exit_ = sched[n]
                    dec = "fixed" if kind == "fixed" else n
                    if ex not in HOLIDAYS.get(ds, set()):
                        win = (check, kind, None, exit_, f"{ds}_{n}")
                elif ds in HOLIDAYS:
                    dec = "none"
                else:
                    g = (T - max(w[0] for w in E)) / M if E else math.inf
                    dec = "none"
                    if g >= 60:
                        cond = conditions(T, d, req)
                        used = {w[2] for w in E if w[1] == "trigger"}
                        for c in CONDS:
                            if cond[c] and c not in used and (c != "move" or g >= 120):
                                dec = f"trigger:{c}"
                                win = (T, "trigger", c, T + 270 * M, f"{ds}_T{T:%H%M}")
                                break
            if dec != "fixed" and dec not in ("trade_open", "day_cap", "none"):
                if fires >= 6:
                    dec, win = "fire_cap", None
                else:
                    fires += 1
            if win:
                check, kind, reason, exit_, cid = win
                woken.append((check, kind, reason, exit_, outcome(cid), cid))
            checks.append([z(T), dec])
        out[ds] = {"checks": checks, "wakes": [[w[5], w[1], w[2], w[4]] for w in woken]}
    return out


def legacy_wakes(bars, sched, check_to, cap=6, gap_min=60):
    """wake_freq.simulate with p_enter = 0, line for line in plain Python: [(tday, check, why)]."""
    by_day = {}
    for b in bars:
        by_day.setdefault(tday(b[0]), []).append(b)
    days = sorted(by_day)
    rng = {e: max(b[2] for b in by_day[e]) - min(b[3] for b in by_day[e]) for e in days}
    rows = []
    for i, td in enumerate(days):
        win = [rng[e] for e in days[max(0, i - 20):i]]
        if td.weekday() >= 5 or len(win) < 10:
            continue
        r20 = statistics.median(win)
        prev = days[i - 1]
        yh, yl = max(b[2] for b in by_day[prev]), min(b[3] for b in by_day[prev])
        day = sorted(by_day[td])
        t0 = dt.datetime(td.year, td.month, td.day, tzinfo=UTC)
        asia = [b for b in day if b[0] < t0 + dt.timedelta(hours=7)]
        ah = max((b[2] for b in asia), default=math.nan)
        al = min((b[3] for b in asia), default=math.nan)
        fired, n_dec, last = set(), 0, None
        for ts, o, h, l, c in day:
            t = ts + 30 * M
            hm = t.strftime("%H:%M")
            if n_dec >= cap:
                break
            if hm > check_to and t.hour < 22:
                continue
            why = None
            for k, v in sched.items():
                if hm == v and k not in fired:
                    why = k
            if why is None and (last is None or t - last >= gap_min * M):
                after = t >= t0 + dt.timedelta(hours=7)
                if after and "asia_up" not in fired and c > ah:
                    why = "asia_up"
                elif after and "asia_dn" not in fired and c < al:
                    why = "asia_dn"
                elif "y_high" not in fired and abs(h - yh) <= 0.1 * r20 or ("y_high" not in fired and h >= yh):
                    why = "y_high"
                elif "y_low" not in fired and (abs(l - yl) <= 0.1 * r20 or l <= yl):
                    why = "y_low"
                else:
                    prev2 = [b for b in day if b[0] <= ts - dt.timedelta(hours=1)]
                    if prev2 and abs(c - prev2[-1][4]) >= 0.3 * r20 and (last is None or t - last >= 120 * M):
                        why = "big_move"
            if why is None:
                continue
            fired.add(why)
            n_dec += 1
            last = t
            rows.append((td.isoformat(), z(t), why))
    return rows


def load_bars(path):
    doc = json.load(open(path))
    return [(parse_z(r[0]), r[1], r[2], r[3], r[4]) for r in doc["bars"]]


def synthetic(seed):
    """Seeded 30-minute bars, 2026-09-27 22:00Z (a Sunday open) to 2026-11-27, through the US clock change of
    2026-11-01, with the daily break, Thanksgiving's short sessions and some bars left out."""
    rnd = random.Random(seed)
    rows, px = [], 4000.0
    d = dt.date(2026, 9, 28)
    while d <= dt.date(2026, 11, 27):
        if d.weekday() < 5:
            t = local(d - dt.timedelta(days=1), 18, 0, NEW_YORK)  # the model day opens at 18:00 New York
            end = local(d, 17, 0, NEW_YORK)
            if d.isoformat() in ("2026-11-26", "2026-11-27"):
                end = dt.datetime(d.year, d.month, d.day, 18, tzinfo=UTC)
            px *= math.exp(rnd.gauss(0, 0.004))  # the gap over the break
            while t < end:
                r = rnd.gauss(0, 0.0013) + (rnd.gauss(0, 0.006) if rnd.random() < 0.01 else 0.0)
                o = round(px, 3)
                c = round(o * math.exp(r), 3)
                h = round(max(o, c) + abs(rnd.gauss(0, 0.0006)) * o, 3)
                l = round(min(o, c) - abs(rnd.gauss(0, 0.0006)) * o, 3)
                h, l = max(h, o, c), min(l, o, c)
                drop = rnd.random() < 0.02
                drop |= z(t) in ("2026-10-28T09:00:00Z", "2026-10-28T09:30:00Z", "2026-10-28T10:00:00Z",
                                 "2026-10-28T10:30:00Z", "2026-11-03T09:00:00Z")
                if not drop:
                    rows.append([z(t), o, h, l, c])
                px = c
                t += 30 * M
        d += dt.timedelta(days=1)
    return {"source": f"synthetic, ref_wakes.py synthetic {seed}", "bars": rows}


def main(argv):
    if argv[0] == "synthetic":
        json.dump(synthetic(int(argv[1])), sys.stdout, separators=(",", ":"))
        return
    if argv[0] == "expected":
        bars = load_bars(argv[1])
        a, b = dt.date.fromisoformat(argv[2]), dt.date.fromisoformat(argv[3])
        days = [a + dt.timedelta(days=i) for i in range((b - a).days + 1)]
        a15 = dt.date.fromisoformat(argv[4]) if len(argv) > 4 else None
        doc = {"fixture": argv[1].rsplit("/", 1)[-1], "a14_from": argv[2]}
        if a15:
            doc["a15_from"] = argv[4]
        doc["days"] = replay(bars, days, a15_from=a15)
        json.dump(doc, sys.stdout, separators=(",", ":"))
        return
    raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
