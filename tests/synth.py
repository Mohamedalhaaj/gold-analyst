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
              "analyst/calendars/fomc_2026_2027.csv", "analyst/calendars/macro_2026_2027.csv", "analyst/PARAMS.json", "analyst/PINS.json",
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


# ---------------------------------------------------------------- fake Twelve Data (register R-AN-87, §16.18 A9)
TD_SYMS = {"XAU/USD": "XAUUSD", "EUR/USD": "EURUSD", "USD/JPY": "USDJPY", "GBP/USD": "GBPUSD", "USD/CAD": "USDCAD",
           "USD/CHF": "USDCHF", "AUD/JPY": "AUDJPY"}


def td_body(values, status="ok", **extra):
    """A Twelve Data time_series body: values [(datetime str, o, h, l, c)] with float prices written to 5 dp."""
    doc = {"meta": {"symbol": "X", "interval": "1h", "type": "Physical Currency"},
           "values": [{"datetime": t, "open": f"{o:.5f}", "high": f"{h:.5f}", "low": f"{l:.5f}", "close": f"{c:.5f}"}
                      for (t, o, h, l, c) in values], "status": status}
    doc.update(extra)
    return json.dumps(doc).encode()


def td_error(code, message="You have run out of API credits for the current minute."):
    return json.dumps({"code": code, "message": message, "status": "error"}).encode()


def td_query(url):
    q = url.split("?", 1)[1]
    return dict(kv.split("=", 1) for kv in q.split("&"))


