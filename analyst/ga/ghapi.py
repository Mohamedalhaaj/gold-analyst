"""A small GitHub REST client for the script-only workflows (GITHUB_TOKEN from the job's environment; the token is
never printed). FROZEN with the merge and settle code."""
import json
import os
import re
import time
import urllib.error
import urllib.request

API = "https://api.github.com"


class GitHubError(Exception):
    pass


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
            m = re.search(r'<([^>]+)>;\s*rel="next"', link or "")
            url = m.group(1) if m else None
        return out

    def activity(self, ref=None):
        q = f"/repos/{self.repo}/activity?per_page=100"
        if ref:
            q += "&ref=" + urllib.request.quote(ref, safe="")
        return self._pages(q)

    def run(self, run_id):
        return self._req("GET", f"/repos/{self.repo}/actions/runs/{run_id}")[0]

    def comments(self, issue):
        return self._pages(f"/repos/{self.repo}/issues/{issue}/comments?per_page=100")

    def post_comment(self, issue, body):
        return self._req("POST", f"/repos/{self.repo}/issues/{issue}/comments", {"body": body})[0]

    def dispatch(self, workflow_file, ref, inputs):
        self._req("POST", f"/repos/{self.repo}/actions/workflows/{workflow_file}/dispatches",
                  {"ref": ref, "inputs": inputs})
