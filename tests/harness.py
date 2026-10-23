"""A local world for the session scripts and analyst-merge: a bare origin, a session clone, a merge workspace, a fake
GitHub API whose activity entries carry the server times the tests choose."""
import datetime as dt
import json

from ga import clock, gitops, merge, rec, store
from ga.session import Session
from synth import FakeAPI, FakeClock, FakeFeed, World, default_slots, git, mode_json, repo_files, store_files

UTC = rec.UTC


class NoNetGit(gitops.Git):
    def ls_remote_ok(self, url):
        return False


class Harness:
    def __init__(self, tmp, mode="canary", canary_from="2026-11-10", live_from=None, store_last=dt.date(2026, 11, 9),
                 extra=None, slots_until=dt.date(2026, 11, 20)):
        files = repo_files({"analyst/MODE.json": mode_json(mode, canary_from=canary_from, live_from=live_from)})
        files.update(store_files(clock.days_range(dt.date(2026, 9, 1), store_last)))
        files[store.SLOTS_PATH] = default_slots(slots_until).encode()
        files.update(extra or {})
        self.world = World(tmp, files, protect_main=True)
        self.t = [dt.datetime(2026, 11, 10, 7, 20, tzinfo=UTC)]
        self.api = FakeAPI(self.now)
        self.feed = FakeFeed()
        self.clk = FakeClock()
        self.log = []
        self.sess_dir = self.world.clone("session")
        self.known = {}
        self.run_id = 5000
        self.merge_fail = False

    def now(self):
        return self.t[0]

    def at(self, *a):
        self.t[0] = dt.datetime(*a, tzinfo=UTC)
        return self.t[0]

    def session(self):
        return Session(self.sess_dir, git=NoNetGit(self.sess_dir), now=self.now, http_get=self.feed,
                       mono=self.clk.mono, sleep=self.clk.sleep, env={"CLAUDE_CODE_SESSION_ID": "sess-1"},
                       probe_get=lambda u, t: (0, None), cc_version="test-cc", out=self.log.append)

    def run(self, step, *a, **kw):
        self.world.deny_main(True)
        try:
            return getattr(self.session(), step)(*a, **kw)
        finally:
            self.world.deny_main(False)

    def register_pushes(self, t=None, only=None):
        """Add activity entries for new claude/analyst-* branch tips, timed `t` (default now)."""
        out = git(self.world.origin, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/claude/")
        new = []
        for line in out.splitlines():
            ref, sha = line.split()
            if not ref.startswith("refs/heads/claude/analyst-") or self.known.get(ref) == sha:
                continue
            if only and only not in ref:
                continue
            self.api.add_push(ref, self.known.get(ref, "0" * 40), sha, t or self.now())
            self.known[ref] = sha
            new.append((ref, sha))
        return new

    def merge(self, push=None, dispatch=True):
        """Run analyst-merge for one push (ref, sha) or closer-only (None)."""
        ws_dir = self.world.clone("merge")
        ws = merge.Workspace(ws_dir)
        if self.merge_fail:
            def boom(*a, **k):
                raise RuntimeError("push failed")
            ws.commit_push = boom
        self.run_id += 1
        ev = {"ref": push[0], "before": "0" * 40, "after": push[1]} if push else {}
        return merge.run_merge(ws, self.api, self.now, {"workflow": "analyst-merge", "id": self.run_id,
                                                        "attempt": 1, "event": "push" if push else "dispatch"}, ev,
                               dispatch_track=dispatch)

    def push_and_merge(self, t=None, only=None):
        last = None
        for p in self.register_pushes(t, only):
            last = self.merge(p)
        return last

    def main_json(self, path):
        b = self.world.main_file(path)
        return None if b is None else json.loads(b)

    def write_plan(self, obj):
        p = self.sess_dir / ".analyst_run" / "plan.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, ensure_ascii=False), "utf-8")
        return p

    def agent_branch(self, branch, files=None, delete=(), base="main"):
        """Craft a branch as a tampering agent would: add/modify/delete files on top of main."""
        d = self.world.clone("agent")
        git(d, "checkout", "-q", "-B", "work", f"origin/{base}" if base == "main" else base)
        for p, b in (files or {}).items():
            fp = d / p
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_bytes(b if isinstance(b, bytes) else b.encode())
        for p in delete:
            git(d, "rm", "-q", p)
        git(d, "add", "-A")
        git(d, "commit", "-q", "-m", "x", "--allow-empty")
        git(d, "push", "-q", "-f", "origin", f"HEAD:refs/heads/{branch}")
        return git(d, "rev-parse", "HEAD")

    def witnesses(self, mode="canary"):
        pre = "analyst/canary/witness/" if mode == "canary" else "analyst/witness/"
        return [self.main_json(p) for p in self.world.main_list(pre)]

    def last_witness(self, mode="canary"):
        ws = self.witnesses(mode)
        return ws[-1] if ws else None


PLAN = {"action": "buy", "stop_mult": 1.5, "target_mult": 2.0, "confidence": "low", "reason_ar": "تجربة",
        "key_risks_ar": "خطر"}
