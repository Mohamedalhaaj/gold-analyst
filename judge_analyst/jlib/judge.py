"""The analyst judge's checks A0-A11 (prereg §8 as amended by v0.5 M7; IMPLEMENTATION_REGISTER R-AN-66..R-AN-70).

A0 is the judge's own input check (a shallow clone, an unreachable API, an unreadable source store or hour file):
WARNING, never SOUND, never latched. A1-A11 follow R-AN-67's table.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import os
import re
from dataclasses import dataclass, field

from . import derive, settle, stats
from .clock import Events, Holidays, Window, add_months_12, iso_week, parse_call_id, windows_between
from .common import (UTC, ZERO64, Findings, compact_form, fmt_utc, load_json_bytes, norm_api_time, num_exact,
                     parse_utc, price_eq, record_form, rel_eq, safe_detail, sha256_bytes, sha256_compact)
from .context import compare_context, rebuild_context
from .m1 import DaysTable, Store

EPROC_SHA256 = "ee8d456ae3b5ddd1112bad92d12fffa95d2b12e0559335233c6a9b3a68067973"
BOT_LOGIN = "github-actions[bot]"
PREFIX = {"live": "analyst/", "canary": "analyst/canary/"}
CALL_ID_RE = r"[0-9]{4}-[0-9]{2}-[0-9]{2}_W[12]"
TERMINAL_STATUSES = {"called", "stay_out", "no_call:refusal", "no_call:invalid", "no_call:data", "no_call:d_ref",
                     "no_call:late", "no_call:run", "missed:trigger", "missed:push", "skipped:holiday"}
CALL_STATUSES = {"called", "stay_out", "no_call:data", "no_call:d_ref", "no_call:invalid", "no_call:refusal"}
WITNESS_VIOLATION = {"agent_scope", "rewrite", "invalid_content", "attempt_cap"}
WITNESS_WARNING = {"mode", "attempt_n", "window_closed", "no_settled", "lesson_cap", "not_saturday", "incomplete_week",
                   "version", "week_done"}
H = dt.timedelta(hours=1)
COMPLETENESS_GRACE = dt.timedelta(hours=36)   # judge decision J-D3: after deadline + 20 min
COMMENT_GRACE = dt.timedelta(hours=24)
PUSH_GRACE = dt.timedelta(hours=24)
SETTLE_GRACE = dt.timedelta(hours=36)

# Path classes (R-AN-2, R-AN-59).
_CREATE_ONLY = re.compile(
    r"^analyst/(canary/)?(attempts|calls|lessons|witness|terminal|settled|twins|revisions|state)/[^/]+$"
    r"|^analyst/(canary/)?playbooks/v[1-9][0-9]*\.md$"
    r"|^analyst/(runs|usage)/[^/]+\.json$"
    r"|^analyst/canary/checks/[^/]+$")
_WRITE_ONCE = re.compile(r"^analyst/START(_[^/]+)?\.json$|^mirror/system/raw/.+\.bi5$")
APPEND_ONLY = {"mirror/system/days.csv", "mirror/system/forecasts_slots.csv", "mirror/exports.csv",
               "analyst/ADJUDICATIONS.csv", "analyst/ops_changes.csv", "reports/judge_analyst/evidence.csv",
               "reports/judge_analyst/FINDINGS.csv", "reports/judge_analyst/VERDICTS.csv",
               "reports/judge_analyst/ADJUDICATIONS.csv", "reports/judge_analyst/hours.csv"}

EVIDENCE_HEADER = ["judge_utc", "source", "ref", "push_after", "activity_id", "activity_type", "activity_utc", "actor",
                   "run_id", "run_attempt", "run_event", "run_created_utc", "comment_id", "comment_created_utc",
                   "comment_body_sha256"]


def allow_patterns(prefix: str):
    p = re.escape(prefix)
    return {
        "attempt": re.compile(rf"^{p}attempts/(({CALL_ID_RE})|lesson-({CALL_ID_RE})|revision-v[1-9][0-9]*)_[12]\.json$"),
        "call": re.compile(rf"^{p}calls/{CALL_ID_RE}\.json$"),
        "lesson": re.compile(rf"^{p}lessons/{CALL_ID_RE}\.json$"),
        "revision": re.compile(rf"^{p}playbooks/v[1-9][0-9]*\.md$"),
    }


def classify_path(path: str):
    """(mode, kind) of an allowlisted agent path, or None (R-AN-33)."""
    if path.startswith(".github/"):
        return None
    for mode in ("canary", "live"):
        for kind, rx in allow_patterns(PREFIX[mode]).items():
            if rx.match(path):
                return mode, kind
    return None


def diff_conforms(changes) -> bool:
    return len(changes) == 1 and changes[0][0] == "A" and classify_path(changes[0][1]) is not None


@dataclass
class Rec:
    path: str
    raw: bytes
    rec: object
    commit: str = "-"


@dataclass
class Result:
    findings: Findings
    head: str
    evidence_new: list = field(default_factory=list)
    summary: dict = field(default_factory=dict)


class Judge:
    def __init__(self, git, now_utc: str, api=None, hours=None, store_git=None, fetch_commit=None,
                 evidence_rows=None, prev_anchor=None, judge_manifest_ok=True, events=None):
        self.git = git
        self.now_s = now_utc
        self.now = parse_utc(now_utc)
        self.api = api
        self.hours = hours
        self.store_git = store_git
        self.fetch_commit = fetch_commit
        self.evidence_rows = evidence_rows or []
        self.prev_anchor = prev_anchor
        self.judge_manifest_ok = judge_manifest_ok
        self.events = events or Events.judge_copies()
        self.F = Findings()
        self.summary = {}
        self.evidence_new = []

    # ================================================================ helpers
    def bytes_at_head(self, path):
        return self.git.show(self.head, path)

    def json_at_head(self, path):
        b = self.bytes_at_head(path)
        if b is None:
            return None
        try:
            return load_json_bytes(b)
        except Exception:  # noqa: BLE001
            return None

    def records(self, folder: str):
        out = {}
        for p in self.git.ls_tree(self.head, folder):
            if p[len(folder):].count("/"):
                continue
            b = self.bytes_at_head(p)
            try:
                r = load_json_bytes(b) if p.endswith(".json") else None
            except Exception:  # noqa: BLE001
                r = None
            out[p] = Rec(p, b, r, self.first_add.get(p, "-"))
        return out

    def commit_of(self, path):
        return self.first_add.get(path, "-")

    # ================================================================ run
    def run(self) -> Result:
        self.head = self.git.head()
        self.log = self.git.first_parent_log(self.head)
        self.first_add = {}
        for sha, _parent, changes in self.log:
            for st, p in changes:
                if st.startswith("A") and p not in self.first_add:
                    self.first_add[p] = sha
        if self.git.is_shallow():
            self.F.w("A0", "shallow_clone", "refs/heads/main", "-", "a full clone is required (R-AN-66)")
        if not self.judge_manifest_ok:
            self.F.w("A0", "judge_manifest", "judge_analyst/FROZEN_JUDGE_ANALYST.sha256", "-",
                     "the judge's files differ from its own manifest")
        self.mode_cfg = self.json_at_head("analyst/MODE.json")
        self.params = self.json_at_head("analyst/PARAMS.json") or {}
        self.pins = self.json_at_head("analyst/PINS.json") or {}
        if not isinstance(self.mode_cfg, dict):
            self.F.v("A8", "mode_file", "analyst/MODE.json", "-", "MODE.json unreadable: expected windows unknown")
            self.mode_cfg = {}
        self.holidays = Holidays(self.bytes_at_head("analyst/tables/holidays.csv"))
        self.load_api()
        self.wit = []
        self.wit_by_seq = {}
        self.windows = {"canary": [], "live": []}
        self.terminal_info = {}
        self.first_day = None
        self.overlay = {}
        self.safe("witnesses", self.load_witnesses)
        self.safe("witness_runs", self.check_witness_runs)
        self.safe("comments", self.check_comments)
        self.safe("pushes", self.check_pushes)
        for mode in ("canary", "live"):
            self.safe(f"mode_{mode}", self.check_mode, mode)
        self.safe("start", self.check_start)
        self.safe("runs_exports", self.check_runs_exports)
        self.safe("settlement_state", self.check_settlement_and_state)
        self.safe("mirror_source", self.check_mirror_source)
        self.safe("frozen", self.check_frozen)
        self.safe("history", self.check_history)
        self.summary["head"] = self.head
        return Result(self.F, self.head, self.evidence_new, self.summary)

    def safe(self, name, fn, *args):
        """Run one check; an unexpected exception (a malformed record the code did not foresee) becomes an A0 WARNING
        naming the check, so the run still ends with a verdict that is never SOUND."""
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001
            self.F.w("A0", "check_crashed", f"judge/{name}", "-", safe_detail(type(e).__name__))
            return None

    # ================================================================ API and evidence (R-AN-66, R-AN-69)
    def load_api(self):
        self.activity = []
        self.comments = []
        self.runs = {}
        self.api_ok = False
        if self.api is not None:
            try:
                self.activity = self.api.activity()
                issue = self.pins.get("witness_issue")
                self.comments = self.api.issue_comments(issue) if isinstance(issue, int) else []
                self.api_ok = True
            except Exception as e:  # noqa: BLE001
                self.F.w("A0", "api_unreachable", "api", "-", safe_detail(type(e).__name__))
        else:
            self.F.w("A0", "api_unreachable", "api", "-", "no API client")
        ev_act = {r["activity_id"]: r for r in self.evidence_rows if r["source"] == "activity"}
        ev_cmt = {r["comment_id"]: r for r in self.evidence_rows if r["source"] == "comment"}
        self.ev_runs = {(r["run_id"], r["run_attempt"]): r for r in self.evidence_rows if r["source"] == "run"}
        self.act_by_after = {}
        seen_ids = set()
        for r in ev_act.values():
            if r["push_after"]:
                seen_ids.add(r["activity_id"])
                self.act_by_after.setdefault(r["push_after"], []).append(
                    {"timestamp_utc": r["activity_utc"], "id": r["activity_id"], "ref": r["ref"],
                     "type": r["activity_type"], "actor": r["actor"], "source": "evidence"})
        for a in self.activity:
            aid = str(a.get("id"))
            ts = norm_api_time(a.get("timestamp"))
            actor = (a.get("actor") or {}).get("login", "")
            row = {"timestamp_utc": ts, "id": aid, "ref": a.get("ref", ""), "type": a.get("activity_type", ""),
                   "actor": actor, "source": "api"}
            if aid in ev_act and ev_act[aid]["activity_utc"] != ts:
                self.F.w("A11", "evidence_changed", f"activity/{aid}", "-", "activity timestamp differs from evidence")
            if a.get("after") and aid not in seen_ids:
                self.act_by_after.setdefault(a["after"], []).append(row)
            if aid not in ev_act:
                self.evidence_new.append(self._ev("activity", ref=a.get("ref", ""), push_after=a.get("after", ""),
                                                  activity_id=aid, activity_type=a.get("activity_type", ""),
                                                  activity_utc=ts, actor=actor))
                ev_act[aid] = self.evidence_new[-1]
        self.comment_by_id = {}
        for c in self.comments:
            cid = str(c.get("id"))
            body = c.get("body") or ""
            created = norm_api_time(c.get("created_at"))
            self.comment_by_id[cid] = {"body": body, "created_utc": created, "login": (c.get("user") or {}).get("login"),
                                       "sha": sha256_bytes(body.encode("utf-8"))}
            if cid not in ev_cmt and (c.get("user") or {}).get("login") == BOT_LOGIN:
                self.evidence_new.append(self._ev("comment", comment_id=cid, comment_created_utc=created,
                                                  comment_body_sha256=sha256_bytes(body.encode("utf-8"))))
                ev_cmt[cid] = self.evidence_new[-1]
        self.ev_cmt = ev_cmt

    def _ev(self, source, **kw):
        row = {k: "" for k in EVIDENCE_HEADER}
        row["judge_utc"] = self.now_s
        row["source"] = source
        for k, v in kw.items():
            row[k] = "" if v is None else str(v)
        return row

    def run_meta(self, run_id, run_attempt=None):
        """Run metadata: from the evidence file when the judge has seen the run before, else from the API (and then
        copied to the evidence file)."""
        if run_id is None:
            return None
        key = str(run_id)
        if key in self.runs:
            return self.runs[key]
        meta = None
        for (rid, att), r in self.ev_runs.items():
            if rid == key:
                meta = {"id": rid, "attempt": att, "event": r["run_event"], "created_utc": r["run_created_utc"],
                        "path": None, "name": None}
        if meta is None and self.api_ok:
            try:
                r = self.api.run(run_id)
            except Exception:  # noqa: BLE001
                r = None
            if r:
                meta = {"id": str(r.get("id")), "attempt": str(r.get("run_attempt", "")),
                        "event": r.get("event", ""), "created_utc": norm_api_time(r.get("created_at")),
                        "path": r.get("path", ""), "name": r.get("name", "")}
                if (meta["id"], meta["attempt"]) not in self.ev_runs:
                    self.evidence_new.append(self._ev("run", run_id=meta["id"], run_attempt=meta["attempt"],
                                                      run_event=meta["event"], run_created_utc=meta["created_utc"]))
                    self.ev_runs[(meta["id"], meta["attempt"])] = self.evidence_new[-1]
        self.runs[key] = meta
        return meta

    def check_witness_runs(self):
        """Copy every witness run's metadata to the evidence file (C14) and check the witness's run block: the run is
        an analyst-merge or analyst-settle run (A6) and its recorded created_utc/event/attempt match (A11)."""
        missing = 0
        for r in self.wit:
            run = r.rec.get("run")
            if not isinstance(run, dict) or run.get("id") is None:
                self.F.v("A6", "witness_run", r.path, r.commit, "witness without its run")
                continue
            m = self.run_meta(run.get("id"))
            if m is None:
                missing += 1
                continue
            if m.get("path") is not None and m["path"] not in (".github/workflows/analyst-merge.yml",
                                                                ".github/workflows/analyst-settle.yml"):
                self.F.v("A6", "witness_run_workflow", r.path, r.commit, "witness run is not analyst-merge/settle")
            if run.get("created_utc") != m.get("created_utc") or str(run.get("attempt")) != str(m.get("attempt")) \
                    or run.get("event") != m.get("event"):
                self.F.w("A11", "run_meta", r.path, r.commit, "witness run block differs from the run record")
        if missing:
            self.F.w("A0", "runs_unreachable", "api", "-", f"{missing} witness runs not readable")

    def server_time(self, after, run_id, ref=None):
        """(time_utc, source) recomputed: the push's activity timestamp (the entry on the push's ref, else the earliest
        entry with that `after`), else the witness run's created_at."""
        lst = self.act_by_after.get(after) if after else None
        if lst:
            same = [a for a in lst if ref and a["ref"] == ref]
            a = min(same or lst, key=lambda x: x["timestamp_utc"])
            return a["timestamp_utc"], "activity"
        m = self.run_meta(run_id)
        if m and m.get("created_utc"):
            return m["created_utc"], "run_created"
        return None, None

    # ================================================================ witnesses (R-AN-35)
    def load_witnesses(self):
        self.wit = []
        for mode in ("live", "canary"):
            folder = PREFIX[mode] + "witness/"
            for p, r in sorted(self.records(folder).items()):
                if not isinstance(r.rec, dict):
                    self.F.v("A6", "unreadable_witness", p, r.commit, "witness file is not a JSON record")
                    continue
                w = r.rec
                name = p.rsplit("/", 1)[-1]
                if w.get("schema") != "analyst-witness/1" or not isinstance(w.get("seq"), int) or \
                        name != f"{w['seq']:06d}.json" or w.get("mode") != mode:
                    self.F.v("A6", "witness_fields", p, r.commit, "schema, seq, file name or mode")
                if r.raw != record_form(w):
                    self.F.w("A11", "record_form", p, r.commit, "bytes differ from the R-AN-3 record form")
                r.mode = mode
                self.wit.append(r)
        seqs = {}
        for r in self.wit:
            s = r.rec.get("seq")
            if s in seqs:
                self.F.v("A6", "duplicate_seq", r.path, r.commit, f"seq {s}")
            seqs[s] = r
        self.wit_by_seq = seqs

    def wits(self, mode=None, kind=None, wid=None, status=None):
        out = []
        for r in self.wit:
            w = r.rec
            if mode is not None and r.mode != mode:
                continue
            if kind is not None and w.get("kind") != kind:
                continue
            if wid is not None and w.get("id") != wid:
                continue
            if status is not None and w.get("status") != status:
                continue
            out.append(r)
        return out

    @staticmethod
    def comment_body(w: dict) -> str:
        rec = {k: v for k, v in w.items() if k != "comment"}
        return f"analyst-witness/1 {w.get('kind')} {w.get('id') if w.get('id') is not None else '-'} seq " \
               f"{w.get('seq')}\n{compact_form(rec)}"

    def check_comments(self):
        """A6: a witness file whose payload differs from its comment; a file without its comment or the reverse."""
        file_comment_ids = set()
        for r in self.wit:
            w = r.rec
            c = w.get("comment")
            body = self.comment_body(w)
            body_sha = sha256_bytes(body.encode("utf-8"))
            if not isinstance(c, dict) or c.get("issue") != self.pins.get("witness_issue"):
                self.F.v("A6", "file_without_comment", r.path, r.commit, "comment block missing or wrong issue")
                continue
            cid = str(c.get("id"))
            file_comment_ids.add(cid)
            if c.get("body_sha256") != body_sha:
                self.F.v("A6", "payload_differs", r.path, r.commit, "body_sha256 is not the sha of the file payload")
            api_c = self.comment_by_id.get(cid)
            if api_c is not None:
                if api_c["login"] != BOT_LOGIN:
                    self.F.v("A6", "comment_author", r.path, r.commit, "witness comment not by the workflow token")
                if api_c["body"] != body:
                    self.F.v("A6", "payload_differs", r.path, r.commit, "comment body differs from the file payload")
                if c.get("created_utc") != api_c["created_utc"]:
                    self.F.w("A11", "comment_time", r.path, r.commit, "comment created_utc differs from the API")
            elif self.api_ok:
                ev = self.ev_cmt.get(cid)
                if ev is None:
                    self.F.v("A6", "file_without_comment", r.path, r.commit, f"comment {cid} not on the issue")
                elif ev["comment_body_sha256"] != body_sha:
                    self.F.v("A6", "payload_differs", r.path, r.commit, "evidence body sha differs")
                else:
                    self.F.w("A6", "comment_deleted", r.path, r.commit, f"comment {cid} only in the evidence file")
        # comments without a file
        seen_seq = {}
        for cid, c in self.comment_by_id.items():
            if c["login"] != BOT_LOGIN or not c["body"].startswith("analyst-witness/1 "):
                continue
            first = c["body"].split("\n", 1)[0].split(" ")
            try:
                seq = int(first[-1])
            except ValueError:
                self.F.v("A6", "comment_unparsable", f"comment/{cid}", "-", "first line")
                continue
            if seq in seen_seq:
                self.F.v("A6", "duplicate_seq", f"comment/{cid}", "-", f"seq {seq}")
            seen_seq[seq] = cid
            if cid not in file_comment_ids:
                age = self.now - parse_utc(c["created_utc"])
                if age > COMMENT_GRACE:
                    self.F.v("A6", "comment_without_file", f"comment/{cid}", "-", f"seq {seq}")
        self.summary["witness_files"] = len(self.wit)
        self.summary["witness_comments"] = len(seen_seq)

    # ================================================================ pushes and the allowlist (A3)
    def commit_available(self, sha):
        if self.git.commit_exists(sha):
            return True
        if self.fetch_commit is not None:
            try:
                if self.fetch_commit(sha):
                    return self.git.commit_exists(sha)
            except Exception:  # noqa: BLE001
                return False
        return False

    def push_diff(self, after):
        if not self.commit_available(after):
            return None
        base = self.git.merge_base(self.head, after)
        if base is None:
            return None
        return self.git.diff_name_status(base, after)

    def check_pushes(self):
        unverified = 0
        for r in self.wit:
            w = r.rec
            reason = w.get("reason")
            if reason in WITNESS_VIOLATION:
                self.F.v("A3", reason, r.path, r.commit, f"witness kind {w.get('kind')}")
            elif reason in WITNESS_WARNING:
                self.F.w("A3", reason, r.path, r.commit, f"witness kind {w.get('kind')}")
            if w.get("status") == "accepted" and w.get("kind") in ("attempt", "call", "lesson", "revision"):
                path = w.get("path") or ""
                cls = classify_path(path)
                if cls is None or cls[0] != r.mode or cls[1] != w.get("kind"):
                    self.F.v("A3", "accepted_out_of_scope", r.path, r.commit, "path is outside the allowlist")
                b = self.bytes_at_head(path)
                if b is None or sha256_bytes(b) != w.get("file_sha256"):
                    self.F.v("A3", "accepted_copy", r.path, r.commit, "main's file differs from the witnessed sha")
                after = (w.get("push") or {}).get("after")
                ch = self.push_diff(after) if after else None
                if ch is None:
                    unverified += 1
                elif not (diff_conforms(ch) and ch[0][1] == path):
                    self.F.v("A3", "accepted_out_of_scope", r.path, r.commit, "pushed diff breaks R-AN-33")
        self.summary["accepted_push_diffs_unverified"] = unverified
        # every session-written record on main came through an accepted witness
        accepted_paths = {(r.rec.get("path"), r.rec.get("file_sha256")) for r in self.wit
                          if r.rec.get("status") == "accepted"}
        for mode in ("live", "canary"):
            pre = PREFIX[mode]
            for sub in ("attempts/", "calls/", "lessons/"):
                for p in self.git.ls_tree(self.head, pre + sub):
                    if p[len(pre + sub):].count("/"):
                        continue
                    if (p, sha256_bytes(self.bytes_at_head(p))) not in accepted_paths:
                        self.F.v("A3", "unwitnessed_record", p, self.commit_of(p), "no accepted witness for it")
            for p in self.git.ls_tree(self.head, pre + "playbooks/"):
                if re.match(rf"^{re.escape(pre)}playbooks/v[1-9][0-9]*\.md$", p) and \
                        (p, sha256_bytes(self.bytes_at_head(p))) not in accepted_paths:
                    self.F.v("A3", "unwitnessed_record", p, self.commit_of(p), "no accepted witness for it")
        # pushes seen by GitHub
        witnessed_after = {(r.rec.get("push") or {}).get("after") for r in self.wit}
        owner = (self.pins.get("repo") or "/").split("/")[0]
        for a in self.activity:
            ref = a.get("ref") or ""
            after = a.get("after") or ""
            typ = a.get("activity_type") or ""
            ts = norm_api_time(a.get("timestamp"))
            actor = (a.get("actor") or {}).get("login", "")
            if ref == "refs/heads/main":
                if typ in ("force_push", "branch_deletion"):
                    self.F.v("A7", "main_rewrite", "refs/heads/main", after or "-", f"activity {typ}")
                elif actor not in (BOT_LOGIN, owner):
                    self.F.v("A3", "main_push_actor", "refs/heads/main", after or "-", safe_detail("actor", actor))
                continue
            if not ref.startswith("refs/heads/claude/"):
                continue
            if typ == "branch_deletion" or not after or set(after) == {"0"}:
                continue
            if not ref.startswith("refs/heads/claude/analyst-"):
                self.F.w("A3", "other_claude_ref", ref, after, "a claude/ push analyst-merge never sees")
                continue
            if after in witnessed_after:
                continue
            if self.now - parse_utc(ts) <= PUSH_GRACE:
                continue
            ch = self.push_diff(after)
            if ch is None:
                self.F.w("A3", "unwitnessed_push", ref, after, "no witness after 24 h; diff unavailable")
            elif diff_conforms(ch):
                self.F.w("A3", "unwitnessed_push", ref, after, "no witness after 24 h; diff conforms")
            else:
                self.F.v("A3", "unwitnessed_out_of_scope", ref, after, "no witness after 24 h; diff breaks R-AN-33")

    # ================================================================ per mode: A1, A2, A4, A5, A6, A8
    def expected_windows(self, mode):
        m = self.mode_cfg
        if mode == "canary":
            f, t = m.get("canary_from"), m.get("canary_to")
        else:
            f, t = m.get("live_from"), None
        if not f:
            return []
        d0 = dt.date.fromisoformat(f)
        d1 = dt.date.fromisoformat(t) if t else (self.now.date() + dt.timedelta(days=1))
        return windows_between(d0, d1)

    def check_mode(self, mode):
        pre = PREFIX[mode]
        wins = self.expected_windows(mode)
        expected = {w.call_id: w for w in wins}
        self.windows[mode] = wins
        terms = self.records(pre + "terminal/")
        calls = self.records(pre + "calls/")
        attempts = self.records(pre + "attempts/")
        term_by_id = {}
        for p, r in terms.items():
            cid = p.rsplit("/", 1)[-1][:-5]
            term_by_id[cid] = r
            if not isinstance(r.rec, dict):
                self.F.v("A6", "unreadable_terminal", p, r.commit, "")
                continue
            if r.raw != record_form(r.rec):
                self.F.w("A11", "record_form", p, r.commit, "bytes differ from the R-AN-3 record form")
            if cid not in expected:
                self.F.v("A8", "unexpected_terminal", p, r.commit, "terminal for a window outside the mode's period")
        call_by_id = {p.rsplit("/", 1)[-1][:-5]: r for p, r in calls.items()}
        self.summary[f"{mode}_expected_windows"] = 0
        prev_sha = ZERO64
        prev_known = True
        n_closed = 0
        for win in wins:
            cid = win.call_id
            due = self.now >= win.deadline + dt.timedelta(minutes=20) + COMPLETENESS_GRACE
            tr = term_by_id.get(cid)
            twits = self.wits(mode, "terminal", cid)
            tw_acc = [r for r in twits if r.rec.get("status") == "accepted"]
            if due:
                self.summary[f"{mode}_expected_windows"] += 1
                if tr is None:
                    self.F.v("A8", "missing_terminal", f"{pre}terminal/{cid}.json", "-", "no terminal record")
                if len(tw_acc) != 1 and (tr is not None or tw_acc):
                    self.F.v("A8", "terminal_witness_count", f"{pre}terminal/{cid}.json",
                             tr.commit if tr else "-", f"{len(tw_acc)} terminal witnesses")
            elif tr is None and len(tw_acc) > 1:
                self.F.v("A8", "terminal_witness_count", f"{pre}terminal/{cid}.json", "-",
                         f"{len(tw_acc)} terminal witnesses")
            if tr is None:
                prev_known = False
                continue
            n_closed += 1
            t = tr.rec if isinstance(tr.rec, dict) else {}
            tsha = sha256_bytes(tr.raw)
            # chain (R-AN-37)
            if prev_known and t.get("prev_chain_sha256") != prev_sha:
                self.F.v("A6", "broken_chain", tr.path, tr.commit, "prev_chain_sha256 is not the previous terminal")
            prev_sha, prev_known = tsha, True
            if len(tw_acc) == 1:
                tw = tw_acc[0].rec.get("terminal") or {}
                if tw.get("terminal_sha256") != tsha or tw.get("prev_chain_sha256") != t.get("prev_chain_sha256"):
                    self.F.v("A6", "terminal_witness_differs", tr.path, tr.commit, "witness terminal block")
            self.safe(f"window_{mode}_{cid}", self.check_window, mode, win, tr, t, call_by_id.get(cid), attempts)
        self.summary[f"{mode}_terminals"] = n_closed

    def check_window(self, mode, win, tr, t, call_r, attempts):
        pre = PREFIX[mode]
        cid = win.call_id
        # fixed fields
        want = {"schema": "analyst-terminal/1", "call_id": cid, "mode": mode, "window": win.w,
                "model_day": win.day.isoformat(), "entry_utc": fmt_utc(win.entry),
                "deadline_utc": fmt_utc(win.deadline)}
        for k, v in want.items():
            if t.get(k) != v:
                self.F.w("A11", "terminal_fields", tr.path, tr.commit, f"field {k}")
        status = t.get("status")
        if status not in TERMINAL_STATUSES or status == "missed:push":
            self.F.v("A6", "terminal_status_enum", tr.path, tr.commit, safe_detail(status))
        # attempts of this draw (accepted witnesses)
        acc = self.wits(mode, "attempt", cid, "accepted")
        att = []
        for r in acc:
            w = r.rec
            push = w.get("push") or {}
            ts, src = self.server_time(push.get("after"), (w.get("run") or {}).get("id"), push.get("ref"))
            if ts is None:
                self.summary["times_unverified"] = self.summary.get("times_unverified", 0) + 1
                ts = (w.get("window") or {}).get("time_utc")
            elif ts != (w.get("window") or {}).get("time_utc"):
                self.F.w("A11", "server_time", r.path, r.commit, "attempt time differs from the recomputed one")
            arec = self.json_at_head(w.get("path") or "") or {}
            n = arec.get("n")
            void = ts is None or ts < fmt_utc(win.span_lo) or ts > fmt_utc(win.deadline)
            if not (w.get("window") or {}).get("void") and void:
                self.F.v("A2", "attempt_window", r.path, r.commit, "non-void attempt pushed outside [entry-45, deadline]")
            elif bool((w.get("window") or {}).get("void")) != void:
                self.F.w("A11", "void_flag", r.path, r.commit, "void flag differs from the recomputed one")
            att.append({"n": n, "time": ts, "void": void, "path": w.get("path"), "seq": w.get("seq"),
                        "sha": w.get("file_sha256")})
        if len(att) > 2:
            self.F.v("A2", "attempt_count", tr.path, tr.commit, f"{len(att)} accepted attempts")
        ns = [a["n"] for a in att]
        if len(set(ns)) != len(ns) or any(n not in (1, 2) for n in ns):
            self.F.v("A2", "attempt_numbers", tr.path, tr.commit, "attempt numbers repeat or are not 1/2")
        # the call
        cw = self.wits(mode, "call", cid, "accepted")
        lock = lock_src = None
        call = call_r.rec if call_r is not None and isinstance(call_r.rec, dict) else None
        if cw:
            w = cw[0].rec
            push = w.get("push") or {}
            lock, lock_src = self.server_time(push.get("after"), (w.get("run") or {}).get("id"), push.get("ref"))
            if lock is None:
                self.summary["times_unverified"] = self.summary.get("times_unverified", 0) + 1
                lock = (w.get("window") or {}).get("time_utc")
            elif lock != (w.get("window") or {}).get("time_utc"):
                self.F.w("A11", "server_time", cw[0].path, cw[0].commit, "lock time differs from the recomputed one")
        # expected terminal status (R-AN-36, R-AN-39)
        derivable = True
        if call is not None:
            derivable = self.check_call(mode, win, call_r, call, lock)
        if cw and t.get("writer") == "merge":
            if lock is None or lock > fmt_utc(win.deadline):
                exp = ("no_call:late", None)
            else:
                k = (call or {}).get("attempt_n")
                a = [x for x in att if x["n"] == k]
                if not a or a[0]["time"] is None or not (a[0]["time"] < lock):
                    exp = ("no_call:invalid", "no_attempt")
                elif a[0]["void"]:
                    exp = ("no_call:invalid", "void_attempt")
                elif not derivable:
                    exp = ("no_call:invalid", "underivable")
                else:
                    exp = ((call or {}).get("status"), (call or {}).get("detail"))
        elif self.holidays.window_holiday(win):
            exp = ("skipped:holiday", None)
        elif att:
            exp = ("no_call:run", None)
        else:
            exp = ("missed:trigger", t.get("detail") if status == "missed:trigger" else None)
            if not self.holidays.covers(win.day):
                exp = ("missed:trigger", "no_table")
        got = (status, t.get("detail"))
        if status in ("called", "stay_out"):
            if not cw:
                self.F.v("A2", "call_without_witness", tr.path, tr.commit, "no accepted call push")
            elif lock is not None and lock > fmt_utc(win.deadline):
                self.F.v("A1", "late_lock", tr.path, tr.commit, safe_detail("lock", lock, "deadline",
                                                                             fmt_utc(win.deadline)))
            elif exp[0] == "no_call:invalid" and exp[1] in ("no_attempt", "void_attempt"):
                self.F.v("A2", exp[1], tr.path, tr.commit, "counted call without a valid earlier attempt")
            elif exp[0] == "no_call:invalid" and exp[1] == "underivable":
                pass  # reported by A5 on the call file
            elif got != exp:
                self.F.v("A2", "terminal_status", tr.path, tr.commit, safe_detail("expected", exp[0], exp[1]))
        elif got != exp and not (exp[0] == "missed:trigger" and status == "missed:trigger"):
            self.F.w("A11", "terminal_status", tr.path, tr.commit, safe_detail("expected", exp[0], exp[1], "got",
                                                                              status, t.get("detail")))
        # terminal's call and attempts blocks
        tc = t.get("call")
        if cw and t.get("writer") == "merge":
            w = cw[0].rec
            csha = sha256_bytes(call_r.raw) if call_r is not None else None
            if not isinstance(tc, dict) or tc.get("sha256") != csha:
                self.F.v("A6", "terminal_call_sha", tr.path, tr.commit, "terminal.call.sha256 is not the call file's")
            else:
                for k, v in (("path", f"{pre}calls/{win.call_id}.json"), ("plan_status", (call or {}).get("status")),
                             ("lock_utc", (w.get("window") or {}).get("time_utc")),
                             ("push_sha", (w.get("push") or {}).get("after")), ("witness_seq", w.get("seq"))):
                    if tc.get(k) != v:
                        self.F.w("A11", "terminal_fields", tr.path, tr.commit, f"call.{k}")
        ta = t.get("attempts")
        want_att = sorted([{"n": a["n"], "path": a["path"], "sha256": a["sha"], "void": a["void"],
                            "witness_seq": a["seq"]} for a in att], key=lambda x: (x["n"] is None, x["n"]))
        got_att = sorted([{k: x.get(k) for k in ("n", "path", "sha256", "void", "witness_seq")}
                          for x in (ta or []) if isinstance(x, dict)], key=lambda x: (x["n"] is None, x["n"]))
        if not isinstance(ta, list) or got_att != want_att:
            self.F.w("A11", "terminal_fields", tr.path, tr.commit, "attempts")
        self.terminal_info[(mode, win.call_id)] = {"status": status, "lock": lock, "lock_src": lock_src,
                                                   "attempts": len(att), "call": call, "call_raw": call_r,
                                                   "term": tr, "win": win, "detail": t.get("detail")}

    # ---------------------------------------------------------------- A4 / A5 on one call file
    def check_call(self, mode, win, call_r, call, lock) -> bool:
        """A4 (context reproduction) and A5 (derivability). Returns whether S/T/D_ref follow (R-AN-36 step 4)."""
        p, c = call_r.path, call_r.commit
        ctx = call.get("context")
        derivable = True
        if call.get("schema") != "analyst-call/1" or call.get("call_id") != win.call_id or call.get("mode") != mode \
                or call.get("window") != win.w or call.get("model_day") != win.day.isoformat():
            self.F.v("A5", "call_ids", p, c, "schema, ids or mode")
        for k, v in win.times().items():
            if call.get(k) != v:
                self.F.v("A5", "call_times", p, c, f"field {k}")
        status = call.get("status")
        if status not in CALL_STATUSES:
            self.F.v("A5", "call_status", p, c, safe_detail(status))
        if not isinstance(ctx, dict):
            self.F.v("A4", "context_missing", p, c, "")
            return False
        if call.get("context_sha256") != sha256_compact(ctx):
            self.F.v("A4", "context_sha", p, c, "context_sha256 is not the sha of the compact context")
        if call.get("decision_utc") != ctx.get("asof_utc"):
            self.F.v("A4", "decision_utc", p, c, "decision_utc differs from context.asof_utc")
        if lock is not None and isinstance(ctx.get("asof_utc"), str):
            try:
                if parse_utc(ctx["asof_utc"]) > parse_utc(lock) + dt.timedelta(seconds=60):
                    self.F.v("A4", "asof_after_lock", p, c, "asof_utc > lock_utc + 60 s")
            except ValueError:
                pass
        R = ctx.get("repo_head_sha")
        if not (isinstance(R, str) and self.git.commit_exists(R) and self.git.is_ancestor(R, self.head)):
            self.F.v("A4", "repo_head", p, c, "repo_head_sha is not a commit of main")
        else:
            rb = rebuild_context(ctx, win.call_id, mode, self.git, self.hours, self.holidays, self.events)
            if rb.error:
                self.F.v("A4", "context_unreproducible", p, c, safe_detail("rebuild", rb.error))
            else:
                for sym, hour, _a, _b in rb.sha_mismatch:
                    self.F.w("A11", "hour_sha", f"{sym}/{hour}", c, safe_detail("call", win.call_id))
                if rb.unavailable:
                    self.F.w("A0", "hours_unavailable", p, c, f"{len(rb.unavailable)} hour files not re-downloaded")
                for sym, hour in rb.inadmissible:
                    self.F.v("A4", "inadmissible_hour", p, c, safe_detail(sym, hour))
                mm = compare_context(rb, ctx)
                if mm:
                    self.F.v("A4", "context_differs", p, c, safe_detail(*mm[:12]))
                self.summary["contexts_rebuilt"] = self.summary.get("contexts_rebuilt", 0) + 1
        # ---- A5: derivable from the plan and the context
        ctx_status = ctx.get("status")
        plan = call.get("plan")
        if ctx_status in ("no_call:data", "no_call:d_ref"):
            if status != ctx_status or plan is not None:
                self.F.v("A5", "status_vs_context", p, c, "a no_call context must give that status and no plan")
        elif status in ("called", "stay_out"):
            errs = derive.plan_errors(plan)
            if errs:
                self.F.v("A5", "plan_invalid", p, c, safe_detail(*errs))
                derivable = False
            else:
                act = plan["action"]
                if (status == "called") != (act in ("buy", "sell")):
                    self.F.v("A5", "status_vs_plan", p, c, "status does not follow from plan.action")
        dref = ctx.get("d_ref")
        if call.get("d_ref") != dref:
            self.F.v("A5", "d_ref_copy", p, c, "call.d_ref is not context.d_ref")
            derivable = False
        if ctx_status == "ok":
            ok = isinstance(dref, dict) and isinstance(dref.get("rows"), list) and len(dref["rows"]) >= 1
            if ok:
                try:
                    pref = (ctx.get("p_ref") or {}).get("p_ref")
                    v = derive.dref_values([{"F_S": float(r["F_S"])} for r in dref["rows"]], float(pref))
                    ok = (rel_eq(dref.get("F_ref"), v["F_ref"]) and rel_eq(dref.get("D_raw"), v["D_raw"]) and
                          num_exact(dref.get("D_ref"), v["D_ref"]) and dref.get("D_ref_cents") == v["D_ref_cents"] and
                          isinstance(dref.get("D_ref_cents"), int) and price_eq(dref.get("P_ref"), float(pref)) and
                          dref.get("slot") == win.slot and dref.get("k") == derive.K_DREF and len(dref["rows"]) >= 10)
                except (TypeError, ValueError, KeyError):
                    ok = False
            if not ok:
                self.F.v("A5", "d_ref_underivable", p, c, "D_ref does not follow from the context")
                derivable = False
        if status == "called" and derivable and isinstance(plan, dict) and not derive.plan_errors(plan):
            side = derive.side_of(plan["action"])
            sd, td = derive.st_dimes(dref["D_ref_cents"], plan["stop_mult"], plan["target_mult"])
            checks = [call.get("side") == side, num_exact(call.get("stop_mult"), float(plan["stop_mult"])),
                      num_exact(call.get("target_mult"), float(plan["target_mult"])),
                      call.get("S_dimes") == sd, call.get("T_dimes") == td,
                      num_exact(call.get("S"), sd / 10), num_exact(call.get("T"), td / 10)]
            if not all(checks):
                self.F.v("A5", "st_underivable", p, c, "side, multiples, S or T do not follow from plan and D_ref")
                derivable = False
        elif status in ("stay_out",) and isinstance(plan, dict):
            if call.get("side") not in (0, None) or any(call.get(k) is not None for k in
                                                         ("stop_mult", "target_mult", "S", "T", "S_dimes", "T_dimes")):
                self.F.v("A5", "st_underivable", p, c, "stay_out carries trade fields")
                derivable = False
        elif status not in ("called", "stay_out"):
            if any(call.get(k) is not None for k in ("S", "T", "S_dimes", "T_dimes")):
                self.F.v("A5", "st_underivable", p, c, "a no_call carries S or T")
        req = call.get("request") or {}
        if req.get("model_set") != self.pins.get("model") or req.get("effort_set") != self.pins.get("effort"):
            self.F.w("A11", "request_pins", p, c, "request.model_set/effort_set differ from PINS")
        self.check_frozen_selfreport(p, c, call, R)
        return derivable

    def check_frozen_selfreport(self, path, commit, call, R):
        fr = call.get("frozen") or {}
        tag = self.pins.get("freeze_tag") or "analyst-v1-freeze"
        ref = self.git.tag_commit(tag) or (R if isinstance(R, str) and self.git.commit_exists(R) else None)
        if ref is None:
            return
        man = self.git.show(ref, "analyst/FROZEN_ANALYST.sha256")
        if man is None:
            return
        msha = sha256_bytes(man)
        rp = self.git.show(ref, "analyst/ROUTINE_PROMPT.md")
        if fr.get("manifest_sha256") != msha or fr.get("code_sha8") != msha[:8] or \
                (rp is not None and fr.get("routine_prompt_sha256") != sha256_bytes(rp)):
            self.F.w("A11", "frozen_selfreport", path, commit, "self-reported manifest or prompt sha differs")

    # ================================================================ START (A9)
    def check_start(self):
        st_path = "analyst/START.json"
        st = self.json_at_head(st_path)
        first = None
        for win in self.windows.get("live", []):
            info = self.terminal_info.get(("live", win.call_id))
            if info and info["status"] in ("called", "stay_out"):
                first = (win, info)
                break
        self.first_day = None
        if first is None:
            if st is not None:
                self.F.v("A9", "start_without_call", st_path, self.commit_of(st_path), "no live called/stay_out")
            return
        win, info = first
        d0 = win.day
        m = d0 + dt.timedelta(days=(7 - d0.weekday()) % 7 or 7)
        if d0.weekday() >= 3:
            m += dt.timedelta(days=7)
        self.first_day = m
        if st is None:
            self.F.v("A9", "start_missing", st_path, "-", "first successful live call has no START.json")
            return
        tr = info["term"]
        want = {"schema": "analyst-start/1", "freeze_tag": self.pins.get("freeze_tag"), "first_call_id": win.call_id,
                "d0": d0.isoformat(), "first_day": m.isoformat(), "terminal_sha256": sha256_bytes(tr.raw),
                "first_call_lock_utc": info["lock"],
                "rule": "first Monday m > d0 with numpy.busday_count(d0, m) >= 3"}
        bad = [k for k, v in want.items() if st.get(k) != v]
        if self.commit_of(st_path) != self.commit_of(tr.path):
            bad.append("commit")
        if bad:
            self.F.v("A9", "start_differs", st_path, self.commit_of(st_path), safe_detail(*bad))
        self.first_day = m

    # ================================================================ owner-attested runs (A10, R-AN-57, R-AN-60)
    def check_runs_exports(self):
        self.overlay = {}
        runs = []
        exports = {}
        for p, r in self.records("analyst/runs/").items():
            if not isinstance(r.rec, dict) or r.rec.get("schema") != "analyst-runs/1":
                self.F.w("A10", "runs_export_form", p, r.commit, "unreadable export", owner_attested=True)
                continue
            exports[r.rec.get("iso_week")] = r
            for x in r.rec.get("runs") or []:
                if isinstance(x, dict):
                    runs.append((r, x))
        model = self.pins.get("model")
        t_run = int(self.params.get("T_run_min") or 0)
        t_setup = int(self.params.get("T_setup_min") or 0)
        acc_by_mode = {}
        for mode in ("live", "canary"):
            acc_by_mode[mode] = {r.rec.get("id") for r in self.wits(mode, "attempt", None, "accepted")}
        win_index = {}
        for mode in ("live", "canary"):
            for w in self.windows.get(mode, []):
                win_index.setdefault(w.call_id, []).append((mode, w))
        pauses = self.pause_intervals()
        for r, x in runs:
            if x.get("model") is None:
                self.F.w("A10", "model_unknown", r.path, r.commit, safe_detail("run", x.get("run_id")),
                         owner_attested=True)
            elif x.get("model") != model:
                self.F.v("A10", "run_model", r.path, r.commit, safe_detail("run", x.get("run_id")),
                         owner_attested=True)
            routine = x.get("routine")
            if routine not in ("analyst-w1", "analyst-w2"):
                continue
            st = str(x.get("status") or "").lower()
            if not (st.startswith("succe") or st == "completed"):
                continue
            try:
                cu = parse_utc(x.get("created_utc"))
            except ValueError:
                continue
            wname = "W1" if routine == "analyst-w1" else "W2"
            cid = f"{cu.date().isoformat()}_{wname}"
            for mode, w in win_index.get(cid, []):
                hi = w.deadline - dt.timedelta(minutes=t_run + t_setup)
                if not (w.span_lo <= cu <= hi) or self.holidays.window_holiday(w):
                    continue
                if mode == "live" and any(a <= cu < b for a, b in pauses):
                    continue
                if cid not in acc_by_mode[mode]:
                    self.F.v("A10", "in_span_without_attempt", r.path, r.commit, safe_detail("run", x.get("run_id"),
                                                                                             cid),
                             owner_attested=True)
        # model overlay for live called/stay_out (R-AN-57)
        for win in self.windows.get("live", []):
            info = self.terminal_info.get(("live", win.call_id))
            if not info or info["status"] not in ("called", "stay_out"):
                continue
            sid = ((info["call"] or {}).get("self_reported") or {}).get("session_id")
            routine = "analyst-w1" if win.w == "W1" else "analyst-w2"
            cands = [(r, x) for r, x in runs if x.get("routine") == routine]
            match = [(r, x) for r, x in cands if sid and x.get("session_id") == sid]
            if not match:
                lo, hi = win.entry - dt.timedelta(minutes=60), win.deadline
                match = [(r, x) for r, x in cands if not x.get("session_id") and
                         _in(x.get("created_utc"), lo, hi)]
            if not match:
                wk = iso_week(win.day)
                week_end = win.day + dt.timedelta(days=7 - win.day.weekday())
                if wk in exports or self.now.date() > week_end + dt.timedelta(days=3):
                    self.F.w("A11", "unverified_model", info["term"].path, info["term"].commit,
                             "no owner-exported run matches this call", owner_attested=True)
                continue
            r, x = match[0]
            if x.get("model") is not None and x.get("model") != model:
                self.overlay[win.call_id] = str(x.get("run_id"))

    def pause_intervals(self):
        """[(start, end)] UTC datetimes when live calls were disabled (PAUSED..RESUMED, RETIRED..)."""
        out = []
        evs = []
        for p, r in sorted(self.records("analyst/state/").items()):
            if isinstance(r.rec, dict) and r.rec.get("schema") == "analyst-state-event/1":
                evs.append(r.rec)
        evs.sort(key=lambda e: e.get("seq") or 0)
        open_ = {}
        for e in evs:
            try:
                t = parse_utc(e.get("utc"))
            except (ValueError, TypeError):
                continue
            if e.get("event") == "PAUSED":
                open_[e.get("reason")] = t
            elif e.get("event") == "RESUMED":
                subj = str(e.get("reason") or "")[len("clear:"):]
                if subj in open_:
                    out.append((open_.pop(subj), t))
            elif e.get("event") == "RETIRED":
                out.append((t, dt.datetime.max.replace(tzinfo=UTC)))
        for t in open_.values():
            out.append((t, dt.datetime.max.replace(tzinfo=UTC)))
        return out

    # ================================================================ settlement, twins, E, K, states, ledger (A11)
    def check_settlement_and_state(self):
        days_b = self.bytes_at_head("mirror/system/days.csv")
        if days_b is None:
            self.summary["settled_checked"] = 0
            return
        try:
            days = DaysTable(days_b)
        except ValueError:
            self.F.w("A11", "mirror_days", "mirror/system/days.csv", "-", "unreadable")
            return
        settled = self.records("analyst/settled/")
        twins_r = self.records("analyst/twins/")
        trades = []
        twins = []
        processed = set()
        n_checked = 0
        rows_expected = []
        live_wins = self.windows.get("live", [])
        by_day = {}
        for win in live_wins:
            info = self.terminal_info.get(("live", win.call_id))
            if info is None:
                continue
            by_day.setdefault(win.day, []).append((win, info))
        store = Store(days, lambda p: self.bytes_at_head(p))
        for win in live_wins:
            info = self.terminal_info.get(("live", win.call_id))
            if info is None:
                continue
            two = (win.day - dt.timedelta(days=1), win.day)
            for x in two:
                store.load_day(x)
            final_inputs = all(days.row(x, s) is not None and days.row(x, s)["status"] in ("ok", "missing")
                               for x in two for s in ("BID", "ASK"))
            bad_store = any(m[0] in (two[0].isoformat(), two[1].isoformat()) for m in store.mismatch)
            processable = final_inputs and not bad_store and self.now >= win.exit
            mins = settle.Minutes(store, win)
            void = settle.is_void(days, win.day)
            sp = f"analyst/settled/{win.call_id}.json"
            tp = f"analyst/twins/{win.call_id}.json"
            cls = "counted" if (self.first_day is not None and win.day >= self.first_day) else "shakedown"
            row = {"call_id": win.call_id, "model_day": win.day.isoformat(), "window": win.w,
                   "terminal_status": info["status"], "status": info["status"], "class": cls,
                   "terminal_sha256": sha256_bytes(info["term"].raw)}
            if win.call_id in self.overlay:
                row["status"] = "no_call:model"
            call = info["call"] or {}
            out = None
            if info["status"] == "called" and isinstance(call.get("S_dimes"), int):
                s = call.get("side")
                out = settle.settle_side(mins, s, call["S_dimes"], call["T_dimes"], void) if processable else None
                sr = settled.get(sp)
                if sr is None:
                    if processable and self.now >= win.exit + SETTLE_GRACE:
                        self.F.w("A11", "unsettled", sp, "-", "processable trade without a settled record")
                elif out is None:
                    self.F.w("A11", "settled_unverifiable", sp, sr.commit, "inputs at HEAD not final")
                else:
                    n_checked += 1
                    mm = self.compare_settled(sr, out, mins, s, call, info, cls, days)
                    if mm:
                        self.F.w("A11", "settled_mismatch", sp, sr.commit, safe_detail(*mm[:12]))
                if out is not None and sr is not None and cls == "counted" and win.call_id not in self.overlay:
                    o = out
                    trades.append({"call_id": win.call_id, "model_day": win.day, "w": win.w, "side": s,
                                   "y": o["y"], "R": o["R"], "net": o["net_usd"], "stress": o["stress_net_usd"],
                                   "cost": o["cost_usd"]})
                row.update({"out": out})
            # twin
            if info["status"] != "skipped:holiday":
                tw = settle.twin(mins, void) if processable else None
                trc = twins_r.get(tp)
                if trc is None:
                    if processable and self.now >= win.exit + SETTLE_GRACE:
                        self.F.w("A11", "untwinned", tp, "-", "processed window without a twin")
                elif tw is not None:
                    mm = self.compare_twin(trc, tw, win, info)
                    if mm:
                        self.F.w("A11", "twin_mismatch", tp, trc.commit, safe_detail(*mm))
                if tw is not None and tw[5] == "final":
                    twins.append((win.day, win.w, tw[4]))
                row["d_W"] = tw[4] if tw else None
            rows_expected.append(row)
        for d, lst in by_day.items():
            ok = True
            for win, info in lst:
                if info["status"] == "skipped:holiday":
                    continue
                if f"analyst/twins/{win.call_id}.json" not in twins_r:
                    ok = False
                if info["status"] == "called" and f"analyst/settled/{win.call_id}.json" not in settled:
                    ok = False
            if ok and len(lst) == 2:
                processed.add(d)
        self.summary["settled_checked"] = n_checked
        # E path over counted trades (window order), ledger and state
        ys = [t["y"] for t in trades]
        epath = stats.e_path(ys)
        for t, e in zip(trades, epath):
            t["E"] = e
        self.check_ledger(rows_expected, trades)
        self.check_states(trades, twins, processed, rows_expected)

    def compare_settled(self, sr, out, mins, s, call, info, cls, days):
        r = sr.rec if isinstance(sr.rec, dict) else {}
        mm = []
        if sr.raw != record_form(r):
            mm.append("record_form")
        exp_fixed = {"schema": "analyst-settled/1", "call_id": info["win"].call_id, "mode": "live",
                     "window": info["win"].w, "model_day": info["win"].day.isoformat(), "class": cls,
                     "action": (call.get("plan") or {}).get("action"), "side": s,
                     "S_dimes": call.get("S_dimes"), "T_dimes": call.get("T_dimes"),
                     "entry_utc": fmt_utc(info["win"].entry), "exit_utc": fmt_utc(info["win"].exit),
                     "call_sha256": sha256_bytes(info["call_raw"].raw),
                     "terminal_sha256": sha256_bytes(info["term"].raw),
                     "context_sha256": call.get("context_sha256")}
        for k, v in exp_fixed.items():
            if r.get(k) != v:
                mm.append(k)
        for k in ("stop_mult", "target_mult", "D_ref", "S", "T"):
            src = call.get(k) if k != "D_ref" else (call.get("d_ref") or {}).get("D_ref")
            if not num_exact(r.get(k), src):
                mm.append(k)
        if r.get("context") != call.get("context"):
            mm.append("context")
        plan = call.get("plan") or {}
        want_call = {k: plan.get(k) for k in ("action", "stop_mult", "target_mult", "confidence", "reason_ar",
                                              "key_risks_ar")}
        rc = r.get("call") or {}
        if any(not (rc.get(k) == v or num_exact(rc.get(k), v)) for k, v in want_call.items()):
            mm.append("call")
        mm += [f"outcome.{x}" for x in _cmp_outcome(r.get("outcome"), out)]
        opp = settle.settle_side(mins, -s, call["S_dimes"], call["T_dimes"], out["status"] == "void")
        mm += [f"opposite.{x}" for x in _cmp_outcome(r.get("opposite"), opp)]
        ps = settle.path_summary(mins, s, out)
        if ps is not None:
            mm += [f"path_summary.{x}" for x in _cmp_path(r.get("path_summary"), ps)]
        inp = r.get("inputs") or {}
        head_sha = r.get("head_sha")
        if isinstance(head_sha, str) and self.git.commit_exists(head_sha):
            db = self.git.show(head_sha, "mirror/system/days.csv")
            if db is None or inp.get("days_csv_sha256") != sha256_bytes(db):
                mm.append("inputs.days_csv_sha256")
            else:
                dtab = DaysTable(db)
                want = []
                for x in (info["win"].day - dt.timedelta(days=1), info["win"].day):
                    for sd in ("BID", "ASK"):
                        rw = dtab.row(x, sd) or {}
                        want.append({"day": x.isoformat(), "side": sd, "status": rw.get("status"),
                                     "sha256": rw.get("sha256") or None})
                got = [{k: (d.get(k) or None) if k == "sha256" else d.get(k) for k in ("day", "side", "status",
                                                                                         "sha256")}
                       for d in (inp.get("days") or []) if isinstance(d, dict)]
                if got != want:
                    mm.append("inputs.days")
        else:
            mm.append("head_sha")
        return mm

    def compare_twin(self, trc, tw, win, info):
        r = trc.rec if isinstance(trc.rec, dict) else {}
        mm = []
        me, mes, mx, mxs, dW, status = tw
        want = {"schema": "analyst-twin/1", "call_id": win.call_id, "mode": "live", "window": win.w,
                "model_day": win.day.isoformat(), "terminal_status": info["status"], "entry_utc": fmt_utc(win.entry),
                "exit_utc": fmt_utc(win.exit), "status": status}
        for k, v in want.items():
            if r.get(k) != v:
                mm.append(k)
        if status == "final":
            if not (price_eq(r.get("mid_entry"), me) and price_eq(r.get("mid_exit"), mx)):
                mm.append("mids")
            if r.get("mid_entry_src") != mes or r.get("mid_exit_src") != mxs:
                mm.append("mid_src")
            if not rel_eq(r.get("d_W"), dW):
                mm.append("d_W")
        elif r.get("d_W") is not None:
            mm.append("d_W")
        return mm

    def terminals_at_last_change(self, path):
        """call_ids of the live terminal records present at the last main commit that changed `path` (a snapshot is
        compared with the state of the records when it was written)."""
        last = None
        for sha, _parent, changes in self.log:
            if any(p == path for _st, p in changes):
                last = sha
        if last is None:
            return None
        return {p.rsplit("/", 1)[-1][:-5] for p in self.git.ls_tree(last, "analyst/terminal/")}

    def check_ledger(self, rows_expected, trades):
        b = self.bytes_at_head("analyst/ledger.csv")
        if b is None:
            if rows_expected:
                self.F.w("A11", "ledger_missing", "analyst/ledger.csv", "-", "")
            return
        try:
            led = list(csv.DictReader(io.StringIO(b.decode("utf-8"))))
        except Exception:  # noqa: BLE001
            self.F.w("A11", "ledger_unreadable", "analyst/ledger.csv", "-", "")
            return
        by = {r.get("call_id"): r for r in led}
        tno = {t["call_id"]: (i + 1, t) for i, t in enumerate(trades)}
        bad = []
        # the ledger is a snapshot: compare the windows whose terminal existed at its last commit
        known = self.terminals_at_last_change("analyst/ledger.csv")
        for row in rows_expected:
            if known is not None and row["call_id"] not in known:
                continue
            lr = by.get(row["call_id"])
            if lr is None:
                bad.append(f"{row['call_id']}:missing")
                continue
            for k in ("model_day", "window", "terminal_status", "status", "terminal_sha256"):
                if lr.get(k, "") != str(row[k]):
                    bad.append(f"{row['call_id']}:{k}")
            if row["call_id"] in tno:
                i, t = tno[row["call_id"]]
                if lr.get("trade_no") != str(i) or not rel_eq(_f(lr.get("E")), t["E"]) or \
                        not rel_eq(_f(lr.get("y")), t["y"]):
                    bad.append(f"{row['call_id']}:trade")
            elif lr.get("trade_no", "") != "":
                bad.append(f"{row['call_id']}:trade_no")
            o = row.get("out")
            if o is not None and not rel_eq(_f(lr.get("net_usd")), o["net_usd"]):
                bad.append(f"{row['call_id']}:net_usd")
            if row.get("d_W") is not None and not rel_eq(_f(lr.get("d_W")), row["d_W"]):
                bad.append(f"{row['call_id']}:d_W")
        if bad:
            self.F.w("A11", "ledger_mismatch", "analyst/ledger.csv", "-", safe_detail(*bad[:12]))

    def check_states(self, trades, twins, processed, rows_expected):
        evs = []
        for p, r in sorted(self.records("analyst/state/").items()):
            if not isinstance(r.rec, dict) or r.rec.get("schema") != "analyst-state-event/1":
                self.F.w("A11", "state_event_form", p, r.commit, "")
                continue
            evs.append(r)
        evs.sort(key=lambda r: r.rec.get("seq") or 0)
        replay = stats.replay_states(trades, twins, self.first_day, processed)
        got = [(r.rec.get("event"), r.rec.get("reason"), r.rec.get("model_day"), r.rec.get("n"), r.rec.get("E"))
               for r in evs if r.rec.get("event") in ("TESTING", "PROMOTED", "DEMOTED", "RETIRED") and
               r.rec.get("reason") != "violation"]
        want = [(e, rs, d.isoformat(), n, E) for (e, rs, d, n, E, _K) in replay]
        if len(got) > len(want) or any(g[:3] != w[:3] or g[3] != w[3] or not rel_eq(g[4], w[4])
                                       for g, w in zip(got, want)):
            self.F.w("A11", "state_events", "analyst/state/", "-", safe_detail("recorded", len(got), "replayed",
                                                                               len(want)))
        elif len(want) > len(got):
            last = want[len(got)]
            self.F.w("A11", "state_event_missing", "analyst/state/", "-", safe_detail(last[0], last[1], last[2]))
        # PAUSED model:<run_id> needs an overlay; RESUMED clear:<subject> needs an owner ADJUDICATIONS row
        adj = self.bytes_at_head("analyst/ADJUDICATIONS.csv") or b""
        clears = set()
        try:
            for r in csv.DictReader(io.StringIO(adj.decode("utf-8"))):
                if r.get("decision") == "clear":
                    clears.add(r.get("subject"))
        except Exception:  # noqa: BLE001
            pass
        for r in evs:
            e = r.rec
            if e.get("event") == "PAUSED" and str(e.get("reason", "")).startswith("model:"):
                if str(e["reason"])[6:] not in set(self.overlay.values()):
                    self.F.w("A11", "pause_unsupported", r.path, r.commit, "no exported run on another model")
            if e.get("event") == "RESUMED":
                subj = str(e.get("reason", ""))[len("clear:"):]
                if subj not in clears:
                    self.F.w("A11", "resume_unsupported", r.path, r.commit, "no owner clear row")
        for run_id in set(self.overlay.values()):
            if not any(r.rec.get("event") == "PAUSED" and r.rec.get("reason") == f"model:{run_id}" for r in evs):
                self.F.w("A11", "overlay_missing", "analyst/state/", "-", safe_detail("run", run_id))
        # STATE.json snapshot
        S = self.json_at_head("analyst/STATE.json")
        if S is None:
            return
        bad = []
        lpd = S.get("last_processed_day")
        upto = [t for t in trades if lpd and t["model_day"].isoformat() <= lpd]
        if S.get("n") != len(upto):
            bad.append("n")
        if not rel_eq(S.get("E"), stats.e_value([t["y"] for t in upto])):
            bad.append("E")
        if S.get("E_star") not in (400, 400.0):
            bad.append("E_star")
        promo = [r.rec.get("model_day") for r in evs if r.rec.get("event") == "PROMOTED"]
        post = [t["y"] for t in upto if promo and t["model_day"].isoformat() > promo[0]]
        if not rel_eq(S.get("K"), stats.k_value(post) if promo else 1.0):
            bad.append("K")
        if (S.get("first_day") or None) != (self.first_day.isoformat() if self.first_day else None):
            bad.append("first_day")
        latched = [r.rec.get("event") for r in evs if r.rec.get("event") in ("TESTING", "PROMOTED", "DEMOTED",
                                                                                 "RETIRED")]
        want_state = latched[-1] if latched else "SHAKEDOWN"
        if S.get("state") != want_state:
            bad.append("state")
        if upto:
            share = max(sum(1 for t in upto if t["side"] == 1), sum(1 for t in upto if t["side"] == -1)) / len(upto)
            if not rel_eq(S.get("side_share"), share) or S.get("side_label") != (share > 0.85):
                bad.append("side_share")
        known = self.terminals_at_last_change("analyst/STATE.json")
        last40 = [r for r in rows_expected if r["terminal_status"] != "skipped:holiday" and
                  (known is None or r["call_id"] in known)][-40:]
        if last40:
            ms = sum(1 for r in last40 if r["status"].startswith(("missed:", "no_call:"))) / len(last40)
            if not rel_eq(S.get("missed_share_40"), ms) and not (ms == 0 and S.get("missed_share_40") in (0, 0.0)):
                bad.append("missed_share_40")
            if S.get("paused_display") != (ms > 0.15):
                bad.append("paused_display")
        if bad:
            self.F.w("A11", "state_snapshot", "analyst/STATE.json", "-", safe_detail(*bad))

    # ================================================================ mirror vs source (A11)
    def check_mirror_source(self):
        if self.store_git is None:
            self.F.w("A0", "source_store_unavailable", "mirror/system/", "-",
                     "gold-forward-watch not readable: the mirror is not compared with its source")
            return
        sg = self.store_git
        shead = sg.head()
        bad = []
        md = self.bytes_at_head("mirror/system/days.csv")
        sd = sg.show(shead, "data/system/days.csv")
        if md is not None and (sd is None or not sd.startswith(md)):
            self.F.w("A11", "mirror_days", "mirror/system/days.csv", "-", "not a byte prefix of the source days.csv")
        ms = self.bytes_at_head("mirror/system/forecasts_slots.csv")
        sf = sg.show(shead, "data/system/forecasts.csv")
        if ms is not None:
            if sf is None:
                bad.append("forecasts")
            else:
                lines = sf.decode("utf-8").split("\n")
                keep = [lines[0]] + [ln for ln in lines[1:] if ln and ln.split(",")[1:2] and
                                     ln.split(",")[1] in ("9", "10", "15")]
                want = ("\n".join(keep) + "\n").encode("utf-8")
                if not want.startswith(ms):
                    self.F.w("A11", "mirror_slots", "mirror/system/forecasts_slots.csv", "-",
                             "not a prefix of the source's slot 9/10/15 lines")
        for p in self.git.ls_tree(self.head, "mirror/system/raw/"):
            src = "data/system/raw/" + p[len("mirror/system/raw/"):]
            a = self.bytes_at_head(p)
            b = sg.show(shead, src)
            if b is None or a != b:
                self.F.w("A11", "mirror_raw", p, self.commit_of(p), "differs from the source raw file")
        eb = self.bytes_at_head("mirror/exports.csv")
        if eb:
            try:
                for r in csv.DictReader(io.StringIO(eb.decode("utf-8"))):
                    if not sg.commit_exists(r.get("source_sha", "")):
                        self.F.w("A11", "mirror_export_sha", "mirror/exports.csv", "-",
                                 safe_detail("source_sha unknown", r.get("source_sha", "")[:12]))
            except Exception:  # noqa: BLE001
                self.F.w("A11", "mirror_export_sha", "mirror/exports.csv", "-", "unreadable")

    # ================================================================ frozen inputs (A11)
    def check_frozen(self):
        b = self.bytes_at_head("analyst/vendor/eproc.py")
        if b is None or sha256_bytes(b) != EPROC_SHA256:
            self.F.w("A11", "eproc_sha", "analyst/vendor/eproc.py", "-", "not the lab's frozen eproc.py (R-AN-7)")
        from .clock import BLS_SHA256, FOMC_SHA256
        for name, sha in (("bls_2026_2027.csv", BLS_SHA256), ("fomc_2026_2027.csv", FOMC_SHA256)):
            p = f"analyst/calendars/{name}"
            x = self.bytes_at_head(p)
            if x is None or sha256_bytes(x) != sha:
                self.F.w("A11", "calendar_sha", p, "-", "not R-V1-15's file")
        if self.holidays.table_error:
            self.F.w("A11", "holiday_table", "analyst/tables/holidays.csv", "-", self.holidays.table_error)
        else:
            miss, extra = self.holidays.mismatch_up_to_horizon()
            if miss or extra:
                self.F.w("A11", "holiday_table", "analyst/tables/holidays.csv", "-",
                         safe_detail(f"{len(miss)} register rows missing", f"{len(extra)} rows not in the register"))
        man = self.bytes_at_head("analyst/FROZEN_ANALYST.sha256")
        if man is not None:
            for line in man.decode("utf-8", "replace").split("\n"):
                if not line.strip():
                    continue
                sha, _, path = line.partition("  ")
                x = self.bytes_at_head(path)
                if x is None or sha256_bytes(x) != sha:
                    self.F.w("A11", "frozen_drift", path, "-", "file differs from FROZEN_ANALYST.sha256 at HEAD")
        else:
            self.F.w("A11", "frozen_drift", "analyst/FROZEN_ANALYST.sha256", "-", "manifest missing")
        for p in self.git.ls_tree(self.head):
            base = p.rsplit("/", 1)[-1]
            if base in ("CLAUDE.md", "AGENTS.md") or "/.claude/" in "/" + p:
                self.F.v("A3", "forbidden_file", p, self.commit_of(p), "R1(a): no CLAUDE.md, AGENTS.md or .claude/")

    # ================================================================ history (A7)
    def check_history(self):
        for sha, parent, changes in self.log:
            for st, p in changes:
                kind = st[:1]
                if _CREATE_ONLY.match(p) or _WRITE_ONCE.match(p):
                    if kind in ("M", "D", "T"):
                        self.F.v("A7", "modified" if kind != "D" else "deleted", p, sha,
                                 "create-only/write-once file changed on main")
                elif p in APPEND_ONLY:
                    if kind == "D":
                        self.F.v("A7", "append_break", p, sha, "append-only file deleted")
                    elif kind in ("M", "T") and parent:
                        old = self.git.show(parent, p) or b""
                        new = self.git.show(sha, p) or b""
                        if not new.startswith(old) or (old and not old.endswith(b"\n")):
                            self.F.v("A7", "append_break", p, sha, "not a whole-line append")
        if self.prev_anchor:
            if not self.git.commit_exists(self.prev_anchor) or not self.git.is_ancestor(self.prev_anchor, self.head):
                self.F.v("A7", "anchor", "reports/judge_analyst/VERDICTS.csv", self.prev_anchor,
                         "main does not descend from the judge's previous anchor")


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _in(s, lo, hi):
    try:
        t = parse_utc(s)
    except (TypeError, ValueError):
        return False
    return lo <= t <= hi


