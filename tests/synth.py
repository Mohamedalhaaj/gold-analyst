"""Synthetic inputs for the tests: Dukascopy day and hour files, the mirrored store, forecasts, fake HTTP, a fake
GitHub API, and a local bare-repo world for analyst-merge."""
import csv
import datetime as dt
import hashlib
import io
import json
import lzma
import math
import shutil
import struct
import subprocess
from pathlib import Path

from ga import clock, hours, rec, store

UTC = rec.UTC
ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- Dukascopy files
def day_bi5(records):
    """records: [(sec, open, close, low, high, vol)] int points -> LZMA-alone bytes."""
    data = b"".join(struct.pack(">iiiiif", *r) for r in records)
    return lzma.compress(data, format=lzma.FORMAT_ALONE)


def tick_bi5(ticks):
    """ticks: [(ms, ask, bid, askv, bidv)] -> LZMA-alone bytes."""
    data = b"".join(struct.pack(">IIIff", *t) for t in ticks)
    return lzma.compress(data, format=lzma.FORMAT_ALONE)


def price_path(t_epoch, base=4000000, amp=3000):
    """A deterministic smooth price in XAU points (base 4000.000 $)."""
    return int(base + amp * math.sin(t_epoch / 7000.0) + 800 * math.sin(t_epoch / 900.0))


