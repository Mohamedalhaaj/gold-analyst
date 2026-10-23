"""analyst-merge and the closer (prereg §3 Lock evidence, §1 Completeness, R1(c); register R-AN-32..R-AN-39,
R-AN-48, R-AN-49). FROZEN.

The job runs main's code only. A pushed commit is fetched as data (`git fetch origin <after>`, `git diff`,
`git show`); nothing from it is executed, and the agent's commit is never merged: an accepted file is copied byte for
byte. Witness comments are posted on the pinned issue BEFORE the files are committed; only comments by
`github-actions[bot]` count (the repository is public, so anyone may comment).

Interpretations beyond the register (logged for its amendment): a witness carries `writes` [{path, sha256, record}]
so that a comment without its file can be completed exactly (record = the script-created object, null for a copied
agent file); `seq` is global across modes; a well-formed push for a window outside the mode's period is rejected with
`mode`; a lesson without an earlier accepted lesson attempt is rejected `lesson_cap`; a playbook without an accepted
revision attempt for its version is rejected `version`.
"""
import datetime as dt
import re
import subprocess

from . import clock, paths, plan, rec, records, tables, weekly, witness
from .repo import DirView

WITNESS_SCHEMA = "analyst-witness/1"
TERMINAL_SCHEMA = "analyst-terminal/1"
START_SCHEMA = "analyst-start/1"
REVISION_SCHEMA = "analyst-revision/1"
ZERO40 = "0" * 40
BOT = "github-actions[bot]"
GRACE = dt.timedelta(minutes=20)
CID = r"[0-9]{4}-[0-9]{2}-[0-9]{2}_W[12]"
HEADER_RE = re.compile(r"^analyst-witness/1 (\S+) (\S+) seq ([0-9]+)$")
TERMINAL_STATUSES = ("called", "stay_out", "no_call:refusal", "no_call:invalid", "no_call:data", "no_call:d_ref",
                     "no_call:late", "no_call:run", "missed:trigger", "missed:push", "skipped:holiday")


def patterns(prefix):
    p = re.escape(prefix)
    return [
        ("attempt", re.compile(rf"^{p}attempts/(?P<draw>{CID}|lesson-{CID}|revision-v[1-9][0-9]*)_(?P<n>[12])\.json$")),
        ("call", re.compile(rf"^{p}calls/(?P<cid>{CID})\.json$")),
        ("lesson", re.compile(rf"^{p}lessons/(?P<cid>{CID})\.json$")),
        ("revision", re.compile(rf"^{p}playbooks/v(?P<k>[1-9][0-9]*)\.md$")),
    ]


def classify_path(path):
    """(kind, mode, match) or (None, None, None)."""
    for mode in ("canary", "live"):          # canary first: its prefix is longer
        for kind, rx in patterns(paths.prefix(mode)):
            m = rx.match(path)
            if m:
                return kind, mode, m
    return None, None, None


def expected_windows(mode_doc, mode, until):
    """The expected windows of `mode` with model day <= until, in window order."""
    _m, a, b = paths.mode_period(mode_doc, mode)
    if a is None:
        return []
    last = until if b is None else min(b, until)
    out = []
    for d in clock.days_range(a, last):
        out.extend(clock.windows_on(d))
    return out


