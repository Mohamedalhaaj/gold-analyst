"""A small GitHub REST client for the script-only workflows (GITHUB_TOKEN from the job's environment; the token is
never printed), and the complete read of the agent inbox's activity log (register R-AN-32, §16.18 A6). FROZEN with
the merge and settle code."""
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


class GitHubError(Exception):
    pass


class ActivityReadFailed(Exception):
    """The inbox's activity could not be read in full: the run must stop before it writes anything (R-AN-39)."""


class GitHubAPI:
    def __init__(self, repo, token=None, sleep=time.sleep):
        self.repo = repo
        self._token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        self.sleep = sleep

    def _req(self, method, url, body=None):
        if not url.startswith("http"):
            url = API + url
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if self._token:
            req.add_header("Authorization", f"Bearer {self._token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                link = r.headers.get("Link", "")
                return (json.loads(raw) if raw else None), link
        except urllib.error.HTTPError as e:
            raise GitHubError(f"{method} {url.split('?')[0]} -> {e.code}") from None

    def _pages(self, url):
        out = []
        while url:
            page, link = self._req("GET", url)
            out.extend(page or [])
            m = NEXT_RE.search(link or "")
            url = m.group(1) if m else None
        return out

    def run(self, run_id):
        return self._req("GET", f"/repos/{self.repo}/actions/runs/{run_id}")[0]

    def comments(self, issue):
        return self._pages(f"/repos/{self.repo}/issues/{issue}/comments?per_page=100")

    def post_comment(self, issue, body):
        return self._req("POST", f"/repos/{self.repo}/issues/{issue}/comments", {"body": body})[0]

    def dispatch(self, workflow_file, ref, inputs):
        self._req("POST", f"/repos/{self.repo}/actions/workflows/{workflow_file}/dispatches",
                  {"ref": ref, "inputs": inputs})


def urllib_http(method, url, token, timeout):
    """(status, Link header, body bytes) of one request; status 0 = no response (a connection error or the timeout).
    An HTTP error status is returned, not raised."""
    req = urllib.request.Request(url, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers.get("Link", "") or "", r.read()
    except urllib.error.HTTPError as e:
        return e.code, "", b""
    except (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError, OSError):
        return 0, "", b""


class _TokenRefused(Exception):
    pass


class InboxActivity:
    """The complete read of `GET /repos/{inbox}/activity?per_page=100&time_period=month` (register R-AN-32, after the
    review of §16.18 A6), the same in analyst-merge and analyst-settle:
    - each page request has a 30 s timeout; a failed try (no response, the timeout, a 429, a 5xx, or a body that is
      not a JSON list) is repeated, up to 3 tries per page, 20 s apart; any other status fails the read at once;
    - if GitHub answers the token with 401, 403 or 404 on any page, the whole read starts again from the first page
      without a token (the inbox is public), with the same tries, at most once per read;
    - the read is complete when every page answered 200 with a JSON list and the last page's Link header has no
      rel="next"; then it returns (entries, "token" | "anonymous"). Otherwise it raises ActivityReadFailed.
    A next link must stay on the API host (the token is never sent elsewhere); a link already followed, or more than
    MAX_PAGES pages, fails the read (a loop guard). The whole read, its restart included, has BUDGET_S from its first
    request (§16.18 A6 fourth review, N1): a request's timeout is the smaller of PAGE_TIMEOUT_S and the time left, and
    the read fails when no time is left for a request or a wait."""

    PAGE_TIMEOUT_S = 30
    TRIES = 3
    RETRY_S = 20
    MAX_PAGES = 1000
    BUDGET_S = 600

    def __init__(self, repo, token=None, http=urllib_http, sleep=time.sleep, mono=time.monotonic):
        self.repo = repo
        self._token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        self.http = http
        self.sleep = sleep
        self.mono = mono
        self._t0 = None

    def _left(self):
        return self.BUDGET_S - (self.mono() - self._t0)

    def read_activity(self):
        if not self.repo:
            raise ActivityReadFailed("no inbox repository")
        self._t0 = self.mono()
        first = f"{API}/repos/{self.repo}/activity?per_page=100&time_period=month"
        if self._token:
            try:
                return self._read(first, self._token), "token"
            except _TokenRefused:
                pass
        try:
            return self._read(first, None), "anonymous"
        except _TokenRefused:                       # cannot happen without a token; kept for clarity
            raise ActivityReadFailed("refused") from None

    def _read(self, url, token):
        out, seen = [], set()
        while url:
            if url in seen or len(seen) >= self.MAX_PAGES or not url.startswith(API + "/"):
                raise ActivityReadFailed("bad next link")
            seen.add(url)
            entries, link = self._page(url, token)
            out.extend(entries)
            m = NEXT_RE.search(link or "")
            url = m.group(1) if m else None
        return out

    def _page(self, url, token):
        for i in range(self.TRIES):
            t = min(self.PAGE_TIMEOUT_S, self._left())
            if t <= 0:
                raise ActivityReadFailed("read budget spent")
            status, link, body = self.http("GET", url, token, t)
            if token and status in (401, 403, 404):
                raise _TokenRefused()
            if status == 200:
                try:
                    doc = json.loads(body.decode("utf-8")) if body else None
                except (ValueError, UnicodeDecodeError):
                    doc = None
                if isinstance(doc, list):
                    return doc, link
            elif not (status == 0 or status == 429 or status >= 500):
                raise ActivityReadFailed(f"activity page -> {status}")
            if i < self.TRIES - 1:
                if self._left() <= self.RETRY_S:
                    raise ActivityReadFailed("read budget spent")
                self.sleep(self.RETRY_S)
        raise ActivityReadFailed("activity page failed after its tries")
