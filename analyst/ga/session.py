"""The routine session steps: start, context, lock, lesson-start/lock, revision-start/lock, probe (prereg R1, R3, §3,
§5; register R-AN-24..R-AN-31, R-AN-48, R-AN-49, R-AN-71, R-AN-84, R-AN-87, §16.18 A6, A9). FROZEN.

Since §16.18 A9 `context` makes its market-data requests to Twelve Data (ga/td.py) without any key: the agent proxy
adds the environment's network secret outside the VM, and this code never reads, holds or prints a key.

Only enums, ids, hashes and paths are printed (and, in `probe`'s line, the record's `cc_version`, the first line of
`claude --version`); no model text reaches stdout, a commit message or a shell argument.
Inputs are read at origin/main (repo_head_sha) through git, never from the working tree. Since §16.18 A6 origin is
the agent inbox: its main is a gold-analyst main commit, one mirror run behind at most, and analyst-merge witnesses
pushes on a schedule. No step waits for a witness: an attempt branch in the inbox that no witness on origin/main
names, and that this session did not push itself, is a draw in progress (R-AN-24, `concurrent`). Exit codes: 0 done
or a clean refusal, 1 a technical failure, 2 an invalid plan/lesson/playbook that the agent may fix and lock again;
`probe`: 0 pass, 4 fail, 5 incomplete, 1 a technical failure.

§16.18 A14 (R-AN-90): for model days >= MODE.json a14_from, `start` selects the window by the clock (W1 or W2, else
W1b or W2b, else the trigger candidate of the current check), keeps R-AN-24's refusals first, then gates a first
draw by `trade_open` and `day_cap` (R-AN-89 steps 1-2, read from origin/main and the inbox's unmerged attempt and
call branches of the model day), and for a trigger candidate makes the XAUUSD_M30 request at or after T + 130 s
(`wake_data`) and recomputes R(T) from it (`not_triggered`). The attempt record carries `wake`; a second draw copies
it from its first attempt record and is not gated again.

§16.18 A15 (R-AN-90, R-AN-92, R-AN-95): for model days >= MODE.json a15_from `start` does not apply `trade_open` (R(T)
without step 1); `context` reads the day's attempt, call and terminal files once more, before its first Twelve Data
request, builds the open set O(R) by ga/review.open_sets and passes it to the context (the `review` block and the
`XAUUSD_M30R` request; a failed read is `error day_read`, exit 1, no call file); `lock` validates `analyst-plan/3`
for an `analyst-context/5` context and refuses an action outside the context's options (`action_not_allowed`, a
validation failure counted toward the cap of 3); hold and close lock as `review_hold` and `review_close`. In canary
mode, in a run on or after a15_from 00:00:00Z, `lesson-start` takes the pending canary settled records instead of the
fixture, and the canary revision reads the canary records; the canary playbook in force follows R-AN-50's A15 rule.
"""
import datetime as dt
import json
import os
import re
import time
from pathlib import Path

from . import clock, context, gitops, memory, paths, plan, probes, rec, records, review, tables, td, wake, witness
from .repo import GitView

WORK_DIRNAME = ".analyst_run"
MAX_FAILURES = 3
PROBE_EXIT = {"pass": 0, "fail": 4, "incomplete": 5}


def _loads_or_none(b):
    try:
        return rec.loads(b)
    except ValueError:
        return None


class DayReadFailed(Exception):
    """The model day's attempt, call or terminal files could not be read (R-AN-90): a technical failure."""


