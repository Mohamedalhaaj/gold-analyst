"""The routine session steps: start, context, lock, lesson-start/lock, revision-start/lock (prereg R1, R3, §3, §5;
register R-AN-24..R-AN-31, R-AN-48, R-AN-49, R-AN-71). FROZEN.

Only enums, ids, hashes and paths are printed; no model text reaches stdout, a commit message or a shell argument.
Inputs are read at origin/main (repo_head_sha) through git, never from the working tree. Exit codes: 0 done or a clean
refusal, 1 a technical failure, 2 an invalid plan/lesson/playbook that the agent may fix and lock again.
"""
import datetime as dt
import json
import os
import time
from pathlib import Path

from . import clock, context, gitops, hours, memory, paths, plan, rec, records, tables, witness
from .repo import GitView

WORK_DIRNAME = ".analyst_run"
MAX_FAILURES = 3
PROBE_TIMEOUT_S = 8


class Refusal(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Session:
    def __init__(self, repo_dir, git=None, now=None, http_get=None, mono=time.monotonic, sleep=time.sleep,
                 env=None, work_dir=None, view_factory=None, probe_get=None, cc_version=None, out=print):
        self.repo_dir = Path(repo_dir)
        self.git = git or gitops.Git(repo_dir)
        self.now = now or (lambda: dt.datetime.now(rec.UTC).replace(microsecond=0))
        self.http_get = http_get or hours.urllib_get
        self.probe_get = probe_get or hours.urllib_get
        self.mono, self.sleep = mono, sleep
        self.env = os.environ if env is None else env
        self.work = Path(work_dir) if work_dir else self.repo_dir / WORK_DIRNAME
        self.view_factory = view_factory or (lambda sha: GitView(self.repo_dir, sha))
        self.cc_version = cc_version
        self.out = out

    # ------------------------------------------------------------ helpers
    def _state_path(self):
        return self.work / "state.json"

    def _load_state(self):
        p = self._state_path()
        return json.loads(p.read_text("utf-8")) if p.exists() else None

    def _save_state(self, st):
        self.work.mkdir(parents=True, exist_ok=True)
        self._state_path().write_text(json.dumps(st, indent=1, sort_keys=True), "utf-8")

    def _selfrep(self, key):
        if self.cc_version is None:
            self.cc_version = records.cc_version() or ""
        return records.self_report(self.now(), key, self.env, self.cc_version or None)

    def _push(self, base, path, data, branch, until, st=None, key=None):
        """Push one new file on a new branch, retrying the same commit until `until` (aware UTC)."""
        commit = (st or {}).get(key) if key else None
        if not commit:
            commit = self.git.commit_one_file(base, path, data, f"analyst {path.rsplit('/', 1)[-1]}")
            if st is not None and key:
                st[key] = commit
                self._save_state(st)
        while True:
            ok, _ = self.git.push(commit, branch)
            if ok:
                return True
            if self.now() >= until:
                return False
            self.sleep(10)

    # ------------------------------------------------------------ start (R-AN-24)
    def start(self):
        now = self.now()
        head = self.git.fetch_main()
        view = self.view_factory(head)
        mode_doc = paths.read_mode(view)
        mode = mode_doc["mode"]
        params = paths.read_params(view)
        try:
            w, n = self._start_checks(view, mode_doc, params, now)
        except Refusal as r:
            self.out(f"refused {r.code}")
            return 0
        frozen = records.frozen_block(view)
        doc = records.attempt("call", mode, w.call_id, n, w.call_id, head, frozen, self._selfrep("started_utc"))
        path = paths.attempt_path(mode, w.call_id, n)
        head2 = self.git.fetch_main()
        if witness.has_terminal(self.view_factory(head2), mode, w.call_id):
            self.out("refused closed")
            return 0
        st = {"kind": "call", "mode": mode, "call_id": w.call_id, "n": n, "attempt_path": path,
              "repo_head_sha_start": head}
        self._save_state(st)
        ok = self._push(head, path, rec.record_bytes(doc), f"claude/analyst-{mode}-attempt-{w.call_id}-{n}",
                        min(w.deadline_utc, now + dt.timedelta(minutes=3)), st, "attempt_commit")
        if not ok:
            self.out("error attempt_push")
            return 1
        self.out(f"attempt_pushed {w.call_id} n={n} mode={mode}")
        return 0

    def _start_checks(self, view, mode_doc, params, now):
        mode = mode_doc["mode"]
        cands = [w for w in clock.windows_on(now.date()) if w.in_start_interval(now, int(params["T_run_min"]))]
        if not cands:
            raise Refusal("outside_span")
        w = cands[0]
        if mode == "off":
            raise Refusal("mode_off")
        hol = tables.load_holidays(view)
        if not hol.has_table(w.day):
            raise Refusal("no_table")
        if hol.is_holiday(w):
            raise Refusal("skipped:holiday")
        _m, a, b = paths.mode_period(mode_doc)
        if a is None or w.day < a or (b is not None and w.day > b):
            raise Refusal("not_expected")
        state = view.read_json(paths.STATE_PATH)
        if state is not None and state.get("calls_enabled") is False:
            raise Refusal("calls_disabled")
        if witness.has_terminal(view, mode, w.call_id):
            raise Refusal("closed")
        acc = witness.accepted_attempts(view, mode, w.call_id)
        if len(acc) >= 2:
            raise Refusal("attempt_cap")
        lim = rec.iso(now - dt.timedelta(minutes=int(params["concurrency_min"])))
        if any((a_.get("window") or {}).get("time_utc", "") > lim for a_ in acc):
            raise Refusal("concurrent")
        return w, len(acc) + 1

    # ------------------------------------------------------------ context (R-AN-14..R-AN-23)
    def context(self):
        st = self._load_state()
        if not st or st.get("kind") != "call" or st.get("locked"):
            self.out("error no_attempt")
            return 1
        asof = self.now()
        head = self.git.fetch_main()
        view = self.view_factory(head)
        mode = st["mode"]
        if witness.has_terminal(view, mode, st["call_id"]):
            self.out("refused closed")
            return 0
        w = clock.window_of(st["call_id"])
        params = paths.read_params(view)
        fetcher = hours.Fetcher(get=self.http_get, clock=self.mono, sleep=self.sleep, now=self.now)
        ctx = context.build(view, head, mode, w, asof, params, fetcher)
        sha = context.context_sha256(ctx)
        st.update(context_sha256=sha, repo_head_sha=head, failures=0)
        if mode == "canary":
            st["canary_probe"] = self.probes(head, w, fetched=ctx["files"])
        self.work.mkdir(parents=True, exist_ok=True)
        (self.work / "context.json").write_bytes(rec.record_bytes(ctx))
        self._save_state(st)
        if ctx["status"] != "ok":
            return self._finish_call(st, ctx, ctx["status"], None, None)
        self.out(f"context_ok {w.call_id} sha256={sha} path={WORK_DIRNAME}/context.json")
        return 0

    # ------------------------------------------------------------ lock (R-AN-28..R-AN-30)
    def lock(self, plan_file=None, decline=False):
        st = self._load_state()
        if not st or st.get("kind") != "call" or "context_sha256" not in st:
            self.out("error no_context")
            return 1
        if st.get("locked"):
            self.out("refused already_locked")
            return 0
        ctx = json.loads((self.work / "context.json").read_text("utf-8"))
        if context.context_sha256(ctx) != st["context_sha256"]:
            self.out("error context_modified")
            return 1
        if ctx["status"] != "ok":
            self.out(f"refused {ctx['status']}")
            return 0
        if decline:
            return self._finish_call(st, ctx, "no_call:refusal", None, None)
        try:
            raw = Path(plan_file).read_bytes()
        except OSError:
            raw = None
        if raw is None:
            obj, errs = None, ["missing_plan"]
        else:
            obj, errs = plan.validate(raw)
        if errs:
            st["failures"] = int(st.get("failures", 0)) + 1
            self._save_state(st)
            self.out("invalid " + ",".join(errs) + f" failures={st['failures']}")
            if st["failures"] >= MAX_FAILURES:
                return self._finish_call(st, ctx, "no_call:invalid", None, None)
            return 2
        status = "stay_out" if obj["action"] == "stay_out" else "called"
        return self._finish_call(st, ctx, status, obj, rec.sha256_bytes(raw))

    def _finish_call(self, st, ctx, status, plan_obj, plan_sha):
        head = self.git.fetch_main()
        view = self.view_factory(head)
        mode = st["mode"]
        if witness.has_terminal(view, mode, st["call_id"]):
            self.out("refused closed")
            return 0
        pins = paths.read_pins(view) or {}
        pb = memory.playbook_in_force(view, dt.date.fromisoformat(ctx["model_day"]), "live")
        doc = records.call(ctx, st["context_sha256"], st["n"], status, plan_obj, plan_sha,
                           int(st.get("failures", 0)), {"version": pb["version"], "sha256": pb["sha256"]}, pins,
                           records.frozen_block(view), self._selfrep("locked_utc"),
                           canary_probe=st.get("canary_probe") if mode == "canary" else None)
        path = paths.call_path(mode, st["call_id"])
        w = clock.window_of(st["call_id"])
        st["locked"] = True
        self._save_state(st)
        ok = self._push(st["repo_head_sha"], path, rec.record_bytes(doc), f"claude/analyst-{mode}-call-{w.call_id}",
                        w.deadline_utc, st, "lock_commit")
        if not ok:
            self.out("error lock_push")
            return 1
        self.out(f"locked {w.call_id} status={status}")
        return 0

    # ------------------------------------------------------------ canary probes (R-AN-71, C5-C7)
    def probes(self, head, w, fetched=()):
        net = {"dukascopy": any(f.get("http", 0) != 0 for f in fetched)}
        for name, u in (("google", "https://www.google.com"), ("api_github", "https://api.github.com"),
                        ("raw_gfw", "https://raw.githubusercontent.com/Mohamedalhaaj/gold-forward-watch/HEAD/"
                                    "README.md")):
            code, _ = self.probe_get(u, PROBE_TIMEOUT_S)
            net[name] = int(code)
        try:
            net["ls_remote_gfw"] = bool(self.git.ls_remote_ok("https://github.com/Mohamedalhaaj/gold-forward-watch"))
        except Exception:  # noqa: BLE001
            net["ls_remote_gfw"] = False
        out = {"network": net}
        try:
            tree = self.git.tree_of(head)
            c1 = self.git.commit_tree(tree, [head], "canary probe push_main (must be refused)")
            out["push_main"] = "accepted" if self.git.push(c1, "main")[0] else "refused"
            wf = b"name: probe\non:\n  workflow_dispatch:\njobs:\n  p:\n    runs-on: ubuntu-latest\n    steps:\n" \
                 b"      - run: echo probe\n"
            c2 = self.git.commit_one_file(head, ".github/workflows/probe.yml", wf, "canary probe workflow_push")
            out["workflow_push"] = "accepted" if self.git.push(c2, f"claude/probe-wf-{w.call_id}")[0] else "refused"
            c3 = self.git.commit_tree(tree, [self.git.first_parent(head)], "canary probe force_main (must be refused)")
            out["force_main"] = "accepted" if self.git.push(c3, "main", force=True)[0] else "refused"
        except Exception:  # noqa: BLE001
            out["git_probe_error"] = True
        return out

    # ------------------------------------------------------------ lessons (R-AN-48, R-AN-71)
    def lesson_start(self):
        head = self.git.fetch_main()
        view = self.view_factory(head)
        mode = paths.read_mode(view)["mode"]
        if mode == "off":
            self.out("refused mode_off")
            return 0
        st = self._load_state() or {}
        pushed = st.get("lesson_pushed", {})
        done = list(st.get("lesson_done", []))          # locked in this session, perhaps not merged yet
        cand = None
        if mode == "canary":
            fx = view.read_json(paths.CANARY_SETTLED_FIXTURE)
            pool = [(fx["call_id"], paths.CANARY_SETTLED_FIXTURE)] if fx else []
        else:
            pool = sorted(((p.rsplit("/", 1)[1][:-5], p) for p in view.list("analyst/settled/")
                           if p.endswith(".json")), key=lambda x: clock.window_key(x[0]))
        for cid, p in pool:
            if cid in done or view.exists(paths.lesson_path(mode, cid)):
                continue
            n_acc = len(witness.accepted_attempts(view, mode, f"lesson-{cid}"))
            if max(n_acc, pushed.get(cid, 0)) >= 2:
                continue
            cand = (cid, view.read(p))
            break
        if cand is None:
            self.out("none")
            return 0
        cid, settled_bytes = cand
        n_acc = len(witness.accepted_attempts(view, mode, f"lesson-{cid}"))
        n = max(n_acc, pushed.get(cid, 0)) + 1
        if n > 2:
            self.out("none")
            return 0
        frozen = records.frozen_block(view)
        doc = records.attempt("lesson", mode, f"lesson-{cid}", n, cid, head, frozen, self._selfrep("started_utc"))
        path = paths.attempt_path(mode, f"lesson-{cid}", n)
        ok = self._push(head, path, rec.record_bytes(doc), f"claude/analyst-{mode}-attempt-lesson-{cid}-{n}",
                        self.now() + dt.timedelta(minutes=3))
        if not ok:
            self.out("error attempt_push")
            return 1
        pushed[cid] = n
        self.work.mkdir(parents=True, exist_ok=True)
        (self.work / "lesson_input.json").write_bytes(settled_bytes)
        pb = memory.playbook_in_force(view, self.now().date(), "live")
        st = {"kind": "lesson", "mode": mode, "call_id": cid, "n": n, "lesson_pushed": pushed, "lesson_done": done,
              "failures": 0,
              "repo_head_sha": head, "settled_sha256": rec.sha256_bytes(settled_bytes),
              "playbook_version": pb["version"]}
        self._save_state(st)
        self.out(f"lesson_attempt_pushed {cid} n={n} path={WORK_DIRNAME}/lesson_input.json")
        return 0

    def lesson_lock(self, lesson_file):
        st = self._load_state()
        if not st or st.get("kind") != "lesson" or st.get("locked"):
            self.out("error no_lesson_attempt")
            return 1
        try:
            obj = json.loads(Path(lesson_file).read_text("utf-8"))
            text = obj.get("text_ar") if isinstance(obj, dict) and set(obj) == {"text_ar"} else None
        except Exception:  # noqa: BLE001
            text = None
        doc = None
        if isinstance(text, str):
            doc = records.lesson(st["mode"], st["call_id"], "ok", text, st["settled_sha256"], st["playbook_version"],
                                 st["n"], self._selfrep("locked_utc"))
        if doc is None or not records.valid_lesson(doc):
            st["failures"] = int(st.get("failures", 0)) + 1
            self._save_state(st)
            self.out(f"invalid lesson failures={st['failures']}")
            if st["failures"] < MAX_FAILURES:
                return 2
            if st["n"] < 2:
                st["locked"] = True
                self._save_state(st)
                self.out("attempt_ended")
                return 0
            doc = records.lesson(st["mode"], st["call_id"], "failed", None, st["settled_sha256"],
                                 st["playbook_version"], st["n"], self._selfrep("locked_utc"))
        path = paths.lesson_path(st["mode"], st["call_id"])
        st["locked"] = True
        self._save_state(st)
        ok = self._push(st["repo_head_sha"], path, rec.record_bytes(doc),
                        f"claude/analyst-{st['mode']}-lesson-{st['call_id']}", self.now() + dt.timedelta(minutes=5),
                        st, "lock_commit")
        if ok:
            st["lesson_done"] = sorted(set(st.get("lesson_done", [])) | {st["call_id"]})
            self._save_state(st)
        self.out(f"lesson_locked {st['call_id']} status={doc['status']}" if ok else "error lesson_push")
        return 0 if ok else 1

    # ------------------------------------------------------------ revision (R-AN-49, R-AN-50)
    def revision_start(self):
        from . import weekly
        now = self.now()
        head = self.git.fetch_main()
        view = self.view_factory(head)
        mode = paths.read_mode(view)["mode"]
        if mode == "off":
            self.out("refused mode_off")
            return 0
        if now.weekday() != 5:
            self.out("refused not_saturday")
            return 0
        iso_week = clock.iso_week(now.date())
        if view.exists(paths.revision_record_path(mode, iso_week)):
            self.out("refused week_done")
            return 0
        ok_week, why = weekly.week_complete(view, mode, now.date())
        if not ok_week:
            self.out(f"refused incomplete_week {why}")
            return 0
        k = weekly.newest_playbook(view, mode)
        draw = f"revision-v{k + 1}"
        st = self._load_state() or {}
        n = max(len(witness.accepted_attempts(view, mode, draw)), st.get("revision_pushed", 0)) + 1
        if n > 2:
            self.out("refused attempt_cap")
            return 0
        doc = records.attempt("revision", mode, draw, n, None, head, records.frozen_block(view),
                              self._selfrep("started_utc"))
        ok = self._push(head, paths.attempt_path(mode, draw, n), rec.record_bytes(doc),
                        f"claude/analyst-{mode}-attempt-{draw}-{n}", now + dt.timedelta(minutes=3))
        if not ok:
            self.out("error attempt_push")
            return 1
        inp = weekly.revision_input(view, mode, k)
        self.work.mkdir(parents=True, exist_ok=True)
        (self.work / "revision_input.json").write_bytes(rec.record_bytes(inp))
        self._save_state({"kind": "revision", "mode": mode, "k": k, "n": n, "revision_pushed": n, "failures": 0,
                          "repo_head_sha": head, "iso_week": iso_week})
        self.out(f"revision_attempt_pushed v{k + 1} n={n} path={WORK_DIRNAME}/revision_input.json")
        return 0

    def revision_lock(self, playbook_file):
        from . import weekly
        st = self._load_state()
        if not st or st.get("kind") != "revision" or st.get("locked"):
            self.out("error no_revision_attempt")
            return 1
        try:
            raw = Path(playbook_file).read_bytes()
        except OSError:
            raw = b""
        errs = weekly.lint_playbook(raw, st["k"] + 1)
        if errs:
            st["failures"] = int(st.get("failures", 0)) + 1
            self._save_state(st)
            self.out("invalid " + ",".join(errs) + f" failures={st['failures']}")
            if st["failures"] >= MAX_FAILURES:
                st["locked"] = True
                self._save_state(st)
                self.out("attempt_ended")
                return 0
            return 2
        st["locked"] = True
        self._save_state(st)
        path = paths.playbook_path(st["mode"], st["k"] + 1)
        ok = self._push(st["repo_head_sha"], path, raw, f"claude/analyst-{st['mode']}-playbook-v{st['k'] + 1}",
                        self.now() + dt.timedelta(minutes=5), st, "lock_commit")
        self.out(f"playbook_locked v{st['k'] + 1}" if ok else "error playbook_push")
        return 0 if ok else 1
