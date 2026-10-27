"""A local world for the session scripts and analyst-merge (register §16.18 A6, the inbox): gold-analyst (a bare
origin whose main the merge writes), the agent inbox (a bare repository with the inbox's rules, whose main the real
analyst-inbox-mirror code keeps level with gold-analyst main), a session clone of the inbox, a merge workspace, a fake
GitHub API whose inbox activity entries carry the server times the tests choose, and a fake `gh` for the probes."""
import datetime as dt
import json

from ga import clock, context, gitops, merge, rec, store
from ga.session import Session
from synth import (INBOX_REPO, FakeAPI, FakeClock, FakeGH, FakeTD, World, default_slots, git, mode_json,
                   repo_files, store_files)

UTC = rec.UTC
INBOX_FROM = "2026-10-01T00:00:00Z"


def pins_with(files, **kw):
    pins = json.loads(files["analyst/PINS.json"])
    pins.update(kw)
    return json.dumps(pins, indent=1, sort_keys=True).encode() + b"\n"


class Harness:
    def __init__(self, tmp, mode="canary", canary_from="2026-11-10", live_from=None, store_last=dt.date(2026, 11, 9),
                 extra=None, slots_until=dt.date(2026, 11, 20), inbox_from_utc=INBOX_FROM, a14_from=None, m30=None):
        files = repo_files({"analyst/MODE.json": mode_json(mode, canary_from=canary_from, live_from=live_from,
                                                           a14_from=a14_from)})
        files["analyst/PINS.json"] = pins_with(files, inbox_from_utc=inbox_from_utc)
        files.update(store_files(clock.days_range(dt.date(2026, 9, 1), store_last)))
        files[store.SLOTS_PATH] = default_slots(slots_until).encode()
        # §16.18 A14 (R-AN-12 step 3b, R-AN-21): the whole forecast store, every slot; slots 9, 10, 15 as above
        files[context.FORECASTS_PATH] = default_slots(slots_until, slots=tuple(range(20))).encode()
        files.update(extra or {})
        self.world = World(tmp, files, protect_main=True, inbox=True)
        self.t = [dt.datetime(2026, 11, 10, 7, 20, tzinfo=UTC)]
        self.api = FakeAPI(self.now)
        self.feed = FakeTD(now=self.now, m30=m30)       # Twelve Data (register R-AN-87, §16.18 A9, A14)
        self.clk = FakeClock()
        self.log = []
        self.merge_log = []
        self.sess_dir = self.world.clone_inbox("session")
        self.gh = FakeGH(INBOX_REPO, self.world.inbox)
        self.probe_get = lambda u, t: (0, None)
        self.known = {}
        self.run_id = 5000
        self.merge_fail = False
        self.auto_mirror = True
        self.runner = None
        self.cc_version = "test-cc"          # None: the session reads `claude --version` itself (records.cc_version)

    def now(self):
        return self.t[0]

    def at(self, *a):
        self.t[0] = dt.datetime(*a, tzinfo=UTC)
        return self.t[0]

    def session(self, sess_dir=None):
        d = sess_dir or self.sess_dir
        return Session(d, git=gitops.Git(d), now=self.now, http_get=self.feed, mono=self.clk.mono,
                       sleep=self.clk.sleep, env={"CLAUDE_CODE_SESSION_ID": "sess-1"}, probe_get=self.probe_get,
                       cc_version=self.cc_version, out=self.log.append, gh=self.gh, direct_url=self.world.direct_url)

    def run(self, step, *a, sess_dir=None, **kw):
        self.world.deny_main(True)
        try:
            return getattr(self.session(sess_dir), step)(*a, **kw)
        finally:
            self.world.deny_main(False)

    def new_session_dir(self, name):
        """A second routine session: its own clone of the inbox and its own scratch state."""
        return self.world.clone_inbox(name)

    def register_pushes(self, t=None, only=None, analyst_only=True):
        """Add inbox activity entries for new claude/ branch tips, timed `t` (default now)."""
        out = git(self.world.inbox, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/claude/")
        new = []
        for line in out.splitlines():
            ref, sha = line.split()
            if self.known.get(ref) == sha:
                continue
            if analyst_only and not ref.startswith("refs/heads/claude/analyst-"):
                continue
            if only and only not in ref:
                continue
            self.api.add_push(ref, self.known.get(ref, "0" * 40), sha, t or self.now(),
                              typ="push" if ref in self.known else "branch_creation")
            self.known[ref] = sha
            new.append((ref, sha))
        return new

    def workspace(self, name="merge"):
        return merge.Workspace(self.world.clone(name), inbox_url=str(self.world.inbox),
                               **({"runner": self.runner} if self.runner else {}))

    def merge(self, dispatch=True, event="schedule"):
        """One analyst-merge run (closer step 1 processes every registered inbox push); then the inbox mirror."""
        ws = self.workspace("merge")
        if self.merge_fail:
            def boom(*a, **k):
                raise RuntimeError("push failed")
            ws.commit_push = boom
        self.run_id += 1
        try:
            return merge.run_merge(ws, self.api, self.now, {"workflow": "analyst-merge", "id": self.run_id,
                                                            "attempt": 1, "event": event},
                                   dispatch=dispatch, log=self.merge_log.append)
        finally:
            if self.auto_mirror:
                self.world.mirror()

    def push_and_merge(self, t=None, only=None):
        new = self.register_pushes(t, only)
        return self.merge() if new else None

    def main_json(self, path):
        b = self.world.main_file(path)
        return None if b is None else json.loads(b)

    def write_plan(self, obj, sess_dir=None):
        p = (sess_dir or self.sess_dir) / ".analyst_run" / "plan.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, ensure_ascii=False), "utf-8")
        return p

    def agent_branch(self, branch, files=None, delete=(), base="main"):
        """Craft a branch in the inbox as a tampering agent would: add/modify/delete files on top of main."""
        d = self.world.clone_inbox("agent")
        git(d, "checkout", "-q", "-B", "work", f"origin/{base}" if base == "main" else base)
        for p, b in (files or {}).items():
            fp = d / p
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_bytes(b if isinstance(b, bytes) else b.encode())
        for p in delete:
            git(d, "rm", "-q", p)
        git(d, "add", "-A")
        git(d, "commit", "-q", "-m", "x", "--allow-empty")
        git(d, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        return git(d, "rev-parse", "HEAD")

    def witnesses(self, mode="canary"):
        pre = "analyst/canary/witness/" if mode == "canary" else "analyst/witness/"
        return [self.main_json(p) for p in self.world.main_list(pre)]

    def last_witness(self, mode="canary"):
        ws = self.witnesses(mode)
        return ws[-1] if ws else None


PLAN = {"action": "buy", "stop_mult": 1.5, "target_mult": 2.0, "confidence": "low", "reason_ar": "تجربة",
        "key_risks_ar": "خطر"}
