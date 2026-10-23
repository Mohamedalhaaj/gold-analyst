"""analyst-canary-poll (prereg R5 C8; register R-AN-72). Canary only; display/measurement, not frozen.

For each UTC hour h 00..14 of today and each of the 6 symbols, poll the hour file from h + 1 h every 2 min until the
first HTTP 200 with a non-empty body, or until h + 3 h. A job covers the targets whose polling window overlaps its own
run (`--until` = the job's end). Rows go to `analyst/canary/hours/<day>_<HH>.csv` (create-only per job; HH = the job's
start hour) with the register's columns; the re-check of yesterday's first bodies at +24 h goes to
`analyst/canary/hours/<yesterday>_recheck.csv` (interpretation: create-only files instead of filling earlier rows).
"""
import argparse
import csv
import datetime as dt
import hashlib
import io
import sys
import time

from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parent))
import _boot  # noqa: E402,F401

import ga  # noqa: F401
from ga import clock, hours, rec

COLS = ["symbol", "hour_utc", "first_ok_utc", "lag_s", "polls", "first_404_utc", "first_empty_utc", "bytes",
        "sha256", "recheck_utc", "recheck_sha256", "same"]
SYMS = ("XAUUSD",) + hours.FX_PAIRS
POLL_S = 120


def poll(day, start, until, get=hours.urllib_get, now=None, sleep=time.sleep):
    now = now or (lambda: dt.datetime.now(rec.UTC).replace(microsecond=0))
    targets = {}
    for hh in range(15):
        h = dt.datetime(day.year, day.month, day.day, hh, tzinfo=rec.UTC)
        if h + clock.HOUR < until and h + dt.timedelta(hours=3) > start:
            for s in SYMS:
                targets[(s, h)] = {"symbol": s, "hour_utc": rec.iso(h), "first_ok_utc": "", "lag_s": "", "polls": 0,
                                   "first_404_utc": "", "first_empty_utc": "", "bytes": "", "sha256": "",
                                   "recheck_utc": "", "recheck_sha256": "", "same": ""}
    open_ = set(targets)
    while open_ and now() < until:
        for key in sorted(open_, key=lambda k: (k[1], k[0])):
            s, h = key
            t = now()
            if t < h + clock.HOUR:
                continue
            if t >= h + dt.timedelta(hours=3):
                open_.discard(key)
                continue
            row = targets[key]
            code, body = get(hours.url(s, h), hours.TIMEOUT_S)
            row["polls"] += 1
            sleep(hours.PACE_S)
            if code == 404 and not row["first_404_utc"]:
                row["first_404_utc"] = rec.iso(t)
            elif code == 200 and not body and not row["first_empty_utc"]:
                row["first_empty_utc"] = rec.iso(t)
            elif code == 200 and body:
                row.update(first_ok_utc=rec.iso(t), lag_s=int((t - (h + clock.HOUR)).total_seconds()),
                           bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
                open_.discard(key)
        if open_:
            sleep(POLL_S)
    return [targets[k] for k in sorted(targets, key=lambda k: (k[1], SYMS.index(k[0])))]


def recheck(rows, get=hours.urllib_get, now=None, sleep=time.sleep):
    now = now or (lambda: dt.datetime.now(rec.UTC).replace(microsecond=0))
    out = []
    for r in rows:
        if not r.get("sha256"):
            continue
        h = rec.parse_iso(r["hour_utc"])
        code, body = get(hours.url(r["symbol"], h), hours.TIMEOUT_S)
        sleep(hours.PACE_S)
        if code != 200:
            continue
        sha = hashlib.sha256(body or b"").hexdigest()
        r2 = dict(r)
        r2.update(recheck_utc=rec.iso(now()), recheck_sha256=sha, same=int(sha == r["sha256"]))
        out.append(r2)
    return out


def to_csv(rows):
    f = io.StringIO()
    w = csv.DictWriter(f, fieldnames=COLS, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return f.getvalue()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=5.75, help="how long this job polls")
    a = ap.parse_args(argv)
    start = dt.datetime.now(rec.UTC).replace(microsecond=0)
    until = start + dt.timedelta(hours=a.hours)
    day = start.date()
    base = _boot.ROOT / "analyst" / "canary" / "hours"
    base.mkdir(parents=True, exist_ok=True)
    rows = poll(day, start, until)
    p = base / f"{day.isoformat()}_{start.hour:02d}.csv"
    if rows and not p.exists():
        p.write_text(to_csv(rows), "utf-8")
    y = day - dt.timedelta(days=1)
    rp = base / f"{y.isoformat()}_recheck.csv"
    if start.hour >= 6 and not rp.exists():
        prev = []
        for f in sorted(base.glob(f"{y.isoformat()}_[0-9][0-9].csv")):
            prev.extend(csv.DictReader(io.StringIO(f.read_text("utf-8"))))
        rc = recheck(prev)
        if rc:
            rp.write_text(to_csv(rc), "utf-8")
    print(f"canary poll {day} rows {len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