class Workspace:
    """The checkout of main the job writes into, plus git access to pushed commits as data."""

    def __init__(self, repo_dir, runner=subprocess.run):
        self.repo_dir = str(repo_dir)
        self.run = runner
        self.view = DirView(repo_dir)

    def git(self, *args, check=True, input_bytes=None):
        p = self.run(["git", "-C", self.repo_dir, *args], capture_output=True, input=input_bytes)
        if check and p.returncode != 0:
            raise RuntimeError(f"git {args[0]} failed: {p.stderr.decode(errors='replace')[-300:]}")
        return p

    def head(self):
        return self.git("rev-parse", "HEAD").stdout.decode().strip()

    def fetch_commit(self, sha, ref=None):
        if self.git("fetch", "--quiet", "--no-tags", "origin", sha, check=False).returncode == 0:
            return
        if ref:
            self.git("fetch", "--quiet", "--no-tags", "origin", f"+{ref}:refs/analyst-fetch/{sha}", check=False)
        if self.git("cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode != 0:
            raise RuntimeError("the pushed commit cannot be fetched")

    def diff_entries(self, after):
        base = self.git("merge-base", "HEAD", after).stdout.decode().strip()
        out = self.git("diff", "--no-renames", "--name-status", "-z", base, after).stdout.decode("utf-8")
        parts = [x for x in out.split("\0") if x != ""]
        return [(parts[i], parts[i + 1]) for i in range(0, len(parts) - 1, 2)]

    def show(self, sha, path):
        p = self.git("show", f"{sha}:{path}", check=False)
        return p.stdout if p.returncode == 0 else None

    def write(self, path, data):
        from pathlib import Path
        p = Path(self.repo_dir) / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def commit_push(self, subject, allowed_prefixes, tries=5, sleep=None, add_root="analyst", modifiable=()):
        self.git("add", "-A", "--", add_root)
        st = self.git("diff", "--cached", "--name-status", "-z").stdout.decode("utf-8").split("\0")
        ent = [(st[i], st[i + 1]) for i in range(0, len(st) - 1, 2) if st[i]]
        staged = [p for _s, p in ent]
        bad = [p for s, p in ent if not any(p.startswith(a) for a in allowed_prefixes)
               or (s != "A" and p not in modifiable)]
        if bad:
            self.git("reset", "-q")
            raise RuntimeError(f"staged outside the allowed paths or a create-only file changed: {bad[:3]}")
        if not staged:
            return False
        self.git("-c", "user.name=github-actions[bot]",
                 "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                 "commit", "-q", "-m", subject)
        for i in range(tries):
            if self.git("pull", "-q", "--rebase", "origin", "main", check=False).returncode == 0 and \
                    self.git("push", "-q", "origin", "HEAD:main", check=False).returncode == 0:
                return True
            if sleep:
                sleep(20)
        raise RuntimeError("push to main failed")


class Merge:
    def __init__(self, ws, api, now, run_info, sleep=lambda s: None):
        self.ws, self.api, self.now, self.sleep = ws, api, now, sleep
        self.run_info = run_info            # {workflow, id, attempt, event}
        self.view = ws.view
        self.mode_doc = paths.read_mode(self.view)
        self.mode = self.mode_doc["mode"]
        self.pins = paths.read_pins(self.view) or {}
        self.issue = self.pins.get("witness_issue")
        self.head_sha = ws.head()
        self.holidays = tables.load_holidays(self.view)
        self._comments = None
        self._seq = None
        self.tracks = []
        self.subjects = []
        self._run_created = None

    # ------------------------------------------------------------ comments, seq
    def bot_comments(self):
        if self._comments is None:
            self._comments = []
            for c in self.api.comments(self.issue) if self.issue else []:
                if (c.get("user") or {}).get("login") != BOT:
                    continue
                lines = (c.get("body") or "").split("\n")
                m = HEADER_RE.match(lines[0]) if lines else None
                if not m or len(lines) != 2:
                    continue
                try:
                    payload = rec.loads(lines[1])
                except ValueError:
                    continue
                self._comments.append({"id": c["id"], "created_utc": rec.normalize_gh(c["created_at"]),
                                       "seq": int(m.group(3)), "payload": payload, "body": c["body"]})
        return self._comments

    def next_seq(self):
        if self._seq is None:
            seqs = [0]
            for mode in ("live", "canary"):
                seqs += [w.get("seq", 0) for w in witness.witnesses(self.view, mode)]
            seqs += [c["seq"] for c in self.bot_comments()]
            self._seq = max(seqs)
        self._seq += 1
        return self._seq

    def run_created(self):
        if self._run_created is None:
            try:
                self._run_created = rec.normalize_gh(self.api.run(self.run_info["id"])["created_at"])
            except Exception:  # noqa: BLE001
                self._run_created = rec.iso(self.now())
        return self._run_created

    def witnessed_after(self, after):
        for mode in ("live", "canary"):
            for w in witness.witnesses(self.view, mode):
                if (w.get("push") or {}).get("after") == after:
                    return True
        return any((c["payload"].get("push") or {}).get("after") == after for c in self.bot_comments())

    # ------------------------------------------------------------ completion of comments without files
    def complete_from_comments(self):
        have = set()
        for mode in ("live", "canary"):
            have |= {w.get("seq") for w in witness.witnesses(self.view, mode)}
        for c in self.bot_comments():
            if c["seq"] in have:
                continue
            doc = dict(c["payload"])
            doc["comment"] = {"issue": self.issue, "id": c["id"], "created_utc": c["created_utc"],
                              "body_sha256": rec.sha256_bytes(c["body"].encode("utf-8"))}
            for wr in doc.get("writes") or []:
                if self.view.exists(wr["path"]):
                    continue
                if wr.get("record") is not None:
                    data = rec.record_bytes(wr["record"])
                else:
                    after = (doc.get("push") or {}).get("after")
                    self.ws.fetch_commit(after, (doc.get("push") or {}).get("ref"))
                    data = self.ws.show(after, wr["path"])
                if data is None or rec.sha256_bytes(data) != wr["sha256"]:
                    raise RuntimeError(f"cannot complete witness seq {c['seq']}")
                self.ws.write(wr["path"], data)
            self.ws.write(paths.witness_path(doc.get("mode") or "live", c["seq"]), rec.record_bytes(doc))
            self.subjects.append(f"complete seq {c['seq']}")

    # ------------------------------------------------------------ witness posting
    def post_witness(self, doc, mode):
        """Post the comment, then write the witness file and its writes. Returns the witness doc (with comment)."""
        doc = dict(doc)
        doc.setdefault("writes", [])
        doc["schema"] = WITNESS_SCHEMA
        doc["mode"] = mode
        doc["run"] = {"workflow": self.run_info.get("workflow"), "id": self.run_info.get("id"),
                      "attempt": self.run_info.get("attempt"), "event": self.run_info.get("event"),
                      "created_utc": self.run_created()}
        doc["written_utc"] = rec.iso(self.now())
        doc.pop("comment", None)
        body = f"analyst-witness/1 {doc['kind']} {doc.get('id') or '-'} seq {doc['seq']}\n{rec.compact(doc)}"
        c = self.api.post_comment(self.issue, body)
        doc["comment"] = {"issue": self.issue, "id": c["id"], "created_utc": rec.normalize_gh(c["created_at"]),
                          "body_sha256": rec.sha256_bytes(body.encode("utf-8"))}
        self.bot_comments().append({"id": c["id"], "created_utc": doc["comment"]["created_utc"], "seq": doc["seq"],
                                    "payload": {k: v for k, v in doc.items() if k != "comment"}, "body": body})
        for wr in doc["writes"]:
            if wr.get("record") is not None:
                self.ws.write(wr["path"], rec.record_bytes(wr["record"]))
        self.ws.write(paths.witness_path(mode, doc["seq"]), rec.record_bytes(doc))
        return doc

    # ------------------------------------------------------------ server times (R-AN-34)
    def push_time(self, ref, after):
        entry = None
        for i in range(3):
            try:
                for e in self.api.activity(ref=ref):
                    if e.get("after") == after:
                        entry = e
                        break
            except Exception:  # noqa: BLE001
                entry = None
            if entry is not None:
                break
            if i < 2:
                self.sleep(20)
        if entry is None:
            return {"activity_id": None, "activity_type": None, "activity_utc": None, "actor": None}, \
                self.run_created(), "run_created"
        t = rec.normalize_gh(entry["timestamp"])
        return {"activity_id": entry.get("id"), "activity_type": entry.get("activity_type"), "activity_utc": t,
                "actor": (entry.get("actor") or {}).get("login")}, t, "activity"

    # ------------------------------------------------------------ chain and terminal (R-AN-36, R-AN-37)
    def prev_chain(self, mode, w):
        exp = expected_windows(self.mode_doc, mode, w.day)
        keys = [x.key() for x in exp]
        if w.key() not in keys:
            return rec.ZERO64
        i = keys.index(w.key())
        if i == 0:
            return rec.ZERO64
        prev = exp[i - 1]
        b = self.view.read(paths.terminal_path(mode, prev.call_id))
        if b is None:
            self.close_window(mode, prev)
            b = self.view.read(paths.terminal_path(mode, prev.call_id))
        return rec.sha256_bytes(b)

    def attempts_of(self, mode, call_id):
        out = []
        for a in witness.accepted_attempts(self.view, mode, call_id):
            out.append({"n": witness.attempt_n_of(a), "path": a["path"], "sha256": a["file_sha256"],
                        "time_utc": a["window"]["time_utc"], "void": a["window"]["void"], "witness_seq": a["seq"]})
        return out

    def write_terminal(self, mode, w, status, detail, writer, call_info=None, push=None):
        if status not in TERMINAL_STATUSES:
            raise ValueError(status)
        doc = {"schema": TERMINAL_SCHEMA, "call_id": w.call_id, "mode": mode, "window": w.name,
               "model_day": w.day.isoformat(), "entry_utc": rec.iso(w.entry_utc),
               "deadline_utc": rec.iso(w.deadline_utc), "status": status, "detail": detail, "writer": writer,
               "call": call_info, "attempts": self.attempts_of(mode, w.call_id),
               "prev_chain_sha256": self.prev_chain(mode, w), "written_utc": rec.iso(self.now()),
               "run_id": self.run_info.get("id"), "head_sha": self.head_sha}
        tbytes = rec.record_bytes(doc)
        tpath = paths.terminal_path(mode, w.call_id)
        writes = [{"path": tpath, "sha256": rec.sha256_bytes(tbytes), "record": doc}]
        if mode == "live" and status in ("called", "stay_out") and not self.view.exists(paths.START_PATH):
            start = self.start_record(w, call_info, doc, rec.sha256_bytes(tbytes))
            writes.append({"path": paths.START_PATH, "sha256": rec.sha256_bytes(rec.record_bytes(start)),
                           "record": start})
        wdoc = {"seq": self.next_seq(), "kind": "terminal", "id": w.call_id, "status": "accepted", "reason": None,
                "path": tpath, "file_sha256": rec.sha256_bytes(tbytes), "push": push,
                "window": {"entry_utc": rec.iso(w.entry_utc), "deadline_utc": rec.iso(w.deadline_utc),
                           "time_utc": (call_info or {}).get("lock_utc"),
                           "time_source": (call_info or {}).get("time_source"), "void": False},
                "terminal": {"status": status, "detail": detail, "terminal_sha256": rec.sha256_bytes(tbytes),
                             "prev_chain_sha256": doc["prev_chain_sha256"],
                             "call_sha256": (call_info or {}).get("sha256"),
                             "push_sha": (call_info or {}).get("push_sha")},
                "writes": writes}
        self.post_witness(wdoc, mode)
        if mode == "live" and status == "called":
            self.tracks.append(w.call_id)
        self.subjects.append(f"terminal {w.call_id} {status}")
        return doc

    def start_record(self, w, call_info, tdoc, tsha):
        d0 = w.day
        m = d0 + dt.timedelta(days=1)
        while not (m.weekday() == 0 and clock.busday_count(d0, m) >= 3):
            m += dt.timedelta(days=1)
        return {"schema": START_SCHEMA, "freeze_tag": self.pins.get("freeze_tag"), "first_call_id": w.call_id,
                "d0": d0.isoformat(), "first_call_lock_utc": (call_info or {}).get("lock_utc"),
                "first_day": m.isoformat(),
                "rule": "first Monday m > d0 with numpy.busday_count(d0, m) >= 3", "terminal_sha256": tsha,
                "written_utc": tdoc["written_utc"], "run_id": tdoc["run_id"], "head_sha": tdoc["head_sha"]}

    # ------------------------------------------------------------ the closer (R-AN-39)
    def refusal_detail(self, w):
        if self.mode == "off":
            return "mode_off"
        if not self.holidays.has_table(w.day):
            return "no_table"
        st = self.view.read_json(paths.STATE_PATH)
        if st is not None and st.get("calls_enabled") is False:
            return "calls_disabled:" + ",".join(st.get("pause_reasons") or [])
        return None

    def close_window(self, mode, w):
        if self.view.exists(paths.terminal_path(mode, w.call_id)):
            return
        if self.holidays.has_table(w.day) and self.holidays.is_holiday(w):
            self.write_terminal(mode, w, "skipped:holiday", None, "closer")
        elif self.attempts_of(mode, w.call_id):
            self.write_terminal(mode, w, "no_call:run", None, "closer")
        else:
            self.write_terminal(mode, w, "missed:trigger", self.refusal_detail(w), "closer")

    def closer(self, skip_after=None):
        now = self.now()
        # 1. unwitnessed claude/analyst-* pushes of the last 14 days
        try:
            acts = self.api.activity(time_period="month")
        except Exception:  # noqa: BLE001
            acts = []
        lim = rec.iso(now - dt.timedelta(days=14))
        todo = []
        for e in acts:
            ref = e.get("ref") or ""
            after = e.get("after") or ZERO40
            if not ref.startswith("refs/heads/claude/analyst-") or after == ZERO40 or after == skip_after:
                continue
            if rec.normalize_gh(e["timestamp"]) < lim or self.witnessed_after(after):
                continue
            todo.append((rec.normalize_gh(e["timestamp"]), ref, e.get("before"), after))
        for _t, ref, before, after in sorted(todo):
            self.process_push(ref, before, after)
        # 2. expected windows past deadline + 20 min without a terminal: the current mode's, and a finished
        #    canary period's (canary_to set) after the switch to live, so no canary window stays open
        for mode in self.closing_modes():
            for w in expected_windows(self.mode_doc, mode, now.date()):
                if now >= w.deadline_utc + GRACE and not self.view.exists(paths.terminal_path(mode, w.call_id)):
                    self.close_window(mode, w)
        # 3. revision bookkeeping
        self.skip_revisions(self.mode if self.mode != "off" else paths.mode_period(self.mode_doc)[0], now)

    def closing_modes(self):
        out = []
        if self.mode_doc.get("canary_from") and (self.mode == "canary" or self.mode_doc.get("canary_to")):
            out.append("canary")
        if self.mode_doc.get("live_from") and self.mode in ("live", "off"):
            out.append("live")
        if self.mode == "off" and not out:
            out.append(paths.mode_period(self.mode_doc)[0])
        return out

    def skip_revisions(self, mode, now):
        _m, a, b = paths.mode_period(self.mode_doc, mode)
        if a is None:
            return
        sat = a + dt.timedelta(days=(5 - a.weekday()) % 7)
        while True:
            sunday = dt.datetime.combine(sat + dt.timedelta(days=1), dt.time(0), tzinfo=rec.UTC)
            if now < sunday or (b is not None and sat - dt.timedelta(days=5) > b):
                break
            iw = clock.iso_week(sat)
            p = paths.revision_record_path(mode, iw)
            if not self.view.exists(p):
                doc = {"schema": REVISION_SCHEMA, "iso_week": iw, "status": "skipped", "version": None,
                       "playbook_sha256": None, "effective_from": None, "written_utc": rec.iso(now),
                       "writer": "closer"}
                self.ws.write(p, rec.record_bytes(doc))
                self.subjects.append(f"revision {iw} skipped")
            sat += dt.timedelta(days=7)

    # ------------------------------------------------------------ one push (R-AN-33)
    def reject(self, mode, kind, id_, reason, path, push, window=None, status="rejected"):
        doc = {"seq": self.next_seq(), "kind": kind, "id": id_, "status": status, "reason": reason, "path": path,
               "file_sha256": None, "push": push, "window": window, "terminal": None, "writes": []}
        self.post_witness(doc, mode)
        self.subjects.append(f"{kind} {id_ or '-'} {status} {reason or ''}".strip())
        return doc

    def process_push(self, ref, before, after):
        if after == ZERO40 or self.witnessed_after(after):
            return None
        self.ws.fetch_commit(after, ref)
        entries = self.ws.diff_entries(after)
        pinfo, t_utc, t_src = self.push_time(ref, after)
        push = {"ref": ref, "before": before, "after": after, **pinfo}
        wmode = self.mode if self.mode in ("live", "canary") else "live"
        if len(entries) != 1 or entries[0][0] != "A" or entries[0][1].startswith(".github/"):
            p = entries[0][1] if len(entries) == 1 else None
            return self.reject(wmode, "push", None, "agent_scope", p, push)
        path = entries[0][1]
        kind, pmode, m = classify_path(path)
        if kind is None:
            return self.reject(wmode, "push", None, "agent_scope", path, push)
        ident = m.groupdict()
        id_ = ident.get("draw") or ident.get("cid") or (f"v{ident['k']}" if ident.get("k") else None)
        if pmode != self.mode:
            return self.reject(wmode, kind, id_, "mode", path, push)
        mode = pmode
        data = self.ws.show(after, path)
        sha = rec.sha256_bytes(data)
        on_main = self.view.read(path)
        if on_main is not None:
            if on_main == data:
                return self.reject(mode, kind, id_, "duplicate", path, push, status="duplicate")
            return self.reject(mode, kind, id_, "rewrite", path, push)
        handler = {"attempt": self._attempt, "call": self._call, "lesson": self._lesson,
                   "revision": self._revision}[kind]
        return handler(mode, path, ident, id_, data, sha, push, t_utc, t_src)

    def _accept(self, mode, kind, id_, path, data, sha, push, window):
        doc = {"seq": self.next_seq(), "kind": kind, "id": id_, "status": "accepted", "reason": None, "path": path,
               "file_sha256": sha, "push": push, "window": window, "terminal": None,
               "writes": [{"path": path, "sha256": sha, "record": None}]}
        self.ws.write(path, data)
        out = self.post_witness(doc, mode)
        self.subjects.append(f"{kind} {id_} seq {out['seq']}")
        return out

    def _in_period(self, mode, day):
        _m, a, b = paths.mode_period(self.mode_doc, mode)
        return a is not None and day >= a and (b is None or day <= b)

    def _attempt(self, mode, path, ident, id_, data, sha, push, t_utc, t_src):
        n = int(ident["n"])
        draw = ident["draw"]
        try:
            doc = rec.loads(data)
        except ValueError:
            doc = None
        if not records.valid_attempt(doc) or doc["draw_id"] != draw or doc["n"] != n or doc["mode"] != mode:
            return self.reject(mode, "attempt", draw, "invalid_content", path, push)
        acc = witness.accepted_attempts(self.view, mode, draw)
        if draw.startswith("lesson-"):
            cid = draw[len("lesson-"):]
            if doc["kind"] != "lesson" or doc["call_id"] != cid:
                return self.reject(mode, "attempt", draw, "invalid_content", path, push)
            if not self._settled_exists(mode, cid):
                return self.reject(mode, "attempt", draw, "no_settled", path, push)
            if len(acc) >= 2:
                return self.reject(mode, "attempt", draw, "lesson_cap", path, push)
            window = {"entry_utc": None, "deadline_utc": None, "time_utc": t_utc, "time_source": t_src,
                      "void": False}
        elif draw.startswith("revision-"):
            if doc["kind"] != "revision" or doc["call_id"] is not None:
                return self.reject(mode, "attempt", draw, "invalid_content", path, push)
            if len(acc) >= 2:
                return self.reject(mode, "attempt", draw, "attempt_cap", path, push)
            window = {"entry_utc": None, "deadline_utc": None, "time_utc": t_utc, "time_source": t_src,
                      "void": False}
        else:
            if doc["kind"] != "call" or doc["call_id"] != draw:
                return self.reject(mode, "attempt", draw, "invalid_content", path, push)
            w = clock.window_of(draw)
            if not self._in_period(mode, w.day):
                return self.reject(mode, "attempt", draw, "mode", path, push)
            window = {"entry_utc": rec.iso(w.entry_utc), "deadline_utc": rec.iso(w.deadline_utc),
                      "time_utc": t_utc, "time_source": t_src,
                      "void": t_utc < rec.iso(w.span_start_utc) or t_utc > rec.iso(w.deadline_utc)}
            if self.view.exists(paths.terminal_path(mode, draw)):
                return self.reject(mode, "attempt", draw, "window_closed", path, push, window=window)
            if len(acc) >= 2:
                return self.reject(mode, "attempt", draw, "attempt_cap", path, push, window=window)
            if n != len(acc) + 1:
                return self.reject(mode, "attempt", draw, "attempt_n", path, push, window=window)
        if draw.startswith(("lesson-", "revision-")) and n != len(acc) + 1:
            return self.reject(mode, "attempt", draw, "attempt_n", path, push, window=window)
        return self._accept(mode, "attempt", draw, path, data, sha, push, window)

    def _call(self, mode, path, ident, cid, data, sha, push, t_utc, t_src):
        try:
            doc = rec.loads(data)
        except ValueError:
            doc = None
        if not records.valid_call(doc) or doc["call_id"] != cid or doc["mode"] != mode:
            return self.reject(mode, "call", cid, "invalid_content", path, push)
        w = clock.window_of(cid)
        if not self._in_period(mode, w.day):
            return self.reject(mode, "call", cid, "mode", path, push)
        window = {"entry_utc": rec.iso(w.entry_utc), "deadline_utc": rec.iso(w.deadline_utc), "time_utc": t_utc,
                  "time_source": t_src, "void": False}
        if self.view.exists(paths.terminal_path(mode, cid)):
            return self.reject(mode, "call", cid, "window_closed", path, push, window=window)
        acc_w = self._accept(mode, "call", cid, path, data, sha, push, window)
        att = [a for a in self.attempts_of(mode, cid) if a["n"] == doc["attempt_n"]]
        if t_utc > rec.iso(w.deadline_utc):
            status, detail = "no_call:late", None
        elif not att or not (att[0]["time_utc"] < t_utc):
            status, detail = "no_call:invalid", "no_attempt"
        elif att[0]["void"]:
            status, detail = "no_call:invalid", "void_attempt"
        elif not plan.derivable(doc):
            status, detail = "no_call:invalid", "underivable"
        else:
            status, detail = doc["status"], doc.get("detail")
        info = {"path": path, "sha256": sha, "plan_status": doc["status"], "lock_utc": t_utc, "time_source": t_src,
                "push_sha": push["after"], "witness_seq": acc_w["seq"]}
        self.write_terminal(mode, w, status, detail, "merge", call_info=info, push=push)
        return acc_w

    def _settled_exists(self, mode, cid):
        if mode == "canary":
            fx = self.view.read_json(paths.CANARY_SETTLED_FIXTURE)
            return bool(fx) and fx.get("call_id") == cid
        return self.view.exists(paths.settled_path(cid))

    def _lesson(self, mode, path, ident, cid, data, sha, push, t_utc, t_src):
        try:
            doc = rec.loads(data)
        except ValueError:
            doc = None
        if not records.valid_lesson(doc) or doc["call_id"] != cid or doc["mode"] != mode:
            return self.reject(mode, "lesson", cid, "invalid_content", path, push)
        if not self._settled_exists(mode, cid):
            return self.reject(mode, "lesson", cid, "no_settled", path, push)
        acc = [a for a in witness.accepted_attempts(self.view, mode, f"lesson-{cid}")
               if witness.attempt_n_of(a) == doc["attempt_n"] and a["window"]["time_utc"] < t_utc]
        if not acc or doc["attempt_n"] > 2:
            return self.reject(mode, "lesson", cid, "lesson_cap", path, push)
        window = {"entry_utc": None, "deadline_utc": None, "time_utc": t_utc, "time_source": t_src, "void": False}
        return self._accept(mode, "lesson", cid, path, data, sha, push, window)

    def _revision(self, mode, path, ident, vid, data, sha, push, t_utc, t_src):
        k = int(ident["k"])
        t = rec.parse_iso(t_utc)
        if t.weekday() != 5:
            return self.reject(mode, "revision", vid, "not_saturday", path, push)
        iw = clock.iso_week(t.date())
        if self.view.exists(paths.revision_record_path(mode, iw)):
            return self.reject(mode, "revision", vid, "week_done", path, push)
        if k != weekly.newest_playbook(self.view, mode) + 1:
            return self.reject(mode, "revision", vid, "version", path, push)
        if not [a for a in witness.accepted_attempts(self.view, mode, f"revision-v{k}")
                if a["window"]["time_utc"] < t_utc]:
            return self.reject(mode, "revision", vid, "version", path, push)
        ok, _why = weekly.week_complete(self.view, mode, t.date())
        if not ok:
            return self.reject(mode, "revision", vid, "incomplete_week", path, push)
        if weekly.lint_playbook(data, k):
            return self.reject(mode, "revision", vid, "invalid_content", path, push)
        window = {"entry_utc": None, "deadline_utc": None, "time_utc": t_utc, "time_source": t_src, "void": False}
        out = self._accept(mode, "revision", vid, path, data, sha, push, window)
        monday = t.date() + dt.timedelta(days=2)
        rdoc = {"schema": REVISION_SCHEMA, "iso_week": iw, "status": "written", "version": k,
                "playbook_sha256": sha, "effective_from": monday.isoformat(), "written_utc": rec.iso(self.now()),
                "writer": "merge"}
        self.ws.write(paths.revision_record_path(mode, iw), rec.record_bytes(rdoc))
        return out


ALLOWED_WRITE_PREFIXES = ("analyst/attempts/", "analyst/calls/", "analyst/lessons/", "analyst/playbooks/",
                          "analyst/witness/", "analyst/terminal/", "analyst/revisions/", "analyst/START.json",
                          "analyst/canary/")


def run_merge(ws, api, now, run_info, event, sleep=lambda s: None, dispatch_track=True):
    """One analyst-merge run. event = {ref, before, after} of the triggering push."""
    m = Merge(ws, api, now, run_info, sleep=sleep)
    m.complete_from_comments()
    m.closer(skip_after=event.get("after"))
    w = None
    if event.get("after") and event["after"] != ZERO40:
        w = m.process_push(event["ref"], event.get("before"), event["after"])
    subject = "analyst-merge " + (f"{w['kind']} {w.get('id') or '-'} seq {w['seq']}" if w else "closer")
    ws.commit_push(subject, ALLOWED_WRITE_PREFIXES, sleep=sleep)
    if dispatch_track:
        for cid in m.tracks:
            try:
                api.dispatch("analyst-track.yml", "main", {"call_id": cid})
            except Exception:  # noqa: BLE001 - the tracker is display only
                pass
    return m
