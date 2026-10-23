"""The session probes and their record `analyst-probe/1` (register R-AN-84; prereg v0.6 R5 C5-C7). FROZEN.

From inside a routine session they test what the session can reach (§16.18 A6): the network allowlist (`net.*`), the
GitHub proxy's repository scope (`rest.*`, `git.ls_remote_*`), the inbox's rules (a push to main, a force push, a
tag, a branch outside claude/, a branch deletion, and the same writes through REST), gold-analyst out of reach
(`ga_push*`), the inbox's Actions (`workflow_push`, judged later from the inbox's runs) and no admin credential
(`admin_probe`). They run in a canary `context` (tag = the call_id; the whole set bounded at 120 s) and in
`run_window.py probe` (tag = p<UTC start>, no window).

Outcomes: an HTTP probe gives its status (0 = no response, a connection error or the 8 s timeout); a git probe
(30 s per command) gives `accepted`, `refused_rule` (GitHub's ruleset code GH013 in the output), `refused` (any other
refusal, the timeout included) or `error` (it could not be set up); a REST probe (`gh api`, 20 s) gives `ok`, `scope`
(the proxy's "GitHub access to ... is not enabled for this session"), `denied`, or `none` (no response, a 5xx, or no
gh); `rest.hooks` gives the length of the hook list instead of `ok`; `activity_seen_s` the seconds from a push to the
first activity answer listing it, or -1. The record keeps only integers, enums and booleans; no remote output text
is kept or printed. Interpretations (logged for the register): a probe not run in time has outcome null; a canary
context that fetched no hour file probes Dukascopy with the `probe` GET; probe commits carry head's commit time, so a
re-run on the same head pushes the same commits.

Fourth review of §16.18 A6 (R-AN-84, decision 31): the REST write probes pass on `scope` as well as `denied`, and a
canary `net.dukascopy` whose fetches all gave 0 (an outage or a blocked network: the call is `no_call:data`) has pass
null, so the call's probe verdict is `incomplete`, not `fail`; `probe` keeps "200".
"""
import json
import os
import re
import shutil
import subprocess

from . import rec

SCHEMA = "analyst-probe/1"
GA_REPO = "Mohamedalhaaj/gold-analyst"
GFW_REPO = "Mohamedalhaaj/gold-forward-watch"
PUBLIC_REPO = "octocat/Hello-World"
DUKASCOPY_URL = "https://datafeed.dukascopy.com/datafeed/XAUUSD/2026/09/01/10h_ticks.bi5"
GOOGLE_URL = "https://www.google.com"
RAW_PUBLIC_URL = "https://raw.githubusercontent.com/octocat/Hello-World/master/README"
API_ROOT_URL = "https://api.github.com/"
HTTP_TIMEOUT_S = 8
GIT_TIMEOUT_S = 30
REST_TIMEOUT_S = 20
CANARY_BUDGET_S = 120
SEEN_BOUND_S = {"canary": 20, "probe": 600}
SEEN_POLL_S = 5
WORKFLOW_YML = (b"name: probe\n"
                b"on: [push, workflow_dispatch]\n"
                b"jobs:\n"
                b"  p:\n"
                b"    runs-on: ubuntu-latest\n"
                b"    steps:\n"
                b"      - run: echo probe\n")

ORDER = ("net.dukascopy", "net.google", "net.raw_public", "net.api_root",
         "rest.inbox", "rest.ga", "rest.gfw", "rest.hooks",
         "git.ls_remote_inbox", "git.ls_remote_gfw", "git.ls_remote_gfw_proxy", "git.ls_remote_public",
         "git.ls_remote_public_proxy",
         "push_main", "force_main", "rest.force_main", "tag_push", "rest.tag_create", "other_branch_push",
         "branch_delete", "activity_seen_s", "rest.delete_branch",
         "ga_push", "ga_push_proxy", "workflow_push", "admin_probe")
MEASURED = ("net.api_root", "activity_seen_s")
RECORD_NULL = ("workflow_push",)            # judged from the inbox's runs (A12 inbox_runs), null in the record
REST = ("rest.inbox", "rest.ga", "rest.gfw", "rest.hooks", "rest.force_main", "rest.tag_create",
        "rest.delete_branch", "admin_probe")
GIT = ("git.ls_remote_inbox", "git.ls_remote_gfw", "git.ls_remote_gfw_proxy", "git.ls_remote_public",
       "git.ls_remote_public_proxy", "push_main", "force_main", "tag_push", "other_branch_push", "branch_delete",
       "ga_push", "ga_push_proxy", "workflow_push")
STATUS_RE = re.compile(rb"HTTP/[0-9.]+ ([0-9]{3})")
ERR_STATUS_RE = re.compile(rb"\(HTTP ([0-9]{3})\)")