class Refusal(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Session:
    def __init__(self, repo_dir, git=None, now=None, http_get=None, mono=time.monotonic, sleep=time.sleep,
                 env=None, work_dir=None, view_factory=None, probe_get=None, cc_version=None, out=print, gh=None,
                 direct_url=None):
        self.repo_dir = Path(repo_dir)
        self.git = git or gitops.Git(repo_dir)
        self.now = now or (lambda: dt.datetime.now(rec.UTC).replace(microsecond=0))
        self.http_get = http_get or td.urllib_get          # the context's Twelve Data requests (§16.18 A9)
        self.probe_get = probe_get or td.urllib_get        # keeps an error's body, where Twelve Data puts its code
        self.gh = gh or probes.run_gh
        self.direct_url = direct_url or probes.direct_url
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

    def _cc_version(self):
        """The session's Claude Code version, read once per step by records.cc_version (the first line of
        `claude --version`; None when it is absent, fails or takes more than 10 s): the attempt and call records'
        `self_reported.cc_version` and, since the third review of §16.18 A7 (decision 22), the probe record's
        `cc_version` (R-AN-25, R-AN-84, R-AN-85 "Claude Code version")."""
        if self.cc_version is None:
            self.cc_version = records.cc_version() or ""
        return self.cc_version or None

    def _selfrep(self, key):
        return records.self_report(self.now(), key, self.env, self._cc_version())

    def _pushed_path(self):
        return self.work / "pushed.json"

    def own_commits(self):
        """Every commit this session has pushed (its scratch state, kept apart from state.json, which each start
        step rewrites)."""
        p = self._pushed_path()
        try:
            return set(json.loads(p.read_text("utf-8"))) if p.exists() else set()
        except ValueError:
            return set()

    def _note_own(self, commit):
        own = self.own_commits() | {commit}
        self.work.mkdir(parents=True, exist_ok=True)
        self._pushed_path().write_text(json.dumps(sorted(own)), "utf-8")

    def in_flight(self, view, mode, draw_id):
        """True when the inbox holds an attempt branch of `draw_id` (`claude/analyst-<mode>-attempt-<draw_id>-<n>`,
        R-AN-31) whose head is the `push.after` of no witness on origin/main (accepted, rejected or duplicate) and
        is not a commit this session pushed: a draw in progress that analyst-merge has not witnessed yet, or whose
        witness has not reached the inbox (R-AN-24, §16.18 A6). Raises gitops.GitError when the inbox cannot be
        listed."""
        name = f"refs/heads/claude/analyst-{mode}-attempt-{draw_id}-"
        rx = re.compile(re.escape(name) + r"[0-9]+")
        listed = [(sha, ref) for sha, ref in self.git.ls_remote_heads(f"{name}*") if rx.fullmatch(ref)]
        if not listed:
            return False
        named = {(w.get("push") or {}).get("after") for m in ("live", "canary") for w in witness.witnesses(view, m)}
        own = self.own_commits()
        return any(sha not in named and sha not in own for sha, _ref in listed)

    def _push(self, base, path, data, branch, until, st=None, key=None):
        """Push one new file on a new branch, retrying the same commit until `until` (aware UTC)."""
        commit = (st or {}).get(key) if key else None
        if not commit:
            commit = self.git.commit_one_file(base, path, data, f"analyst {path.rsplit('/', 1)[-1]}")
            if st is not None and key:
                st[key] = commit
                self._save_state(st)
        self._note_own(commit)
        while True:
            ok, _ = self.git.push(commit, branch)
            if ok:
                return True
            if self.now() >= until:
                return False
            self.sleep(10)

    # ------------------------------------------------------------ start (R-AN-24, R-AN-90)
    def start(self):
        now = self.now()
        head = self.git.fetch_main()
        view = self.view_factory(head)
        mode_doc = paths.read_mode(view)
        mode = mode_doc["mode"]
        params = paths.read_params(view)
        self._td_last = None
        try:
            w, n, wake_doc = self._start_checks(view, mode_doc, params, now)
        except Refusal as r:
            self.out(f"refused {r.code}")
            return 0
        except DayReadFailed:
            self.out("error day_read")
            return 1
        except gitops.GitError:
            self.out("error inbox_branches")
            return 1
        frozen = records.frozen_block(view)
        doc = records.attempt("call", mode, w.call_id, n, w.call_id, head, frozen, self._selfrep("started_utc"),
                              wake=wake_doc)
        path = paths.attempt_path(mode, w.call_id, n)
        head2 = self.git.fetch_main()
        if witness.has_terminal(self.view_factory(head2), mode, w.call_id):
            self.out("refused closed")
            return 0
        st = {"kind": "call", "mode": mode, "call_id": w.call_id, "n": n, "attempt_path": path,
              "repo_head_sha_start": head, "wake": wake_doc,
              "td_last_start_mono": self._td_last}
        self._save_state(st)
        ok = self._push(head, path, rec.record_bytes(doc), f"claude/analyst-{mode}-attempt-{w.call_id}-{n}",
                        min(w.deadline_utc, self.now() + dt.timedelta(minutes=3)), st, "attempt_commit")
        if not ok:
            self.out("error attempt_push")
            return 1
        self.out(f"attempt_pushed {w.call_id} n={n} mode={mode}")
        return 0

    def _select(self, mode_doc, params, now):
        """R-AN-90's window selection (R-AN-24's for days before a14_from): the W1 or W2 whose start interval
        contains now, else the W1b or W2b, else the trigger candidate of check T = now floored to the half hour."""
        t_run = int(params["T_run_min"])
        for w in clock.windows_on(now.date()):
            if w.in_start_interval(now, t_run):
                return w
        if paths.a14_applies(mode_doc, now.date()):
            for w in clock.windows_on(now.date(), a14=True):
                if w.name in clock.SECOND_LOOKS and w.in_start_interval(now, t_run):
                    return w
        T = clock.floor_half_hour(now)
        d = clock.tday(T)
        if paths.a14_applies(mode_doc, d) and T in clock.check_grid(d) and T not in clock.scheduled_checks(d) \
                and now <= T + dt.timedelta(minutes=15 - t_run):
            return clock.Window(d, clock.trigger_tag(T))
        raise Refusal("outside_span")

    def _start_checks(self, view, mode_doc, params, now):
        mode = mode_doc["mode"]
        w = self._select(mode_doc, params, now)
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
        if self.in_flight(view, mode, w.call_id):
            raise Refusal("concurrent")
        n = len(acc) + 1
        if n == 2:                                   # not gated again; keeps its first draw's wake (R-AN-90)
            first = view.read_json(paths.attempt_path(mode, w.call_id, 1)) or {}
            return w, n, first["wake"] if isinstance(first.get("wake"), dict) else records.wake_of(w)
        if not paths.a14_applies(mode_doc, w.day):
            return w, n, records.wake_of(w)
        wakes, trades = self.day_windows(view, mode, w.day)
        if paths.a15_applies(mode_doc, w.day):
            trades = []                              # R(T) without step 1: open trades never gate (§16.18 A15)
        T = w.check_utc
        if wake.open_trades(trades, T):
            raise Refusal("trade_open")
        if len(wake.earlier(wakes, T)) >= wake.DAY_CAP:
            raise Refusal("day_cap")
        if w.kind != "trigger":
            return w, n, records.wake_of(w)
        prov, bars = self._wake_request(T)
        if prov["status"] != "ok":
            raise Refusal(f"wake_data td_{prov['reason']}")
        r = wake.rule(w.day, T, bars, wakes, trades, hol.any_row(w.day))
        if r["result"] != "window" or r["tag"] != w.tag:
            raise Refusal("not_triggered")
        return w, n, records.wake_of(w, r["reason"], prov)

    def _wake_request(self, T):
        """The XAUUSD_M30 request of a trigger candidate's first draw (R-AN-90), made at or after T + 130 s, from
        22:00 UTC of the calendar day d − 33 days to T; its `bars` hold every element with t_utc <= T − 30 min."""
        wait = (T + dt.timedelta(seconds=130) - self.now()).total_seconds()
        if wait > 0:
            self.sleep(wait)
        d = clock.tday(T)
        a0 = d - dt.timedelta(days=33)
        a = dt.datetime(a0.year, a0.month, a0.day, 22, tzinfo=rec.UTC)
        fetcher = td.Fetcher(get=self.http_get, clock=self.mono, sleep=self.sleep, now=self.now)
        prov, values = fetcher.fetch(records.WAKE_REQ, td.m30_url(a, T), "30min")
        self._td_last = fetcher.last_start
        bars = wake.used_bars(values, T)
        prov["bars"] = [td.bar_obj(v) for v in bars]
        return prov, bars

    def day_windows(self, view, mode, d):
        """The model day's windows for E(T) and O(T) (R-AN-89, R-AN-90): ([{call_id, check_utc, kind, reason}] of
        the windows with an attempt, [{call_id, check_utc, exit_utc}] of those whose call is a buy or a sell). Read
        from origin/main and from the inbox's attempt and call branches of d whose file is not on main; a failed read
        raises DayReadFailed."""
        att, calls, terms = self.day_files(view, mode, d)
        wakes = []
        for cid in sorted({c for c, _n in att}, key=clock.window_key):
            w = clock.window_of(cid)
            docs = [att[k] for k in sorted(att) if k[0] == cid]
            reason = next((x["wake"].get("reason") for x in docs
                           if isinstance(x, dict) and isinstance(x.get("wake"), dict)), None)
            wakes.append({"call_id": cid, "check_utc": w.check_utc, "kind": w.kind, "reason": reason})
        trades = []
        for cid in sorted(set(calls) | set(terms), key=clock.window_key):
            doc = terms.get(cid) if cid in terms else calls.get(cid)
            if isinstance(doc, dict) and doc.get("status") == "called":
                w = clock.window_of(cid)
                trades.append({"call_id": cid, "check_utc": w.check_utc, "exit_utc": w.exit_utc})
        return wakes, trades

    def review_entries(self, view, mode, d):
        """R-AN-92's reading for `context`: the open_sets entries of model day d's windows that have a call or a
        terminal, in window order. A window's call status is its terminal's on origin/main, else its call file's on
        origin/main, else on its call branch; a failed read raises DayReadFailed."""
        _att, calls, terms = self.day_files(view, mode, d)
        out = []
        for cid in sorted(set(calls) | set(terms), key=clock.window_key):
            doc = terms.get(cid) if cid in terms else calls.get(cid)
            status = doc.get("status") if isinstance(doc, dict) else None
            out.append(review.entry_from_call(cid, clock.window_of(cid), status, calls.get(cid)))
        return out

    def day_files(self, view, mode, d):
        """({(call_id, n): attempt}, {call_id: call}, {call_id: terminal}) of model day d: origin/main, then the
        inbox's attempt and call branches of d whose file is not on main; a failed read raises DayReadFailed."""
        P = paths.prefix(mode)
        day = d.isoformat()
        att_rx = re.compile(rf"(?P<cid>{day}_(?:{clock.TAG_RE}))_(?P<n>[12])\.json")
        cid_rx = re.compile(rf"(?P<cid>{day}_(?:{clock.TAG_RE}))\.json")
        att, calls, terms = {}, {}, {}
        for p in view.list(f"{P}attempts/{day}_"):
            m = att_rx.fullmatch(p[len(P) + len("attempts/"):])
            if m:
                att[(m["cid"], int(m["n"]))] = view.read_json(p)
        for sub, out in (("calls/", calls), ("terminal/", terms)):
            for p in view.list(f"{P}{sub}{day}_"):
                m = cid_rx.fullmatch(p[len(P) + len(sub):])
                if m:
                    out[m["cid"]] = view.read_json(p)
        try:
            base = f"refs/heads/claude/analyst-{mode}-"
            for _sha, ref in self.git.ls_remote_heads(f"{base}attempt-{day}_*"):
                m = re.fullmatch(rf"{re.escape(base)}attempt-(?P<cid>{day}_(?:{clock.TAG_RE}))-(?P<n>[12])", ref)
                if m and (m["cid"], int(m["n"])) not in att:
                    att[(m["cid"], int(m["n"]))] = _loads_or_none(
                        self.git.ref_file(ref, paths.attempt_path(mode, m["cid"], int(m["n"]))))
            for _sha, ref in self.git.ls_remote_heads(f"{base}call-{day}_*"):
                m = re.fullmatch(rf"{re.escape(base)}call-(?P<cid>{day}_(?:{clock.TAG_RE}))", ref)
                if m and m["cid"] not in calls:
                    calls[m["cid"]] = _loads_or_none(self.git.ref_file(ref, paths.call_path(mode, m["cid"])))
        except gitops.GitError as e:
            raise DayReadFailed() from e
        return att, calls, terms

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
        if st.get("td_last_start_mono") is not None:   # 8.0 s after the start's request's last try (R-AN-90)
            wait = min(td.PACE_S, float(st["td_last_start_mono"]) + td.PACE_S - self.mono())
            if wait > 0:
                self.sleep(wait)
        open_ = None
        if paths.a15_applies(paths.read_mode(view), w.day):     # R-AN-92: read before the first request
            try:
                entries = [e for e in self.review_entries(view, mode, w.day) if e["call_id"] != w.call_id]
            except DayReadFailed:
                self.out("error day_read")
                return 1
            me = {"call_id": w.call_id, "entry_utc": w.entry_utc, "exit_utc": w.exit_utc, "status": None}
            entries = sorted(entries + [me], key=lambda e: (e["entry_utc"], e["call_id"] == w.call_id))
            entries = entries[:entries.index(me) + 1]
            open_ = review.open_sets(entries)[0][w.call_id]
        fetcher = td.Fetcher(get=self.http_get, clock=self.mono, sleep=self.sleep, now=self.now)
        ctx = context.build(view, head, mode, w, asof, params, fetcher, wake=st.get("wake") or records.wake_of(w),
                            review_open=open_)
        sha = context.context_sha256(ctx)
        st.update(context_sha256=sha, repo_head_sha=head, failures=0)
        self.work.mkdir(parents=True, exist_ok=True)
        (self.work / "context.json").write_bytes(rec.record_bytes(ctx))
        if mode == "canary":                       # after the context file (R-AN-84); live calls never probe
            st["canary_probe"] = self.run_probes(view, head, "canary", w.call_id, fetched=ctx["files"])
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
            p3 = plan.plan3_applies(ctx.get("schema"))
            obj, errs = plan.validate(raw, stay_reason=plan.stay_reason_applies(ctx.get("schema")), plan3=p3)
            if p3 and obj is not None and obj["action"] not in review.allowed_actions(ctx):
                obj, errs = None, ["action_not_allowed"]          # R-AN-26 (§16.18 A15)
        if errs:
            st["failures"] = int(st.get("failures", 0)) + 1
            self._save_state(st)
            self.out("invalid " + ",".join(errs) + f" failures={st['failures']}")
            if st["failures"] >= MAX_FAILURES:
                return self._finish_call(st, ctx, "no_call:invalid", None, None)
            return 2
        return self._finish_call(st, ctx, plan.STATUS_OF[obj["action"]], obj, rec.sha256_bytes(raw))

    def _finish_call(self, st, ctx, status, plan_obj, plan_sha):
        head = self.git.fetch_main()
        view = self.view_factory(head)
        mode = st["mode"]
        if witness.has_terminal(view, mode, st["call_id"]):
            self.out("refused closed")
            return 0
        pins = paths.read_pins(view) or {}
        pb = memory.playbook_in_force(view, dt.date.fromisoformat(ctx["model_day"]),
                                      self._pb_mode(view, mode, dt.date.fromisoformat(ctx["model_day"])))
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

    @staticmethod
    def _pb_mode(view, mode, day):
        """Whose playbook is in force: the canary's own versions only for canary model days >= a15_from (R-AN-50,
        §16.18 A15); otherwise the live version, as before."""
        return "canary" if mode == "canary" and paths.a15_applies(paths.read_mode(view), day) else "live"

    # ------------------------------------------------------------ probes (R-AN-71, R-AN-84; C5-C7)
    def run_probes(self, view, head, kind, tag, fetched=None):
        """The R-AN-84 probes against origin (the inbox), as an `analyst-probe/1` record. A canary context bounds the
        set at 120 s. Both kinds of the record also hold `cc_version`, the session's Claude Code version (third
        review of §16.18 A7, decision 22), read after the probes: it is not a probe, so it is in neither `outcomes`
        nor `pass`, and the verdict and the exit code do not depend on it."""
        inbox = (paths.read_pins(view) or {}).get("inbox_repo")
        res = probes.Prober(self.git, self.gh, self.probe_get, self.mono, self.sleep, self.now, inbox, head, kind,
                            tag, direct=self.direct_url, fetched=fetched,
                            budget_s=probes.CANARY_BUDGET_S if kind == "canary" else None).run()
        res["cc_version"] = self._cc_version()
        return res

    def probe(self):
        """`run_window.py probe` (R-AN-84): the probes with no window, ignoring mode and time. It writes the record
        to .analyst_run/probe.json, pushes it as probes/<tag>.json on claude/probe-result-<tag> (one new commit on
        origin/main), and prints `probe_<verdict> ` and the record's compact form."""
        t = self.now()
        tag = "p" + t.strftime("%Y%m%dT%H%M%SZ")
        try:
            head = self.git.fetch_main()
            view = self.view_factory(head)
        except Exception:  # noqa: BLE001
            self.out("error fetch")
            return 1
        res = self.run_probes(view, head, "probe", tag)
        body = rec.record_bytes(res)
        self.work.mkdir(parents=True, exist_ok=True)
        (self.work / "probe.json").write_bytes(body)
        try:
            ok = self._push(head, f"probes/{tag}.json", body, f"claude/probe-result-{tag}",
                            self.now() + dt.timedelta(minutes=3))
        except Exception:  # noqa: BLE001
            ok = False
        self.out(f"probe_{res['verdict']} {rec.compact(res)}")
        if not ok:
            self.out("error probe_push")
            return 1
        return PROBE_EXIT[res["verdict"]]

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
        a15 = paths.a15_from(paths.read_mode(view))
        canary_real = mode == "canary" and a15 is not None and self.now().date() >= a15     # R-AN-95
        if mode == "canary" and not canary_real:
            fx = view.read_json(paths.CANARY_SETTLED_FIXTURE)
            pool = [(fx["call_id"], paths.CANARY_SETTLED_FIXTURE)] if fx else []
        else:
            pre = f"{paths.prefix(mode)}settled/"
            pool = []
            for p in view.list(pre):
                if p.endswith(".json"):
                    try:
                        pool.append((clock.window_key(p[len(pre):-5]), p[len(pre):-5], p))
                    except ValueError:
                        continue
            pool = [(cid, p) for _k, cid, p in sorted(pool)]
        for cid, p in pool:
            if cid in done or view.exists(paths.lesson_path(mode, cid)):
                continue
            n_acc = len(witness.accepted_attempts(view, mode, f"lesson-{cid}"))
            if max(n_acc, pushed.get(cid, 0)) >= 2:
                continue
            try:
                if self.in_flight(view, mode, f"lesson-{cid}"):
                    continue                       # another session's lesson draw, not witnessed yet
            except gitops.GitError:
                self.out("error inbox_branches")
                return 1
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
        pb = memory.playbook_in_force(view, self.now().date(), self._pb_mode(view, mode, self.now().date()))
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
        try:
            if self.in_flight(view, mode, draw):
                self.out("refused concurrent")
                return 0
        except gitops.GitError:
            self.out("error inbox_branches")
            return 1
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
