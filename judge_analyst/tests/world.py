"""A synthetic gold-analyst world for the judge's tests: a git repository with the records the analyst's scripts and
workflows would write (attempts, calls, witnesses, terminals, START, settled, twins, state, ledger, runs exports), a
source store repository (gold-forward-watch stand-in), a GitHub API double and a Dukascopy hour-file double.

It is built from the register's schemas (§16) by the judge's author, not from the analyst code, and it uses the
judge's own functions to fill derived fields, so a clean world must judge SOUND and every tamper must be found.
"""
from __future__ import annotations

import copy
import csv
import datetime as dt
import io
import lzma
import os
import random
import shutil
import struct
import subprocess

from jlib import derive, settle, stats, ticks
from jlib.clock import Holidays, Window, model_to_utc, register_holiday_rows, windows_between
from jlib.common import UTC, ZERO64, compact_form, fmt_utc, record_form, sha256_bytes, sha256_compact
from jlib.context import CONTEXT_KEYS, rebuild_context
from jlib.clock import Events
from jlib.judge import Judge
from jlib.m1 import DAYS_HEADER, DaysTable, Store
from jlib.sources import FakeGitHub, FakeHourFetcher, HourSource

HERE = os.path.dirname(os.path.abspath(__file__))
JUDGE_ROOT = os.path.dirname(HERE)
OWNER = "Mohamedalhaaj"
BOT = "github-actions[bot]"
AGENT = "claude[bot]"
ISSUE = 7
FORECAST_HEADER = "day,slot,origin_model,origin_utc,F_H1,F_S,price,y_bp,nfp,cpi,fomc,hol,computed_utc"
_REC24 = struct.Struct(">iiiiif")
_TICK = struct.Struct(">IIIff")
PLAN_BUY = {"action": "buy", "stop_mult": 1.0, "target_mult": 2.0, "confidence": "low",
            "reason_ar": "خطة تجريبية من بيانات السياق", "key_risks_ar": "تذبذب مرتفع"}
PLAN_SELL = {"action": "sell", "stop_mult": 1.5, "target_mult": 1.5, "confidence": "medium",
             "reason_ar": "خطة تجريبية للبيع", "key_risks_ar": ""}
PLAN_STAY = {"action": "stay_out", "confidence": "high", "reason_ar": "لا ميزة واضحة", "key_risks_ar": ""}
SRC_DATES = dict(GIT_AUTHOR_NAME="world", GIT_AUTHOR_EMAIL="world@example.invalid", GIT_COMMITTER_NAME="world",
                 GIT_COMMITTER_EMAIL="world@example.invalid")