def direct_url(repo):
    """The direct form D(x)."""
    return f"https://github.com/{repo}.git"


def proxy_url(origin_url, inbox, repo):
    """The proxy form P(x): origin's URL with its trailing `<inbox>` (with or without .git) replaced by x; None when
    origin's URL does not end that way."""
    if not origin_url or not inbox:
        return None
    for suffix, tail in ((inbox + ".git", ".git"), (inbox, "")):
        if origin_url.endswith(suffix):
            return origin_url[:-len(suffix)] + repo + tail
    return None


def run_gh(args, timeout):
    """`gh api -i <args>`: (exit code or None on the timeout, stdout, stderr), or None when gh is absent."""
    exe = shutil.which("gh")
    if not exe:
        return None
    env = dict(os.environ, GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1", NO_COLOR="1", GH_PAGER="cat",
               PAGER="cat")
    try:
        p = subprocess.run([exe, "api", "-i", *args], capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return None, b"", b""
    except OSError:
        return None
    return p.returncode, p.stdout, p.stderr


def _split(res):
    """(HTTP status or None, body bytes, stderr bytes) of a `gh api -i` result."""
    rc, out, err = res
    out, err = out or b"", err or b""
    if rc is None:
        return None, b"", b""
    m = STATUS_RE.match(out)
    if m:
        body = b""
        for sep in (b"\r\n\r\n", b"\n\n"):
            i = out.find(sep)
            if i >= 0:
                body = out[i + len(sep):]
                break
        return int(m.group(1)), body, err
    m = ERR_STATUS_RE.search(err)
    return (int(m.group(1)) if m else None), b"", err


def _scope_message(body, err):
    msgs = []
    try:
        doc = json.loads(body.decode("utf-8"))
        if isinstance(doc, dict) and isinstance(doc.get("message"), str):
            msgs.append(doc["message"])
    except (ValueError, UnicodeDecodeError):
        pass
    for line in err.decode("utf-8", "replace").splitlines():
        line = line.strip()
        msgs.append(line[4:] if line.startswith("gh: ") else line)
    return any(m.startswith("GitHub access to") and "is not enabled for this session" in m for m in msgs)


def rest_outcome(res, count=False):
    if res is None:
        return "none"
    status, body, err = _split(res)
    if status is None or status >= 500:
        return "none"
    if 200 <= status < 300:
        if not count:
            return "ok"
        try:
            doc = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return "none"
        return len(doc) if isinstance(doc, list) else "none"
    if status == 403 and _scope_message(body, err):
        return "scope"
    if 400 <= status < 500:
        return "denied"
    return "none"


def git_outcome(rc, out):
    if rc == 0:
        return "accepted"
    if rc is not None and b"GH013" in (out or b""):
        return "refused_rule"
    return "refused"


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _rule(p, v, kind):
    """R-AN-84's "Pass iff" column for one outcome that is set and not error/none."""
    if p == "net.dukascopy":                # canary: null when every fetch gave 0 (fourth review of §16.18 A6)
        if not _is_int(v):
            return False
        return (True if v != 0 else None) if kind == "canary" else v == 200
    if p == "net.google":
        return _is_int(v) and (v == 0 or not 200 <= v <= 399)
    if p == "net.raw_public":
        return v != 200
    if p == "rest.inbox":
        return v == "ok"
    if p in ("rest.ga", "rest.gfw", "admin_probe"):
        return v in ("scope", "denied")
    if p == "rest.hooks":
        return (v == 0) if _is_int(v) else None
    if p in ("rest.force_main", "rest.tag_create", "rest.delete_branch"):
        return v in ("denied", "scope")     # refused either way; only `ok` fails (fourth review of §16.18 A6)
    if p == "git.ls_remote_inbox":
        return v == "accepted"
    if p.startswith("git.ls_remote_"):
        return v == "refused"
    if p in ("push_main", "force_main", "tag_push", "other_branch_push", "branch_delete"):
        return v in ("refused", "refused_rule")
    if p in ("ga_push", "ga_push_proxy"):
        return v == "refused"
    return None


def passes(outcomes, kind):
    """{probe: True | False | None}. Null: an outcome `error` or `none`, a probe not run in time, a measured probe,
    `workflow_push`, every REST probe when the REST positive control (`rest.inbox` ok) failed, and every git probe
    when the git positive control (`git.ls_remote_inbox` accepted) failed."""
    rest_ok = outcomes.get("rest.inbox") == "ok"
    git_ok = outcomes.get("git.ls_remote_inbox") == "accepted"
    out = {}
    for p in ORDER:
        v = outcomes.get(p)
        if v is None or v in ("error", "none") or p in MEASURED or p in RECORD_NULL \
                or (p in REST and not rest_ok) or (p in GIT and not git_ok):
            out[p] = None
        else:
            out[p] = _rule(p, v, kind)
    return out


def verdict(pass_map):
    """`fail` if any pass is false; else `incomplete` if a probe other than the measured ones and `workflow_push` has
    pass null; else `pass`."""
    if any(v is False for v in pass_map.values()):
        return "fail"
    if any(v is None for p, v in pass_map.items() if p not in MEASURED and p not in RECORD_NULL):
        return "incomplete"
    return "pass"


class Prober:
    """Runs the probes in R-AN-84's order against origin (the inbox). `git` is the session's gitops.Git, `gh(args,
    timeout)` runs `gh api -i`, `http_get(url, timeout)` gives (status, body). With `budget_s`, a probe starts only
    while the set has run for less than that (a canary context); one probe's failure never stops the others."""

    def __init__(self, git, gh, http_get, mono, sleep, now, inbox, head, kind, tag, direct=direct_url, fetched=None,
                 budget_s=None):
        self.git, self.gh, self.http_get = git, gh, http_get
        self.mono, self.sleep, self.now = mono, sleep, now
        self.inbox, self.head, self.kind, self.tag = inbox, head, kind, tag
        self.direct = direct
        self.fetched = list(fetched or [])
        self.budget_s = budget_s
        self.outcomes = {p: None for p in ORDER}
        self.t0 = None
        self.c = {}

    # ------------------------------------------------------------ helpers
    def _in_time(self):
        return self.budget_s is None or self.mono() - self.t0 < self.budget_s

    def step(self, name, fn):
        if not self._in_time():
            return
        try:
            self.outcomes[name] = fn()
        except Exception:  # noqa: BLE001 - the probe could not be set up
            self.outcomes[name] = "error"

    def _http(self, url):
        code, _body = self.http_get(url, HTTP_TIMEOUT_S)
        return int(code or 0)

    def _rest(self, *args, count=False):
        if not self.inbox:
            raise ValueError("no inbox")
        return rest_outcome(self.gh(list(args), REST_TIMEOUT_S), count=count)

    def _git(self, *args):
        rc, out = self.git.try_cmd(*args, timeout=GIT_TIMEOUT_S)
        return git_outcome(rc, out)

    def _proxy(self, repo):
        u = proxy_url(self.origin, self.inbox, repo)
        if u is None:
            raise ValueError("origin's URL has another form")
        return u

    def _dukascopy(self):
        if self.kind == "canary" and self.fetched:
            for f in self.fetched:
                code = f.get("http") or 0
                if code != 0:
                    return int(code)
            return 0
        return self._http(DUKASCOPY_URL)

    def _commits(self):
        """c1 = head's tree on head; c3 = head's tree on head's first parent; c2 = head plus the probe workflow."""
        head = self.head
        tree = self.git.tree_of(head)
        when = self.git.commit_time(head)
        c = {"c1": self.git.commit_tree_at(tree, [head], "probe push_main (must be refused)", when),
             "c2": self.git.commit_one_file(head, ".github/workflows/probe.yml", WORKFLOW_YML,
                                            "probe workflow_push", when_epoch=when)}
        try:
            parent = self.git.first_parent(head)
        except Exception:  # noqa: BLE001 - a root commit has none
            parent = None
        if parent:
            c["c3"] = self.git.commit_tree_at(tree, [parent], "probe force_main (must be refused)", when)
        return c

    def _seen(self, ref, sha):
        """activity_seen_s: poll the inbox's activity for `ref` every SEEN_POLL_S until an answer lists `sha`."""
        bound = SEEN_BOUND_S[self.kind]
        t0 = self.mono()
        path = f"repos/{self.inbox}/activity?ref={ref}&per_page=10"
        while True:
            res = self.gh([path], REST_TIMEOUT_S)
            if res is None:
                return -1                                  # no gh: it cannot be polled
            status, body, _err = _split(res)
            if status == 200:
                try:
                    doc = json.loads(body.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    doc = None
                if isinstance(doc, list) and any(isinstance(e, dict) and e.get("after") == sha for e in doc):
                    return int(self.mono() - t0)
            if self.mono() - t0 >= bound:
                return -1
            self.sleep(SEEN_POLL_S)

    def _branch_delete(self):
        ref = f"refs/heads/claude/probe-del-{self.tag}"
        rc, _out = self.git.try_cmd("push", "origin", f"{self.head}:{ref}", timeout=GIT_TIMEOUT_S)
        if rc != 0:
            self.outcomes["branch_delete"] = "error"       # the creation must land
            self.outcomes["activity_seen_s"] = -1
            return
        try:
            self.outcomes["activity_seen_s"] = self._seen(ref, self.head) if self.inbox else -1
        except Exception:  # noqa: BLE001
            self.outcomes["activity_seen_s"] = -1
        self.outcomes["branch_delete"] = self._git("push", "origin", "--delete", ref)

    # ------------------------------------------------------------ the set (R-AN-84 "Order and verdict")
    def run(self):
        self.t0 = self.mono()
        probe_utc = rec.iso(self.now())
        try:
            self.origin = self.git.remote_url()
        except Exception:  # noqa: BLE001
            self.origin = None
        inbox, head, tag = self.inbox, self.head, self.tag
        self.step("net.dukascopy", self._dukascopy)
        self.step("net.google", lambda: self._http(GOOGLE_URL))
        self.step("net.raw_public", lambda: self._http(RAW_PUBLIC_URL))
        self.step("net.api_root", lambda: self._http(API_ROOT_URL))
        self.step("rest.inbox", lambda: self._rest(f"repos/{inbox}"))
        self.step("rest.ga", lambda: self._rest(f"repos/{GA_REPO}"))
        self.step("rest.gfw", lambda: self._rest(f"repos/{GFW_REPO}"))
        self.step("rest.hooks", lambda: self._rest(f"repos/{inbox}/hooks", count=True))
        self.step("git.ls_remote_inbox", lambda: self._git("ls-remote", "--heads", "origin"))
        self.step("git.ls_remote_gfw", lambda: self._git("ls-remote", "--heads", self.direct(GFW_REPO)))
        self.step("git.ls_remote_gfw_proxy", lambda: self._git("ls-remote", "--heads", self._proxy(GFW_REPO)))
        self.step("git.ls_remote_public", lambda: self._git("ls-remote", "--heads", self.direct(PUBLIC_REPO)))
        self.step("git.ls_remote_public_proxy",
                  lambda: self._git("ls-remote", "--heads", self._proxy(PUBLIC_REPO)))
        if self._in_time():
            try:
                self.c = self._commits()
            except Exception:  # noqa: BLE001 - every probe that needs a commit is then `error`
                self.c = {}
        c = self.c
        self.step("push_main", lambda: self._git("push", "origin", f"{c['c1']}:refs/heads/main"))
        c3_landed = False
        if self._in_time() and c.get("c3"):
            rc, _out = self.git.try_cmd("push", "origin", f"{c['c3']}:refs/heads/claude/probe-c3-{tag}",
                                        timeout=GIT_TIMEOUT_S)
            c3_landed = rc == 0

        def needs_c3(fn):
            return fn() if c3_landed else "error"
        self.step("force_main", lambda: needs_c3(lambda: self._git("push", "--force", "origin",
                                                                   f"{c['c3']}:refs/heads/main")))
        self.step("rest.force_main", lambda: needs_c3(lambda: self._rest(
            "-X", "PATCH", f"repos/{inbox}/git/refs/heads/main", "-f", f"sha={c['c3']}", "-F", "force=true")))
        self.step("tag_push", lambda: self._git("push", "origin", f"{head}:refs/tags/probe-{tag}"))
        self.step("rest.tag_create", lambda: self._rest("-X", "POST", f"repos/{inbox}/git/refs", "-f",
                                                        f"ref=refs/tags/probe-r-{tag}", "-f", f"sha={head}"))
        self.step("other_branch_push", lambda: self._git("push", "origin", f"{head}:refs/heads/probe-{tag}"))
        if self._in_time():
            try:
                self._branch_delete()
            except Exception:  # noqa: BLE001
                self.outcomes["branch_delete"] = "error"
                self.outcomes["activity_seen_s"] = -1
        self.step("rest.delete_branch", lambda: self._rest(
            "-X", "DELETE", f"repos/{inbox}/git/refs/heads/claude/probe-del-{tag}"))
        self.step("ga_push", lambda: self._git("push", self.direct(GA_REPO),
                                               f"{head}:refs/heads/claude/probe-ga-{tag}"))
        self.step("ga_push_proxy", lambda: self._git("push", self._proxy(GA_REPO),
                                                     f"{head}:refs/heads/claude/probe-ga-{tag}"))
        self.step("workflow_push", lambda: self._git("push", "origin", f"{c['c2']}:refs/heads/claude/probe-wf-{tag}"))
        self.step("admin_probe", lambda: self._rest("-X", "PUT", f"repos/{inbox}/actions/permissions", "-F",
                                                    "enabled=false"))
        p = passes(self.outcomes, self.kind)
        return {"schema": SCHEMA, "tag": tag, "kind": self.kind, "repo_head_sha": head, "probe_utc": probe_utc,
                "outcomes": dict(self.outcomes), "pass": p, "verdict": verdict(p)}