class FakeTD:
    """Serves Twelve Data time_series: 1h bars from start_date to end_date inclusive (so the hour past the last one
    the context needs is served too, and must be ignored), XAU only at trading hours (R-AN-15), FX at every weekday
    hour; `fresh` (interval 1min, outputsize 1) gives the minute bar starting at floor_minute(now() - fresh_lag_s -
    60 s). Records requests. `absent` = {(sym, hour)}; `responses` = {symbol | "fresh": (http, body) or a list of
    them, served in order and the last one repeated}; `delay_s` = seconds each request takes on `clk`."""

    def __init__(self, now=None, fn=price_path, absent=(), responses=None, fresh_lag_s=60, clk=None, delay_s=0.0):
        self.now, self.fn, self.absent = now, fn, set(absent)
        self.responses = {k: (list(v) if isinstance(v, list) else [v]) for k, v in (responses or {}).items()}
        self.fresh_lag_s, self.clk, self.delay_s = fresh_lag_s, clk, delay_s
        self.requests = []

    def bar(self, sym, hour):
        base = int(hour.timestamp())
        if sym == "XAUUSD":
            o, c = self.fn(base) / 1000.0, self.fn(base + 3599) / 1000.0
            return round(o, 5), round(max(o, c) + 0.5, 5), round(min(o, c) - 0.5, 5), round(c, 5)
        o = 1.1 + 0.001 * math.sin(base / 5000.0)
        c = 1.1 + 0.001 * math.sin((base + 3599) / 5000.0)
        if sym in ("USDJPY", "AUDJPY"):
            o, c = o * (100 if sym == "USDJPY" else 90), c * (100 if sym == "USDJPY" else 90)
        return round(o, 5), round(max(o, c) + 0.0001, 5), round(min(o, c) - 0.0001, 5), round(c, 5)

    def __call__(self, url, timeout):
        self.requests.append(url)
        if self.clk is not None and self.delay_s:
            self.clk.sleep(self.delay_s)
        q = td_query(url)
        key = "fresh" if q.get("interval") == "1min" else q.get("symbol")
        if key in self.responses:
            seq = self.responses[key]
            return seq.pop(0) if len(seq) > 1 else seq[0]
        if key == "fresh":
            t = (self.now() - dt.timedelta(seconds=self.fresh_lag_s + 60)).replace(second=0, microsecond=0)
            o = self.fn(int(t.timestamp())) / 1000.0
            return 200, td_body([(t.strftime("%Y-%m-%d %H:%M:%S"), o, o + 0.1, o - 0.1, o)])
        sym = TD_SYMS[q["symbol"]]
        a = dt.datetime.strptime(q["start_date"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
        b = dt.datetime.strptime(q["end_date"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
        out = []
        h = a
        while h <= b:
            m = clock.to_server(h)
            trading = (m.weekday() < 5 and m.hour >= 1) if sym == "XAUUSD" else h.weekday() < 5 or \
                (h.weekday() == 6 and h.hour >= 21)
            if trading and (sym, h) not in self.absent:
                out.append((h.strftime("%Y-%m-%d %H:%M:%S"),) + self.bar(sym, h))
            h += dt.timedelta(hours=1)
        return 200, td_body(out)


class FakeClock:
    def __init__(self, t0=0.0):
        self.t = t0

    def mono(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, s)


# ---------------------------------------------------------------- fake GitHub API
class FakeAPI:
    """gold-analyst's API (comments, runs, dispatches) and the inbox's activity log (register R-AN-32: the complete
    read). `read_fail` makes the next reads fail, as `ghapi.InboxActivity` does after its tries."""

    def __init__(self, now):
        self.now = now
        self.issue_comments = []
        self.acts = []
        self.dispatched = []
        self._id = 1000
        self.read_fail = 0
        self.read_how = "token"
        self.reads = 0

    def add_push(self, ref, before, after, t, actor="Mohamedalhaaj", typ="branch_creation", act_id=None):
        self._id += 1
        self.acts.insert(0, {"id": act_id if act_id is not None else self._id, "ref": ref, "before": before,
                             "after": after, "timestamp": rec.iso(t), "activity_type": typ,
                             "actor": {"login": actor}})

    def read_activity(self):
        from ga import ghapi
        self.reads += 1
        if self.read_fail:
            self.read_fail -= 1
            raise ghapi.ActivityReadFailed("page failed after its tries")
        return list(self.acts), self.read_how

    def run(self, run_id):
        return {"created_at": rec.iso(self.now())}

    def comments(self, issue):
        return list(self.issue_comments)

    def post_comment(self, issue, body):
        self._id += 1
        t = rec.iso(self.now())
        c = {"id": self._id, "created_at": t, "updated_at": t, "body": body,
             "user": {"login": "github-actions[bot]", "type": "Bot"}}
        self.issue_comments.append(c)
        return c

    def dispatch(self, wf, ref, inputs):
        self.dispatched.append((wf, ref, inputs))


def gh_response(status, body):
    """What `gh api -i` gives: (exit code, stdout with the status line and headers, stderr)."""
    out = f"HTTP/2.0 {status} X\r\nContent-Type: application/json; charset=utf-8\r\n\r\n".encode()
    out += json.dumps(body).encode()
    ok = 200 <= status < 300
    return (0 if ok else 1), out, (b"" if ok else f"gh: X (HTTP {status})\n".encode())


class FakeGH:
    """`gh api -i <args>` for the session probes (register R-AN-84) as GitHub and Anthropic's proxy answer a session
    attached to the inbox only: the inbox answers, other repositories get the proxy's scope refusal, writes on the
    inbox's protected refs are refused (422), the Actions setting needs admin rights (403), and no webhook exists.
    `routes[(method, path)] = (status, body)` overrides an answer; `absent` = no gh in the image."""

    SCOPE = "GitHub access to {r} is not enabled for this session"

    def __init__(self, inbox_repo, inbox_dir):
        self.inbox_repo, self.inbox_dir = inbox_repo, inbox_dir
        self.routes = {}
        self.calls = []
        self.absent = False
        self.seen_after_polls = 0
        self.polls = 0
        self.on_call = None

    def __call__(self, args, timeout):
        if self.absent:
            return None
        self.calls.append(list(args))
        if self.on_call:
            self.on_call(args)
        method, path, i = "GET", None, 0
        while i < len(args):
            if args[i] == "-X":
                method, i = args[i + 1], i + 2
                continue
            if args[i] in ("-f", "-F"):
                i += 2
                continue
            path, i = args[i], i + 1
        if (method, path) in self.routes:
            return gh_response(*self.routes[(method, path)])
        base = path.split("?", 1)[0]
        inbox = f"repos/{self.inbox_repo}"
        if method == "GET" and base == inbox:
            return gh_response(200, {"id": 1, "full_name": self.inbox_repo})
        if method == "GET" and base == f"{inbox}/hooks":
            return gh_response(200, [])
        if method == "GET" and base == f"{inbox}/activity":
            self.polls += 1
            ref = path.split("ref=", 1)[1].split("&", 1)[0]
            sha = git(self.inbox_dir, "rev-parse", "--verify", "-q", ref, check=False)
            listed = sha and self.polls > self.seen_after_polls
            return gh_response(200, [{"id": 1, "ref": ref, "after": sha}] if listed else [])
        if base.startswith(inbox + "/git/refs"):
            return gh_response(422, {"message": "Repository rule violations found"})
        if base == f"{inbox}/actions/permissions":
            return gh_response(403, {"message": "Resource not accessible by integration"})
        if base.startswith("repos/"):
            r = "/".join(base.split("/")[1:3])
            return gh_response(403, {"message": self.SCOPE.format(r=r)})
        return gh_response(404, {"message": "Not Found"})


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


INBOX_REPO = "Mohamedalhaaj/gold-analyst-inbox"

# The inbox's rulesets (register R-AN-75 item 2), as a pre-receive hook: main only through the deploy key (the
# analyst-inbox-mirror job; here GA_TEST_DEPLOY_KEY), no tags, no branch outside claude/, and claude/** append-only
# (no deletion, no non-fast-forward). GitHub's refusal text carries GH013.
INBOX_HOOK = """#!/bin/sh
z=0000000000000000000000000000000000000000
rule() { echo "error: GH013: Repository rule violations found for $1." >&2; exit 1; }
while read old new ref; do
  if [ -n "$GA_TEST_DEPLOY_KEY" ] && [ "$ref" = refs/heads/main ]; then continue; fi
  case "$ref" in
    refs/heads/claude/*)
      [ "$new" = "$z" ] && rule "$ref"
      if [ "$old" != "$z" ] && ! git merge-base --is-ancestor "$old" "$new"; then rule "$ref"; fi ;;
    *) rule "$ref" ;;
  esac
done
exit 0
"""


class World:
    """origin.git (bare: gold-analyst) + a seed clone that commits `files` to main. With `inbox`, also the agent inbox
    `Mohamedalhaaj/gold-analyst-inbox.git` (bare, with the inbox's rules), whose main the real analyst-inbox-mirror
    code fast-forwards to origin main (`mirror`); sessions clone the inbox."""

    def __init__(self, tmp, files, protect_main=False, inbox=False):
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
        self.inbox = None
        self.mirror_log = []
        if inbox:
            self.inbox = self.tmp / f"{INBOX_REPO}.git"
            self.inbox.parent.mkdir(parents=True, exist_ok=True)
            git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.inbox))
            hook = self.inbox / "hooks" / "pre-receive"
            hook.write_text(INBOX_HOOK)
            hook.chmod(0o755)
            git(self.inbox, "fetch", "-q", str(self.origin), "+refs/heads/main:refs/heads/main")   # as created

    def direct_url(self, repo):
        """D(x) of R-AN-84 in the tests: a local path that does not exist (no network)."""
        return str(self.tmp / "direct" / f"{repo}.git")

    def mirror(self):
        """analyst-inbox-mirror (register R-AN-83): fast-forward inbox main to origin main, with the real job code."""
        if self.inbox is None:
            return None
        import inbox_mirror
        d = self.clone("mirror")
        rc = inbox_mirror.mirror(d, str(self.inbox), env={"GA_TEST_DEPLOY_KEY": "1"}, log=self.mirror_log.append)
        return rc

    def deny_main(self, on=True):
        f = self.origin / "deny_main"
        if on:
            f.write_text("1")
        elif f.exists():
            f.unlink()

    def clone(self, name, src=None):
        d = self.tmp / name
        if d.exists():
            shutil.rmtree(d)
        git(self.tmp, "clone", "-q", str(src or self.origin), str(d))
        return d

    def clone_inbox(self, name):
        return self.clone(name, src=self.inbox or self.origin)

    def owner_commit(self, files, msg="owner", mirror=True):
        git(self.seed, "pull", "-q", "--rebase", "origin", "main")
        for p, b in files.items():
            fp = self.seed / p
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_bytes(b if isinstance(b, bytes) else b.encode())
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "-q", "-m", msg)
        git(self.seed, "push", "-q", "origin", "main")
        if mirror:
            self.mirror()                                  # an owner push to main triggers analyst-inbox-mirror
        return git(self.seed, "rev-parse", "HEAD")

    def branch_sha(self, branch):
        return git(self.inbox or self.origin, "rev-parse", f"refs/heads/{branch}")

    def main_sha(self):
        return git(self.origin, "rev-parse", "refs/heads/main")

    def inbox_main_sha(self):
        return git(self.inbox, "rev-parse", "refs/heads/main")

    def main_file(self, path):
        p = subprocess.run(["git", "-C", str(self.origin), "show", f"main:{path}"], capture_output=True)
        return p.stdout if p.returncode == 0 else None

    def main_list(self, prefix):
        out = git(self.origin, "ls-tree", "-r", "--name-only", "main")
        return sorted(x for x in out.splitlines() if x.startswith(prefix))