def _cmp_outcome(rec, out):
    if not isinstance(rec, dict):
        return ["missing"]
    mm = []
    want_keys = {"status", "P_e", "P_e_src", "P_e_minute_utc", "X", "Tg", "exit_minute_utc", "P_x", "reason", "fill",
                 "cost_usd", "gross_usd", "net_usd", "stress_net_usd", "R", "R_stress", "y"}
    if set(rec.keys()) != want_keys:
        mm.append("keys")
    if rec.get("status") != out["status"]:
        return mm + ["status"]
    if out["status"] != "final":
        for k in ("net_usd", "stress_net_usd", "R", "R_stress", "y", "cost_usd"):
            if not rel_eq(rec.get(k), out[k]) and not (out[k] == 0 and rec.get(k) in (0, 0.0)):
                mm.append(k)
        if rec.get("gross_usd") is not None and not rel_eq(rec.get("gross_usd"), out["net_usd"]):
            mm.append("gross_usd")
        return mm
    for k in ("P_e_src", "P_e_minute_utc", "exit_minute_utc", "reason", "fill"):
        if rec.get(k) != out[k]:
            mm.append(k)
    for k in ("P_e", "X", "Tg", "P_x"):
        if not price_eq(rec.get(k), out[k]):
            mm.append(k)
    for k in ("cost_usd", "gross_usd", "net_usd", "stress_net_usd", "R", "R_stress", "y"):
        a, b = rec.get(k), out[k]
        if not (rel_eq(a, b) or (isinstance(a, (int, float)) and abs(a - b) <= 1e-12)):
            mm.append(k)
    return mm


def _cmp_path(rec, ps):
    if not isinstance(rec, dict):
        return ["missing"]
    mm = []
    rb = rec.get("buckets")
    if not isinstance(rb, list) or len(rb) != 16:
        mm.append("buckets")
    else:
        for a, b in zip(rb, ps["buckets"]):
            if not isinstance(a, dict) or a.get("k") != b["k"] or a.get("n") != b["n"]:
                mm.append(f"buckets[{b['k']}]")
                continue
            if b["n"] and not all(price_eq(a.get(x), b[x]) for x in ("o", "h", "l", "c")):
                mm.append(f"buckets[{b['k']}]")
    for k in ("mfe_usd", "mae_usd", "range_mid_usd"):
        a, b = rec.get(k), ps[k]
        if not (rel_eq(a, b) or (a is not None and b is not None and abs(a - b) <= 1e-9)):
            mm.append(k)
    if rec.get("exit_after_min") not in (ps["exit_after_min"], ps["exit_after_min"] + 1):
        mm.append("exit_after_min")
    return mm
