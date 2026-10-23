"""External inputs of the analyst judge, each behind a small interface with a test double:

- GitHub REST API (activity, the witness issue's comments, workflow runs): `GitHubAPI` / `FakeGitHub`.
- Dukascopy hour files (re-downloads, R-AN-66): `HttpHourFetcher` / `FakeHourFetcher`, behind `HourSource`, which keeps
  the judge's own append-only cache `hours.csv` so a file is downloaded once and later runs reuse its summary.

The token (GITHUB_TOKEN of the judge job) is read from the environment by the caller and only ever placed in a request
header; it is never logged or written.
"""
from __future__ import annotations

import csv
import io
import json
import os
import time
import urllib.error
import urllib.request

from . import ticks
from .common import fmt_utc, sha256_bytes


class ApiError(RuntimeError):
    pass


# ---------------------------------------------------------------- GitHub


class GitHubAPI:
    def __init__(self, repo: str, token: str | None, base: str = "https://api.github.com", timeout: int = 20):
        self.repo = repo
        self.token = token
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _get(self, url: str):
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "X-GitHub-Api-Version": "2022-11-28",
                                                   "User-Agent": "gold-analyst-judge"})
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        last = None
        for i in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read().decode("utf-8")), r.headers.get("Link", "")
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None, ""
                last = f"http {e.code}"
                if e.code not in (429, 500, 502, 503, 504):
                    break
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = type(e).__name__
            time.sleep(5 * (i + 1))
        raise ApiError(last or "unreachable")

    def _paged(self, url: str):
        out = []
        while url:
            data, link = self._get(url)
            if data is None:
                break
            out.extend(data)
            url = None
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part[part.find("<") + 1:part.find(">")]
        return out

    def activity(self):
        return self._paged(f"{self.base}/repos/{self.repo}/activity?per_page=100")

    def issue_comments(self, issue: int):
        return self._paged(f"{self.base}/repos/{self.repo}/issues/{int(issue)}/comments?per_page=100")

    def run(self, run_id):
        data, _ = self._get(f"{self.base}/repos/{self.repo}/actions/runs/{int(run_id)}")
        return data


class FakeGitHub:
    """Test double: {"activity": [...], "comments": {"<issue>": [...]}, "runs": {"<id>": {...}}} in the API's shapes."""

    def __init__(self, data: dict | None = None, unreachable: bool = False):
        self.data = data if data is not None else {"activity": [], "comments": {}, "runs": {}}
        self.unreachable = unreachable

    def _guard(self):
        if self.unreachable:
            raise ApiError("unreachable (test double)")

    def activity(self):
        self._guard()
        return list(self.data.get("activity", []))

    def issue_comments(self, issue: int):
        self._guard()
        return list(self.data.get("comments", {}).get(str(issue), []))

    def run(self, run_id):
        self._guard()
        return self.data.get("runs", {}).get(str(run_id))


# ---------------------------------------------------------------- hour files


class HttpHourFetcher:
    UA = "Mozilla/5.0 (gold-analyst judge)"

    def __init__(self, pacing_s: float = 1.5, timeout: int = 20):
        self.pacing_s = pacing_s
        self.timeout = timeout
        self._last = 0.0

    def get(self, url: str):
        """(http, body); http 0 = connection error after the tries."""
        for i in range(3):
            wait = self._last + self.pacing_s - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": self.UA})
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    body = r.read()
                    self._last = time.monotonic()
                    return r.status, body
            except urllib.error.HTTPError as e:
                self._last = time.monotonic()
                if e.code in (429, 500, 502, 503, 504) and i < 2:
                    continue
                return e.code, b""
            except (urllib.error.URLError, TimeoutError, OSError):
                self._last = time.monotonic()
                if i < 2:
                    continue
                return 0, b""
        return 0, b""


class FakeHourFetcher:
    """Test double: {url: (http, body)}; an unknown url is a 404. `calls` counts requests."""

    def __init__(self, files: dict | None = None, down: bool = False):
        self.files = files if files is not None else {}
        self.down = down
        self.calls = 0

    def get(self, url: str):
        self.calls += 1
        if self.down:
            return 0, b""
        return self.files.get(url, (404, b""))


HOURS_HEADER = ["judge_utc", "sym", "hour_utc", "url", "http", "bytes", "sha256", "ticks", "status", "first_ms",
                "first_ask", "first_bid", "last_ms", "last_ask", "last_bid", "max_sum", "min_sum"]
_INTS = ("http", "bytes", "ticks", "first_ms", "first_ask", "first_bid", "last_ms", "last_ask", "last_bid", "max_sum",
         "min_sum")


class HourSource:
    """The judge's view of one hour file: its own download, cached in `hours.csv` (append-only).
    A file whose download fails (status `error`) is not cached and is retried by a later run."""

    def __init__(self, fetcher, cache_path: str | None, now_utc: str, max_fetch: int = 3000):
        self.fetcher = fetcher
        self.cache_path = cache_path
        self.now_utc = now_utc
        self.max_fetch = max_fetch
        self.fetched = 0
        self.cache = {}
        self.new_rows = []
        self.unavailable = 0
        if cache_path and os.path.exists(cache_path):
            with open(cache_path, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    row = {k: r[k] for k in HOURS_HEADER}
                    for k in _INTS:
                        row[k] = int(row[k]) if row[k] != "" else None
                    self.cache[(row["sym"], row["hour_utc"])] = row

    def get(self, sym: str, hour):
        """The judge's record for (sym, hour): dict with status/sha256/... or None when it could not be fetched."""
        key = (sym, fmt_utc(hour))
        if key in self.cache:
            return self.cache[key]
        if self.fetched >= self.max_fetch:
            self.unavailable += 1
            return None
        url = ticks.hour_url(sym, hour)
        self.fetched += 1
        http, body = self.fetcher.get(url)
        status, summ = ticks.classify(http, body)
        if status == "error":
            self.unavailable += 1
            return None
        row = {"judge_utc": self.now_utc, "sym": sym, "hour_utc": key[1], "url": url, "http": http,
               "bytes": len(body or b""), "sha256": sha256_bytes(body) if body else "", "ticks": None,
               "status": status}
        for k in ("first_ms", "first_ask", "first_bid", "last_ms", "last_ask", "last_bid", "max_sum", "min_sum"):
            row[k] = None
        if summ:
            row.update(summ)
        self.cache[key] = row
        self.new_rows.append(row)
        return row

    def flush(self):
        if not self.cache_path or not self.new_rows:
            return
        new = not os.path.exists(self.cache_path)
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        if new:
            w.writerow(HOURS_HEADER)
        for r in self.new_rows:
            w.writerow(["" if r[k] is None else r[k] for k in HOURS_HEADER])
        with open(self.cache_path, "a", encoding="utf-8", newline="") as f:
            f.write(buf.getvalue())
        self.new_rows = []