def day_records(day, side, fn=price_path, spread=200, vol=1.0, skip=()):
    base = int(dt.datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())
    out = []
    for m in range(1440):
        if m in skip:
            continue
        t = base + 60 * m
        mid = fn(t)
        o = mid + (spread // 2 if side == "ASK" else -spread // 2)
        c = fn(t + 59) + (spread // 2 if side == "ASK" else -spread // 2)
        hi, lo = max(o, c) + 50, min(o, c) - 50
        out.append((60 * m, o, c, lo, hi, vol))
    return out


_DAY_CACHE = {}


def _cached_day(d, side, fn):
    key = (d, side, fn)
    if key not in _DAY_CACHE:
        recs = day_records(d, side, fn)
        _DAY_CACHE[key] = (recs, day_bi5(recs))
    return _DAY_CACHE[key]


def store_files(days, fetched_utc=None, fn=price_path, missing=(), overrides=None):
    """{path: bytes} for mirror/system/days.csv and raw files of the given UTC days (dates)."""
    files = {}
    rows = []
    for d in days:
        for side in store.SIDES:
            f = fetched_utc or rec.iso(dt.datetime(d.year, d.month, d.day, tzinfo=UTC) + dt.timedelta(hours=27))
            if (d, side) in missing:
                rows.append([d.isoformat(), side, "missing", "", "", "", "", f])
                continue
            if (d, side) in (overrides or {}):
                recs = overrides[(d, side)]
                body = day_bi5(recs)
            else:
                recs, body = _cached_day(d, side, fn)
            files[store.raw_path(d, side)] = body
            rows.append([d.isoformat(), side, "ok", str(len(body)), hashlib.sha256(body).hexdigest(),
                         str(len(recs)), str(sum(1 for r in recs if r[5] > 0)), f])
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["day", "side", "status", "bytes", "sha256", "records", "vol_minutes", "fetched_utc"])
    w.writerows(rows)
    files[store.DAYS_PATH] = out.getvalue().encode()
    return files


SLOT_HEADER = "day,slot,origin_model,origin_utc,F_H1,F_S,price,y_bp,nfp,cpi,fomc,hol,computed_utc"


def slots_text(rows):
    """rows: [(day, slot, F_S, flags(nfp,cpi,fomc,hol), computed_utc)]."""
    lines = [SLOT_HEADER]
    for day, slot, fs, flags, comp in rows:
        lines.append(f"{day},{slot},{day} {slot + 1:02d}:00,{day}T00:00Z,12.5,{fs},4000.0,,"
                     f"{flags[0]},{flags[1]},{flags[2]},{flags[3]},{comp}")
    return "\n".join(lines) + "\n"


def default_slots(until_day, slots=(9, 10, 15), n_days=40, fs=lambda d, s: 60.0 + (d.day % 7) + s / 10):
    rows = []
    d = until_day
    k = 0
    while k < n_days:
        d -= dt.timedelta(days=1)
        if d.weekday() < 5:
            for s in slots:
                rows.append((d.isoformat(), s, repr(fs(d, s)), (0, 0, 0, 0),
                             rec.iso(dt.datetime(d.year, d.month, d.day, tzinfo=UTC) + dt.timedelta(hours=30))))
            k += 1
    rows.sort()
    return slots_text(rows)


def repo_files(extra=None):
    """The frozen tables, calendars, prompts and configs from the working tree, as a dict."""
    files = {}
    for p in ["analyst/tables/holidays.csv", "analyst/calendars/bls_2026_2027.csv",
              "analyst/calendars/fomc_2026_2027.csv", "analyst/PARAMS.json", "analyst/PINS.json",
              "analyst/playbooks/v0.md", "analyst/ROUTINE_PROMPT.md", "analyst/prompts/system.md",
              "analyst/prompts/lesson.md", "analyst/prompts/revision.md", "analyst/schema/plan.schema.json"]:
        files[p] = (ROOT / p).read_bytes()
    mf = ROOT / "analyst/FROZEN_ANALYST.sha256"
    files["analyst/FROZEN_ANALYST.sha256"] = mf.read_bytes() if mf.exists() else b""
    files.update(extra or {})
    return files


def mode_json(mode="canary", canary_from="2026-10-19", canary_to=None, live_from=None):
    return json.dumps({"mode": mode, "canary_from": canary_from, "canary_to": canary_to, "live_from": live_from,
                       "set_utc": "2026-10-01T00:00:00Z", "note": "test"}).encode()


# ---------------------------------------------------------------- fake HTTP for hour files
class FakeFeed:
    """Serves hour files: url -> (status, body). Unknown urls -> 404. Records requests."""

    def __init__(self, fn=price_path, absent=(), statuses=None, ticks_per_hour=120):
        self.fn, self.absent, self.statuses = fn, set(absent), dict(statuses or {})
        self.n = ticks_per_hour
        self.requests = []

    def ticks(self, sym, hour):
        base = int(hour.timestamp())
        out = []
        pts = hours.POINTS[sym]
        for i in range(self.n):
            ms = int(i * 3_600_000 / self.n)
            if sym == "XAUUSD":
                mid = self.fn(base + ms // 1000)
                out.append((ms, mid + 100, mid - 100, 1.0, 1.0))
            else:
                mid = int(pts * (1.1 + 0.001 * math.sin((base + ms // 1000) / 5000.0)))
                out.append((ms, mid + 2, mid - 2, 1.0, 1.0))
        return out

    def __call__(self, url, timeout):
        self.requests.append(url)
        if url in self.statuses:
            st = self.statuses[url]
            return st if isinstance(st, tuple) else (st, b"")
        for sym in hours.POINTS:
            if f"/{sym}/" in url:
                parts = url.split("/")
                y, m0, d, hh = int(parts[-4]), int(parts[-3]), int(parts[-2]), int(parts[-1][:2])
                hour = dt.datetime(y, m0 + 1, d, hh, tzinfo=UTC)
                if (sym, hour) in self.absent:
                    return 404, b""
                return 200, tick_bi5(self.ticks(sym, hour))
        return 404, b""


class FakeClock:
    def __init__(self, t0=0.0):
        self.t = t0

    def mono(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, s)


# ---------------------------------------------------------------- fake GitHub API
class FakeAPI:
    def __init__(self, now):
        self.now = now
        self.issue_comments = []
        self.acts = []
        self.dispatched = []
        self._id = 1000

    def add_push(self, ref, before, after, t, actor="claude[bot]", typ="branch_creation"):
        self._id += 1
        self.acts.insert(0, {"id": self._id, "ref": ref, "before": before, "after": after,
                             "timestamp": rec.iso(t), "activity_type": typ, "actor": {"login": actor}})

    def activity(self, ref=None, time_period=None):
        return [a for a in self.acts if ref is None or a["ref"] == ref]

    def run(self, run_id):
        return {"created_at": rec.iso(self.now())}

    def comments(self, issue):
        return list(self.issue_comments)

    def post_comment(self, issue, body):
        self._id += 1
        c = {"id": self._id, "created_at": rec.iso(self.now()), "body": body,
             "user": {"login": "github-actions[bot]", "type": "Bot"}}
        self.issue_comments.append(c)
        return c

    def dispatch(self, wf, ref, inputs):
        self.dispatched.append((wf, ref, inputs))


# ---------------------------------------------------------------- local bare-repo world
def git(cwd, *args, env=None, check=True):
    import os
    e = dict(os.environ)
    e.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
              "GIT_COMMITTER_EMAIL": "t@t"})
    if env:
        e.update(env)
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, env=e)
    if check and p.returncode != 0:
        raise RuntimeError(p.stderr.decode())
    return p.stdout.decode().strip()


class World:
    """origin.git (bare) + a seed clone that commits `files` to main + a session clone + a merge workspace clone."""

    def __init__(self, tmp, files, protect_main=False):
        self.tmp = Path(tmp)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.origin = self.tmp / "origin.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        seed = self.tmp / "seed"
        git(self.tmp, "clone", "-q", str(self.origin), str(seed))
        for p, b in files.items():
            fp = seed / p
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_bytes(b if isinstance(b, bytes) else b.encode())
        git(seed, "checkout", "-q", "-b", "main")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "seed")
        git(seed, "push", "-q", "origin", "main")
        self.seed = seed
        if protect_main:
            hook = self.origin / "hooks" / "pre-receive"
            hook.write_text("#!/bin/sh\nwhile read old new ref; do\n  if [ \"$ref\" = refs/heads/main ] && "
                            "[ -f \"$GIT_DIR/deny_main\" ]; then echo 'main is protected' >&2; exit 1; fi\n"
                            "done\nexit 0\n")
            hook.chmod(0o755)

    def deny_main(self, on=True):
        f = self.origin / "deny_main"
        if on:
            f.write_text("1")
        elif f.exists():
            f.unlink()

    def clone(self, name):
        d = self.tmp / name
        if d.exists():
            shutil.rmtree(d)
        git(self.tmp, "clone", "-q", str(self.origin), str(d))
        return d

    def owner_commit(self, files, msg="owner"):
        git(self.seed, "pull", "-q", "--rebase", "origin", "main")
        for p, b in files.items():
            fp = self.seed / p
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_bytes(b if isinstance(b, bytes) else b.encode())
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "-q", "-m", msg)
        git(self.seed, "push", "-q", "origin", "main")
        return git(self.seed, "rev-parse", "HEAD")

    def branch_sha(self, branch):
        return git(self.origin, "rev-parse", f"refs/heads/{branch}")

    def main_sha(self):
        return git(self.origin, "rev-parse", "refs/heads/main")

    def main_file(self, path):
        p = subprocess.run(["git", "-C", str(self.origin), "show", f"main:{path}"], capture_output=True)
        return p.stdout if p.returncode == 0 else None

    def main_list(self, prefix):
        out = git(self.origin, "ls-tree", "-r", "--name-only", "main")
        return sorted(x for x in out.splitlines() if x.startswith(prefix))