def T(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


class Git:
    def __init__(self, path):
        self.path = path
        os.makedirs(path, exist_ok=True)
        self("init", "-q", "-b", "main")
        self("config", "commit.gpgsign", "false")

    def __call__(self, *args, when: dt.datetime | None = None):
        env = dict(os.environ, **SRC_DATES)
        if when is not None:
            env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when.strftime("%Y-%m-%dT%H:%M:%S+0000")
        p = subprocess.run(["git", "-C", self.path, *args], capture_output=True, env=env)
        if p.returncode != 0:
            raise RuntimeError(f"git {args}: {p.stderr.decode()[:300]}")
        return p.stdout.decode().strip()

    def write(self, rel, data: bytes):
        full = os.path.join(self.path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(data)

    def append(self, rel, data: bytes):
        full = os.path.join(self.path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "ab") as f:
            f.write(data)

    def commit(self, msg, when, paths=None):
        self("add", "-A", *(["--"] + paths if paths else []))
        self("commit", "-q", "--allow-empty", "-m", msg, when=when)
        return self("rev-parse", "HEAD")

    def head(self):
        return self("rev-parse", "HEAD")


def lzma_alone(raw: bytes) -> bytes:
    return lzma.compress(raw, format=lzma.FORMAT_ALONE)


class World:
    """Build with World(tmp).build(); then run_judge() → (findings, verdict)."""

    def __init__(self, root, canary_days=("2026-11-19", "2026-11-20"), live_from="2026-11-23",
                 live_to="2026-12-04", now="2026-12-07T12:00:00Z", plans=None, hooks=None, seed=7):
        self.root = root
        self.canary_days = [dt.date.fromisoformat(x) for x in canary_days]
        self.live_from = dt.date.fromisoformat(live_from)
        self.live_to = dt.date.fromisoformat(live_to)
        self.now = now
        self.hooks = hooks or {}
        self.rng = random.Random(seed)
        self.plans = plans if plans is not None else self.default_plans()
        self.api = {"activity": [], "comments": {str(ISSUE): []}, "runs": {}}
        self.hour_files = {}
        self.seq = 0
        self.run_id = 1000
        self.act_id = 50000
        self.comment_id = 90000
        self.prev_chain = {"live": ZERO64, "canary": ZERO64}
        self.terminals = {}
        self.attempts = {}
        self.settled_done = set()
        self.twins_done = set()
        self.state_events = []
        self.runs_rows = []
        self.start_written = False
        self.events = Events.judge_copies()
        self.holidays = Holidays(None)

    # ------------------------------------------------------------------ outcomes
    def default_plans(self):
        """call_id -> scenario. Unlisted windows of a mode's period are called buys."""
        return {
            "2026-11-19_W1": ("call", PLAN_BUY), "2026-11-19_W2": ("call", PLAN_STAY),
            "2026-11-20_W1": ("call", PLAN_SELL), "2026-11-20_W2": ("missed",),
            "2026-11-23_W1": ("call", PLAN_BUY), "2026-11-23_W2": ("call", PLAN_STAY),
            "2026-11-24_W1": ("missed",), "2026-11-24_W2": ("run_died",),
            "2026-11-25_W1": ("call", PLAN_SELL), "2026-11-25_W2": ("two_attempts", PLAN_BUY),
            "2026-11-26_W1": ("call", PLAN_BUY), "2026-11-27_W1": ("call", PLAN_STAY),
            "2026-11-30_W1": ("call", PLAN_SELL), "2026-11-30_W2": ("call", PLAN_BUY),
            "2026-12-01_W1": ("call", PLAN_BUY), "2026-12-01_W2": ("late", PLAN_SELL),
            "2026-12-02_W1": ("refusal",), "2026-12-02_W2": ("call", PLAN_SELL),
            "2026-12-03_W1": ("data_absent",), "2026-12-03_W2": ("call", PLAN_BUY),
            "2026-12-04_W1": ("call", PLAN_SELL), "2026-12-04_W2": ("call", PLAN_STAY),
        }

    # ------------------------------------------------------------------ build
    def build(self):
        self.repo = Git(os.path.join(self.root, "gold-analyst"))
        self.src = Git(os.path.join(self.root, "gold-forward-watch"))
        first = min(self.canary_days + [self.live_from]) if self.canary_days else self.live_from
        self.store_first = dt.date(2026, 11, 1)
        self.store_last = self.live_to + dt.timedelta(days=1)
        from jlib.gitrepo import GitRepo
        self.gv = GitRepo(self.repo.path)
        self.make_source_store()
        self.make_build_commit(first)
        self.mirrored_until = None
        day = first - dt.timedelta(days=1)
        last = self.live_to + dt.timedelta(days=3)
        while day <= last:
            self.mirror_through(day - dt.timedelta(days=1), T(f"{day.isoformat()}T02:10:00Z"))
            self.settle_run(T(f"{day.isoformat()}T02:15:00Z"))
            if day.weekday() < 5:
                for w in ("W1", "W2"):
                    win = Window(day, w)
                    mode = self.mode_of(day)
                    if mode:
                        self.window(mode, win)
            day += dt.timedelta(days=1)
        self.write_runs_exports()
        if "final" in self.hooks:
            self.hooks["final"](self)
        self.gv.close()
        return self

    def mode_of(self, d):
        if d in self.canary_days:
            return "canary"
        if self.live_from <= d <= self.live_to:
            return "live"
        return None

    # ------------------------------------------------------------------ the source store (gold-forward-watch)
    def make_source_store(self):
        rows = []
        d = dt.date(2026, 9, 1)
        price = 4_000_000
        self.day_rows = {}
        while d <= self.store_last:
            if d < self.store_first:
                for side in ("BID", "ASK"):
                    rows.append([d.isoformat(), side, "missing", "", "", "", "", ""])
            else:
                bid_raw, ask_raw, price, volm = self.make_day(d, price)
                for side, raw in (("BID", bid_raw), ("ASK", ask_raw)):
                    body = lzma_alone(raw)
                    rel = f"data/system/raw/{d.year:04d}/{d.month:02d}/{d.day:02d}/{side}.bi5"
                    self.src.write(rel, body)
                    fetched = fmt_utc(dt.datetime(d.year, d.month, d.day, tzinfo=UTC) + dt.timedelta(hours=26,
                                                                                                    minutes=5))
                    rows.append([d.isoformat(), side, "ok", str(len(body)), sha256_bytes(body), "1440", str(volm),
                                 fetched])
            d += dt.timedelta(days=1)
        self.src_days_rows = rows
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(DAYS_HEADER)
        for r in rows:
            w.writerow(r)
        self.src.write("data/system/days.csv", buf.getvalue().encode())
        # forecasts.csv: slots 0, 9, 10, 15 of every weekday; NFP days flagged
        lines = [FORECAST_HEADER]
        d = dt.date(2026, 10, 1)
        nfp_days = {to_d(t) for t in self.events.by_kind["nfp"]}
        while d <= self.live_to:
            if d.weekday() < 5:
                for slot in (0, 9, 10, 15):
                    fs = round(70 + 20 * self.rng.random(), 6)
                    nfp = "1" if d in nfp_days else "0"
                    origin = f"{d.isoformat()} {slot + 1:02d}:00"
                    lines.append(f"{d.isoformat()},{slot},{origin},{d.isoformat()}T00:00Z,{fs / 3},{fs},4000.0,,"
                                 f"{nfp},0,0,0,{d.isoformat()}T23:30:00Z")
            d += dt.timedelta(days=1)
        self.src_forecasts = ("\n".join(lines) + "\n").encode()
        self.src.write("data/system/forecasts.csv", self.src_forecasts)
        self.src_sha = self.src.commit("system store", T("2026-12-06T00:00:00Z"))

    def make_day(self, d, price):
        base = int(dt.datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())
        weekend = d.weekday() >= 5
        bid = []
        ask = []
        volm = 0
        spread = 250
        for i in range(1440):
            o = price
            if weekend:
                c = o
                h = l = o
                vol = 0.0
            else:
                c = o + int(self.rng.gauss(0, 1300))
                h = max(o, c) + int(abs(self.rng.gauss(0, 300)))
                l = min(o, c) - int(abs(self.rng.gauss(0, 300)))
                vol = 0.0 if (i % 397 == 5) else 0.25
            if vol > 0:
                volm += 1
            bid.append(_REC24.pack(i * 60, o, c, l, h, vol))
            ask.append(_REC24.pack(i * 60, o + spread, c + spread, l + spread, h + spread, vol))
            price = c
        return b"".join(bid), b"".join(ask), price, volm

    # ------------------------------------------------------------------ the build commit
    def make_build_commit(self, first):
        r = self.repo
        hol = io.StringIO()
        w = csv.writer(hol, lineterminator="\n")
        w.writerow(["date", "exchange", "kind", "close_utc"])
        for row in register_holiday_rows():
            w.writerow(row)
        files = {
            "analyst/PARAMS.json": record_form({"T_run_min": 0, "L_min": 50, "T_setup_min": 0, "concurrency_min": 20,
                                                "provisional": True, "canary_report_sha256": None}),
            "analyst/PINS.json": record_form({"repo": f"{OWNER}/gold-analyst", "witness_issue": ISSUE,
                                              "model": "claude-opus-5-5", "effort": "high",
                                              "freeze_tag": "analyst-v1-freeze",
                                              "judge_site_url": "https://example.invalid/judge",
                                              "page_url": "https://example.invalid/analyst/"}),
            "analyst/MODE.json": record_form({"mode": "live", "canary_from": min(self.canary_days).isoformat()
                                              if self.canary_days else None,
                                              "canary_to": max(self.canary_days).isoformat()
                                              if self.canary_days else None,
                                              "live_from": self.live_from.isoformat(),
                                              "set_utc": "2026-11-01T00:00:00Z", "note": "world"}),
            "analyst/tables/holidays.csv": hol.getvalue().encode(),
            "analyst/playbooks/v0.md": "# playbook v0\nلم تثبت أي قاعدة اتجاه على تاريخ الذهب بعد الكلفة\n".encode(),
            "analyst/ROUTINE_PROMPT.md": b"Run the frozen scripts only.\n",
            "analyst/ADJUDICATIONS.csv": b"utc,ref,subject,decision,note\n",
            "analyst/ops_changes.csv": b"utc,file,sha256,head_sha,note\n",
        }
        with open(os.path.join(HERE, "fixtures", "eproc.py"), "rb") as f:
            files["analyst/vendor/eproc.py"] = f.read()
        for name in ("bls_2026_2027.csv", "fomc_2026_2027.csv"):
            with open(os.path.join(JUDGE_ROOT, "calendars", name), "rb") as f:
                files[f"analyst/calendars/{name}"] = f.read()
        for k, v in files.items():
            r.write(k, v)
        man = "".join(f"{sha256_bytes(files[p])}  {p}\n" for p in sorted(files)
                      if p not in ("analyst/MODE.json", "analyst/ADJUDICATIONS.csv", "analyst/ops_changes.csv"))
        r.write("analyst/FROZEN_ANALYST.sha256", man.encode())
        self.manifest_sha = sha256_bytes(man.encode())
        self.prompt_sha = sha256_bytes(files["analyst/ROUTINE_PROMPT.md"])
        if "build" in self.hooks:
            self.hooks["build"](self)
        r.commit("analyst build", T(f"{(first - dt.timedelta(days=3)).isoformat()}T09:00:00Z"))

    # ------------------------------------------------------------------ mirror and settle runs
    def mirror_through(self, day, when):
        if day < self.store_first:
            day = self.store_first - dt.timedelta(days=1)
        if self.mirrored_until is not None and day <= self.mirrored_until:
            return
        r = self.repo
        rows = [x for x in self.src_days_rows if x[0] <= day.isoformat()]
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(DAYS_HEADER)
        for x in rows:
            w.writerow(x)
        r.write("mirror/system/days.csv", buf.getvalue().encode())
        for x in rows:
            if x[2] == "ok":
                d = dt.date.fromisoformat(x[0])
                rel = f"raw/{d.year:04d}/{d.month:02d}/{d.day:02d}/{x[1]}.bi5"
                with open(os.path.join(self.src.path, "data/system", rel), "rb") as f:
                    r.write("mirror/system/" + rel, f.read())
        lines = self.src_forecasts.decode().split("\n")
        keep = [lines[0]] + [ln for ln in lines[1:] if ln and ln.split(",")[1] in ("9", "10", "15") and
                             ln.split(",")[0] <= day.isoformat()]
        r.write("mirror/system/forecasts_slots.csv", ("\n".join(keep) + "\n").encode())
        exp_line = f"{fmt_utc(when)},{self.src_sha},{sha256_bytes(buf.getvalue().encode())},x,y,0,0,{self.next_run()}\n"
        if not os.path.exists(os.path.join(r.path, "mirror/exports.csv")):
            r.write("mirror/exports.csv", b"utc,source_sha,days_sha256,forecasts_sha256,slots_sha256,raw_files,"
                                          b"new_raw_files,run_id\n")
        r.append("mirror/exports.csv", exp_line.encode())
        sha = r.commit(f"analyst mirror {fmt_utc(when)}", when)
        self.activity("refs/heads/main", sha, when, OWNER, "push")
        self.mirrored_until = day

    def next_run(self):
        self.run_id += 1
        return self.run_id

    def activity(self, ref, after, when, actor, typ):
        self.act_id += 1
        self.api["activity"].append({"id": self.act_id, "before": "0" * 40, "after": after, "ref": ref,
                                     "timestamp": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "activity_type": typ,
                                     "actor": {"login": actor}})
        return self.act_id

    def new_run(self, workflow, created, event="push"):
        rid = self.next_run()
        self.api["runs"][str(rid)] = {"id": rid, "run_attempt": 1, "event": event,
                                      "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"), "name": workflow,
                                      "path": f".github/workflows/{workflow}.yml"}
        return {"workflow": workflow, "id": rid, "attempt": 1, "event": event, "created_utc": fmt_utc(created)}

    # ------------------------------------------------------------------ agent pushes and merge
    def agent_push(self, mode, branch, path, data: bytes, when):
        """A session push: one new commit on a new claude/ branch based on main."""
        r = self.repo
        base = r.head()
        r("switch", "-q", "-c", branch, base)
        r.write(path, data)
        extra = self.hooks.get("extra_push_file", {}).get(path)
        if extra:
            r.write(extra[0], extra[1])
        sha = r.commit(f"session push {path}", when)
        r("switch", "-q", "main")
        aid = self.activity(f"refs/heads/{branch}", sha, when, AGENT, "branch_creation")
        return sha, aid

    def witness(self, mode, kind, wid, status, reason, path, file_sha, push, run, window, terminal, when):
        self.seq += 1
        w = {"schema": "analyst-witness/1", "seq": self.seq, "mode": mode, "kind": kind, "id": wid, "status": status,
             "reason": reason, "path": path, "file_sha256": file_sha, "push": push, "run": run, "window": window,
             "terminal": terminal, "written_utc": fmt_utc(when)}
        if "witness" in self.hooks:
            self.hooks["witness"](self, w)
        body = Judge.comment_body(dict(w, comment=None))
        self.comment_id += 1
        created = when + dt.timedelta(seconds=5)
        self.api["comments"][str(ISSUE)].append({"id": self.comment_id, "body": body,
                                                 "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                                 "user": {"login": BOT}})
        w["comment"] = {"issue": ISSUE, "id": self.comment_id, "created_utc": fmt_utc(created),
                        "body_sha256": sha256_bytes(body.encode())}
        pre = "analyst/" if mode == "live" else "analyst/canary/"
        self.repo.write(f"{pre}witness/{self.seq:06d}.json", record_form(w))
        return w

    def merge_push(self, mode, kind, wid, path, data, sha, aid, push_time, win, branch):
        run = self.new_run("analyst-merge", push_time + dt.timedelta(seconds=20))
        push = {"ref": f"refs/heads/{branch}", "before": "0" * 40, "after": sha, "activity_id": aid,
                "activity_type": "branch_creation", "activity_utc": fmt_utc(push_time), "actor": AGENT}
        window = None
        if win is not None:
            void = push_time < win.span_lo or push_time > win.deadline
            window = {"entry_utc": fmt_utc(win.entry), "deadline_utc": fmt_utc(win.deadline),
                      "time_utc": fmt_utc(push_time), "time_source": "activity", "void": void}
        self.repo.write(path, data)
        w = self.witness(mode, kind, wid, "accepted", None, path, sha256_bytes(data), push, run, window, None,
                         push_time + dt.timedelta(seconds=40))
        return w, run

    def write_terminal(self, mode, win, status, detail, writer, call_block, attempts, when, run):
        pre = "analyst/" if mode == "live" else "analyst/canary/"
        t = {"schema": "analyst-terminal/1", "call_id": win.call_id, "mode": mode, "window": win.w,
             "model_day": win.day.isoformat(), "entry_utc": fmt_utc(win.entry), "deadline_utc": fmt_utc(win.deadline),
             "status": status, "detail": detail, "writer": writer, "call": call_block,
             "attempts": attempts, "prev_chain_sha256": self.prev_chain[mode], "written_utc": fmt_utc(when),
             "run_id": run["id"], "head_sha": self.repo.head()}
        if "terminal" in self.hooks:
            self.hooks["terminal"](self, t)
        b = record_form(t)
        path = f"{pre}terminal/{win.call_id}.json"
        self.repo.write(path, b)
        tsha = sha256_bytes(b)
        self.witness(mode, "terminal", win.call_id, "accepted", None, path, tsha, None, run, None,
                     {"status": status, "detail": detail, "terminal_sha256": tsha,
                      "prev_chain_sha256": t["prev_chain_sha256"],
                      "call_sha256": call_block["sha256"] if call_block else None,
                      "push_sha": call_block["push_sha"] if call_block else None}, when)
        self.prev_chain[mode] = tsha
        self.terminals[(mode, win.call_id)] = {"status": status, "detail": detail, "bytes": b, "path": path,
                                               "lock": call_block["lock_utc"] if call_block else None}
        if mode == "live" and status in ("called", "stay_out") and not self.start_written:
            d0 = win.day
            m = d0 + dt.timedelta(days=(7 - d0.weekday()) % 7 or 7)
            if d0.weekday() >= 3:
                m += dt.timedelta(days=7)
            st = {"schema": "analyst-start/1", "freeze_tag": "analyst-v1-freeze", "first_call_id": win.call_id,
                  "d0": d0.isoformat(), "first_call_lock_utc": call_block["lock_utc"], "first_day": m.isoformat(),
                  "rule": "first Monday m > d0 with numpy.busday_count(d0, m) >= 3", "terminal_sha256": tsha,
                  "written_utc": fmt_utc(when), "run_id": run["id"], "head_sha": self.repo.head()}
            if "start" in self.hooks:
                self.hooks["start"](self, st)
            self.repo.write("analyst/START.json", record_form(st))
            self.start_written = True
            self.first_day = m

    # ------------------------------------------------------------------ one window
    def window(self, mode, win):
        scen = self.plans.get(win.call_id, ("call", PLAN_BUY))
        if self.holidays.window_holiday(win):
            scen = ("holiday",)
        pre = "analyst/" if mode == "live" else "analyst/canary/"
        w1 = win.w == "W1"
        fire = win.entry - dt.timedelta(minutes=40)
        kind = scen[0]
        attempts = []
        if kind == "holiday" or kind == "missed":
            self.close(mode, win, "skipped:holiday" if kind == "holiday" else "missed:trigger", attempts)
            return
        times = [fire + dt.timedelta(seconds=90)]
        if kind == "two_attempts":
            times.append(win.deadline - dt.timedelta(minutes=3))
        session = f"session-{win.call_id}-{mode}"
        for n, at in enumerate(times, start=1):
            self.runs_rows.append({"routine": "analyst-w1" if w1 else "analyst-w2", "run_id": f"r-{win.call_id}-{n}",
                                   "session_id": f"{session}-{n}", "created_utc": fmt_utc(at - dt.timedelta(minutes=1)),
                                   "status": "success", "model": "claude-opus-5-5",
                                   "model_source": "get_run_log"})
            rec = {"schema": "analyst-attempt/1", "kind": "call", "mode": mode, "draw_id": win.call_id, "n": n,
                   "call_id": win.call_id, "repo_head_sha": self.repo.head(), "manifest_sha256": self.manifest_sha,
                   "code_sha8": self.manifest_sha[:8], "routine_prompt_sha256": self.prompt_sha,
                   "self_reported": {"started_utc": fmt_utc(at - dt.timedelta(seconds=20)),
                                     "session_id": f"{session}-{n}", "model": "claude-opus-5-5",
                                     "cc_version": "2.9.0", "note": "self-reported, unverified"}}
            data = record_form(rec)
            path = f"{pre}attempts/{win.call_id}_{n}.json"
            branch = f"claude/analyst-{mode}-attempt-{win.call_id}-{n}"
            push_time = self.hooks.get("attempt_time", {}).get((win.call_id, n), at)
            sha, aid = self.agent_push(mode, branch, path, data, push_time)
            w, run = self.merge_push(mode, "attempt", win.call_id, path, data, sha, aid, push_time, win, branch)
            self.repo.commit(f"analyst-merge attempt {win.call_id}_{n} seq {w['seq']}", push_time +
                             dt.timedelta(seconds=50))
            attempts.append({"n": n, "path": path, "sha256": sha256_bytes(data), "time_utc": fmt_utc(push_time),
                             "void": w["window"]["void"], "witness_seq": w["seq"]})
        if kind == "run_died":
            self.close(mode, win, "no_call:run", attempts)
            return
        n = len(times)
        asof = times[-1] + dt.timedelta(seconds=30)
        R = self.repo.head()
        ctx = self.build_context(mode, win, asof, R, hour_absent=(kind == "data_absent"))
        if "context" in self.hooks:
            self.hooks["context"](self, win, ctx)
        plan = None
        status = ctx["status"]
        if status == "ok":
            if kind == "refusal":
                status = "no_call:refusal"
            else:
                plan = copy.deepcopy(scen[1])
                status = "called" if plan["action"] in ("buy", "sell") else "stay_out"
        call = self.make_call(mode, win, ctx, plan, status, n, session + f"-{n}", asof)
        if "call" in self.hooks:
            self.hooks["call"](self, win, call)
        data = record_form(call)
        path = f"{pre}calls/{win.call_id}.json"
        lock_time = asof + dt.timedelta(minutes=2)
        if kind == "late":
            lock_time = win.deadline + dt.timedelta(minutes=5)
        lock_time = self.hooks.get("lock_time", {}).get(win.call_id, lock_time)
        branch = f"claude/analyst-{mode}-call-{win.call_id}"
        sha, aid = self.agent_push(mode, branch, path, data, lock_time)
        w, run = self.merge_push(mode, "call", win.call_id, path, data, sha, aid, lock_time, win, branch)
        if lock_time > win.deadline:
            tstatus, tdetail = "no_call:late", None
        else:
            tstatus, tdetail = call["status"], call["detail"]
        call_block = {"path": path, "sha256": sha256_bytes(data), "plan_status": call["status"],
                      "lock_utc": fmt_utc(lock_time), "time_source": "activity", "push_sha": sha,
                      "witness_seq": w["seq"]}
        when = lock_time + dt.timedelta(seconds=45)
        self.write_terminal(mode, win, tstatus, tdetail, "merge", call_block, attempts, when, run)
        self.repo.commit(f"analyst-merge call {win.call_id} seq {w['seq']}", when + dt.timedelta(seconds=5))

    def close(self, mode, win, status, attempts):
        when = win.deadline + dt.timedelta(minutes=25)
        run = self.new_run("analyst-settle", when - dt.timedelta(seconds=30), event="schedule")
        if "skip_close" in self.hooks and win.call_id in self.hooks["skip_close"]:
            return
        self.write_terminal(mode, win, status, None, "closer", None, attempts, when, run)
        self.repo.commit(f"analyst-settle closer {win.call_id}", when + dt.timedelta(seconds=5))

    def build_context(self, mode, win, asof, R, hour_absent=False):
        hs = HourSource(FakeHourFetcher(self.hour_files), None, fmt_utc(asof))
        rec0 = {"asof_utc": fmt_utc(asof), "repo_head_sha": R, "files": []}
        rb0 = rebuild_context(rec0, win.call_id, mode, self.gv, hs, self.holidays, self.events)
        seq = rb0.expected["_file_seq"]
        files = []
        for i, (sym, hour_s) in enumerate(seq):
            hour = T(hour_s)
            url = ticks.hour_url(sym, hour)
            if hour_absent and sym == "XAUUSD" and i == 1:
                self.hour_files[url] = (404, b"")
                files.append({"sym": sym, "hour_utc": hour_s, "url": url, "http": 404, "bytes": 0, "sha256": "",
                              "ticks": 0, "status": "empty", "fetched_utc": fmt_utc(asof + dt.timedelta(seconds=i)),
                              "tries": 1})
                continue
            body, n = self.tick_file(sym, hour)
            self.hour_files[url] = (200, body)
            files.append({"sym": sym, "hour_utc": hour_s, "url": url, "http": 200, "bytes": len(body),
                          "sha256": sha256_bytes(body), "ticks": n, "status": "present",
                          "fetched_utc": fmt_utc(asof + dt.timedelta(seconds=i)), "tries": 1})
        if "files" in self.hooks:
            self.hooks["files"](self, win, files)
        rec = {"asof_utc": fmt_utc(asof), "repo_head_sha": R, "files": files}
        rb = rebuild_context(rec, win.call_id, mode, self.gv, hs, self.holidays, self.events)
        e = rb.expected
        ctx = {k: e[k] for k in CONTEXT_KEYS if k in e and k != "files"}
        ctx["files"] = files
        if e["status"] == "no_call:data":
            ctx["detail"] = e["detail"][0]
            for k in ("bars_h1", "bars_d1", "p_ref", "fx", "d_ref", "memory"):
                ctx.setdefault(k, None)
            ctx["d_ref"] = None
        return _strip(ctx)

    def tick_file(self, sym, hour):
        key = f"{sym}|{fmt_utc(hour)}"
        rng = random.Random(int(sha256_bytes(key.encode())[:12], 16))
        n = 8 + rng.randrange(8)
        base = {"XAUUSD": 4_000_000, "EURUSD": 108_000, "USDJPY": 150_000, "GBPUSD": 127_000, "USDCAD": 137_000,
                "USDCHF": 88_000}[sym]
        p = base + rng.randrange(-20000, 20000) // (1 if sym == "XAUUSD" else 100)
        ms = sorted(rng.randrange(0, 3_600_000) for _ in range(n))
        raw = b""
        for m in ms:
            p += rng.randrange(-300, 301) // (1 if sym == "XAUUSD" else 30)
            spread = 200 if sym == "XAUUSD" else 2
            raw += _TICK.pack(m, p + spread, p, 0.5, 0.5)
        return lzma_alone(raw), n

    def make_call(self, mode, win, ctx, plan, status, n, session, asof):
        dref = ctx.get("d_ref")
        side = derive.side_of(plan["action"]) if plan else (0 if status == "stay_out" else None)
        sm = tm = S = Tt = sd = td = None
        if plan and plan["action"] in ("buy", "sell"):
            sm, tm = float(plan["stop_mult"]), float(plan["target_mult"])
            sd, td = derive.st_dimes(dref["D_ref_cents"], sm, tm)
            S, Tt = sd / 10, td / 10
        if status == "stay_out":
            side = 0
        if status not in ("called", "stay_out"):
            side = None
        return {"schema": "analyst-call/1", "call_id": win.call_id, "mode": mode, "window": win.w,
                "model_day": win.day.isoformat(), "entry_utc": fmt_utc(win.entry), "exit_utc": fmt_utc(win.exit),
                "deadline_utc": fmt_utc(win.deadline), "status": status,
                "detail": ctx.get("detail") if status.startswith("no_call:d") else None, "attempt_n": n,
                "decision_utc": ctx["asof_utc"], "context_sha256": sha256_compact(ctx), "context": ctx,
                "playbook": {"version": 0, "sha256": None}, "plan": plan,
                "plan_sha256": sha256_bytes(compact_form(plan).encode()) if plan else None, "d_ref": dref,
                "side": side, "stop_mult": sm, "target_mult": tm, "S": S, "T": Tt, "S_dimes": sd, "T_dimes": td,
                "lock_validation_failures": 0,
                "request": {"runner": "claude-code-routine", "model_set": "claude-opus-5-5", "effort_set": "high"},
                "frozen": {"manifest_sha256": self.manifest_sha, "code_sha8": self.manifest_sha[:8],
                           "routine_prompt_sha256": self.prompt_sha,
                           "prompts_sha256": {"system": "0" * 64, "lesson": "0" * 64, "revision": "0" * 64},
                           "schema_sha256": "0" * 64},
                "repo_head_sha": ctx["repo_head_sha"],
                "self_reported": {"locked_utc": fmt_utc(asof), "session_id": session, "model": "claude-opus-5-5",
                                  "cc_version": "2.9.0", "note": "self-reported, unverified"}}

    # ------------------------------------------------------------------ settlement runs
    def settle_run(self, when):
        r = self.repo
        days_b = open(os.path.join(r.path, "mirror/system/days.csv"), "rb").read() \
            if os.path.exists(os.path.join(r.path, "mirror/system/days.csv")) else None
        if days_b is None:
            return
        days = DaysTable(days_b)
        store = Store(days, lambda p: _read(r.path, p))
        changed = False
        run = None
        wins = [w for w in windows_between(self.live_from, self.live_to)]
        processed_days = set()
        for win in wins:
            key = ("live", win.call_id)
            if key not in self.terminals:
                break
            two = (win.day - dt.timedelta(days=1), win.day)
            if not all(days.row(x, s) and days.row(x, s)["status"] in ("ok", "missing") for x in two
                       for s in ("BID", "ASK")) or when < win.exit:
                break
            for x in two:
                store.load_day(x)
            t = self.terminals[key]
            if run is None:
                run = self.new_run("analyst-settle", when, event="push")
            mins = settle.Minutes(store, win)
            void = settle.is_void(days, win.day)
            if t["status"] == "called" and win.call_id not in self.settled_done:
                call = _json(r.path, f"analyst/calls/{win.call_id}.json")
                s = call["side"]
                out = settle.settle_side(mins, s, call["S_dimes"], call["T_dimes"], void)
                opp = settle.settle_side(mins, -s, call["S_dimes"], call["T_dimes"], void)
                cls = "counted" if (self.start_written and win.day >= self.first_day) else "shakedown"
                plan = call["plan"]
                rec = {"schema": "analyst-settled/1", "call_id": win.call_id, "mode": "live", "window": win.w,
                       "model_day": win.day.isoformat(), "class": cls, "action": plan["action"], "side": s,
                       "stop_mult": call["stop_mult"], "target_mult": call["target_mult"],
                       "D_ref": call["d_ref"]["D_ref"], "S": call["S"], "T": call["T"], "S_dimes": call["S_dimes"],
                       "T_dimes": call["T_dimes"], "entry_utc": fmt_utc(win.entry), "exit_utc": fmt_utc(win.exit),
                       "outcome": settle.public(out), "opposite": settle.public(opp),
                       "path_summary": settle.path_summary(mins, s, out),
                       "call": {k: plan.get(k) for k in ("action", "stop_mult", "target_mult", "confidence",
                                                         "reason_ar", "key_risks_ar")},
                       "context": call["context"],
                       "inputs": {"days": [{"day": x.isoformat(), "side": sd, "status": days.row(x, sd)["status"],
                                            "sha256": days.row(x, sd)["sha256"] or None}
                                           for x in two for sd in ("BID", "ASK")],
                                  "days_csv_sha256": sha256_bytes(days_b)},
                       "call_sha256": sha256_bytes(_read(r.path, f"analyst/calls/{win.call_id}.json")),
                       "terminal_sha256": sha256_bytes(t["bytes"]), "context_sha256": call["context_sha256"],
                       "settled_utc": fmt_utc(when), "run_id": run["id"], "head_sha": r.head()}
                if "settled" in self.hooks:
                    self.hooks["settled"](self, win, rec)
                r.write(f"analyst/settled/{win.call_id}.json", record_form(rec))
                self.settled_done.add(win.call_id)
                changed = True
            if t["status"] != "skipped:holiday" and win.call_id not in self.twins_done:
                me, mes, mx, mxs, dW, st = settle.twin(mins, void)
                tw = {"schema": "analyst-twin/1", "call_id": win.call_id, "mode": "live", "window": win.w,
                      "model_day": win.day.isoformat(), "terminal_status": t["status"], "entry_utc": fmt_utc(win.entry),
                      "exit_utc": fmt_utc(win.exit), "mid_entry": me, "mid_entry_src": mes, "mid_exit": mx,
                      "mid_exit_src": mxs, "d_W": dW, "status": st, "settled_utc": fmt_utc(when), "run_id": run["id"],
                      "head_sha": r.head()}
                r.write(f"analyst/twins/{win.call_id}.json", record_form(tw))
                self.twins_done.add(win.call_id)
                changed = True
        if changed:
            self.write_state(when, run)
            r.commit(f"analyst-settle {fmt_utc(when)}", when + dt.timedelta(seconds=30))

    def write_state(self, when, run):
        r = self.repo
        days = DaysTable(_read(r.path, "mirror/system/days.csv"))
        store = Store(days, lambda p: _read(r.path, p))
        trades, twins, rows = [], [], []
        by_day = {}
        for win in windows_between(self.live_from, self.live_to):
            t = self.terminals.get(("live", win.call_id))
            if t is None:
                continue
            by_day.setdefault(win.day, []).append(win)
            row = {"call_id": win.call_id, "terminal_status": t["status"]}
            if win.call_id in self.settled_done:
                s = _json(r.path, f"analyst/settled/{win.call_id}.json")
                o = s["outcome"]
                row.update(o)
                row["class"] = s["class"]
                if s["class"] == "counted":
                    trades.append({"call_id": win.call_id, "model_day": win.day, "w": win.w, "side": s["side"],
                                   "y": o["y"], "R": o["R"], "net": o["net_usd"], "stress": o["stress_net_usd"],
                                   "cost": o["cost_usd"]})
            if win.call_id in self.twins_done:
                tw = _json(r.path, f"analyst/twins/{win.call_id}.json")
                row["d_W"] = tw["d_W"]
                if tw["status"] == "final":
                    twins.append((win.day, win.w, tw["d_W"]))
            rows.append(row)
        processed = set()
        for d, wl in by_day.items():
            if len(wl) == 2 and all((self.terminals[("live", w.call_id)]["status"] == "skipped:holiday" or
                                     w.call_id in self.twins_done) and
                                    (self.terminals[("live", w.call_id)]["status"] != "called" or
                                     w.call_id in self.settled_done) for w in wl):
                processed.add(d)
        fd = self.first_day if self.start_written else None
        replay = stats.replay_states(trades, twins, fd, processed)
        for ev in replay[len(self.state_events):]:
            self.seq_state = len(self.state_events) + 1
            e, reason, d, n, E, K = ev
            rec = {"schema": "analyst-state-event/1", "seq": self.seq_state, "event": e, "reason": reason,
                   "model_day": d.isoformat(), "n": n, "E": E, "K": K, "utc": fmt_utc(when), "run_id": run["id"],
                   "head_sha": r.head()}
            r.write(f"analyst/state/{self.seq_state:06d}.json", record_form(rec))
            self.state_events.append(ev)
        lpd = max(processed).isoformat() if processed else None
        upto = [t for t in trades if lpd and t["model_day"].isoformat() <= lpd]
        epath = stats.e_path([t["y"] for t in upto])
        share = (max(sum(1 for t in upto if t["side"] == 1), sum(1 for t in upto if t["side"] == -1)) / len(upto)
                 if upto else None)
        last40 = [x for x in rows if x["terminal_status"] != "skipped:holiday"][-40:]
        ms = (sum(1 for x in last40 if x["terminal_status"].startswith(("missed:", "no_call:"))) / len(last40)
              if last40 else 0.0)
        state = self.state_events[-1][0] if self.state_events else "SHAKEDOWN"
        S = {"schema": "analyst-state/1", "asof_utc": fmt_utc(when), "state": state, "calls_enabled": True,
             "pause_reasons": [], "paused_display": ms > 0.15, "missed_share_40": ms, "n": len(upto),
             "E": epath[-1] if epath else 1.0, "E_star": 400.0, "K": 1.0, "first_day": fd.isoformat() if fd else None,
             "last_processed_day": lpd, "side_share": share, "side_label": bool(share and share > 0.85),
             "judge_verdict": {"value": None, "asof_utc": None, "fetched": False},
             "last_event_seq": len(self.state_events), "run_id": run["id"], "head_sha": r.head()}
        if "state" in self.hooks:
            self.hooks["state"](self, S)
        r.write("analyst/STATE.json", record_form(S))
        # ledger
        tno = {t["call_id"]: (i + 1, e) for i, (t, e) in enumerate(zip(upto, epath))}
        cols = ["call_id", "model_day", "window", "class", "terminal_status", "status", "detail", "trade_no", "E", "y",
                "net_usd", "d_W", "terminal_sha256"]
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(cols)
        for x in rows:
            win = Window(*_cid(x["call_id"]))
            t = self.terminals[("live", x["call_id"])]
            i_e = tno.get(x["call_id"])
            w.writerow([x["call_id"], win.day.isoformat(), win.w, x.get("class", ""), t["status"], t["status"],
                        t["detail"] or "", i_e[0] if i_e else "", repr(i_e[1]) if i_e else "",
                        repr(x["y"]) if "y" in x and i_e else "", repr(x["net_usd"]) if "net_usd" in x else "",
                        repr(x["d_W"]) if x.get("d_W") is not None else "", sha256_bytes(t["bytes"])])
        data = buf.getvalue().encode()
        if "ledger" in self.hooks:
            data = self.hooks["ledger"](self, data)
        r.write("analyst/ledger.csv", data)

    # ------------------------------------------------------------------ owner exports
    def write_runs_exports(self):
        weeks = {}
        for x in self.runs_rows:
            d = T(x["created_utc"]).date()
            y, wk, _ = d.isocalendar()
            weeks.setdefault(f"{y}-W{wk:02d}", []).append(x)
        if "runs" in self.hooks:
            self.hooks["runs"](self, weeks)
        for wk, rows in sorted(weeks.items()):
            rec = {"schema": "analyst-runs/1", "iso_week": wk, "exported_utc": "2026-12-07T09:00:00Z",
                   "attested_by": "owner",
                   "routines": [{"name": "analyst-w1", "trigger_id": "t1", "model": "claude-opus-5-5",
                                 "effort": "high", "crons": ["20 6,7 * * 1-5"], "enabled": True},
                                {"name": "analyst-w2", "trigger_id": "t2", "model": "claude-opus-5-5",
                                 "effort": "high", "crons": ["50 12,13 * * 1-5"], "enabled": True}],
                   "runs": rows}
            self.repo.write(f"analyst/runs/{wk}.json", record_form(rec))
        self.repo.commit("owner runs export", T("2026-12-07T09:00:00Z"))

    # ------------------------------------------------------------------ judging
    def judge(self, out_dir=None, api=None, hours=None, store=True, now=None, prev_anchor=None, evidence=None):
        from jlib.gitrepo import GitRepo
        git = GitRepo(self.repo.path)
        hs = hours or HourSource(FakeHourFetcher(self.hour_files), os.path.join(out_dir, "hours.csv")
                                 if out_dir else None, now or self.now)
        sg = GitRepo(self.src.path) if store else None
        j = Judge(git, now or self.now, api=api if api is not None else FakeGitHub(copy.deepcopy(self.api)),
                  hours=hs, store_git=sg, evidence_rows=evidence or [], prev_anchor=prev_anchor)
        try:
            return j.run()
        finally:
            git.close()
            if sg is not None:
                sg.close()

    def clone(self, dest):
        """A copy of the built world (repositories and doubles) for a post-hoc tamper."""
        shutil.copytree(self.root, dest)
        w = copy.copy(self)
        w.root = dest
        w.repo = _Rebind(os.path.join(dest, "gold-analyst"))
        w.src = _Rebind(os.path.join(dest, "gold-forward-watch"))
        w.api = copy.deepcopy(self.api)
        w.hour_files = dict(self.hour_files)
        return w


class _Rebind(Git):
    def __init__(self, path):  # noqa: D401 - an existing repository
        self.path = path


def _strip(x):
    if isinstance(x, dict):
        return {k: _strip(v) for k, v in x.items() if not k.startswith("_")}
    if isinstance(x, list):
        return [_strip(v) for v in x]
    return x


def _read(root, rel):
    p = os.path.join(root, rel)
    if not os.path.exists(p):
        return None
    with open(p, "rb") as f:
        return f.read()


def _json(root, rel):
    import json
    return json.loads(_read(root, rel).decode())


def _cid(cid):
    d, w = cid.split("_")
    return dt.date.fromisoformat(d), w


def to_d(t):
    from jlib.clock import to_server
    return to_server(t).date()
