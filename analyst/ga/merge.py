"""analyst-merge and the closer (prereg §3 Lock evidence, §1 Completeness, R1(c); register R-AN-32..R-AN-39,
R-AN-48, R-AN-49, §16.18 A6). FROZEN.

The job runs main's code only. Sessions push to the agent inbox (PINS.inbox_repo), never to gold-analyst; the job
finds their pushes in the inbox's activity log, read in full or not at all (R-AN-32), and fetches each pushed commit
from the public inbox as data, without gold-analyst's credential (`git fetch <inbox url> <after>`, `git diff`,
`git show`); nothing from it is executed, and the agent's commit is never merged: an accepted file is copied byte for
byte. Witness comments are posted on gold-analyst's pinned issue BEFORE the files are committed; only comments by
`github-actions[bot]` that were never edited count (R-AN-35), while seq counts every bot witness header.

Interpretations beyond the register (logged for its amendment): a witness carries `writes` [{path, sha256, record}]
so that a comment without its file can be completed exactly (record = the script-created object, null for a copied
agent file); `seq` is global across modes; a well-formed push for a window outside the mode's period is rejected with
`mode`; a lesson without an earlier accepted lesson attempt is rejected `lesson_cap`; a playbook without an accepted
revision attempt for its version is rejected `version`; with PINS.inbox_from_utc unset, no inbox entry is processed.

No push can halt the merge or the closer (register §16 note of 2026-10-23, review blocker B2): an orphan or unfetchable
commit, a fetch beyond its bound, a blob over 4 MiB, a path whose call_id names no window (an impossible date or a
weekend) and a non-regular file are `agent_scope`; an unexpected failure while an allowed file's content is checked,
before anything was written or posted, is `invalid_content`; a comment whose agent file cannot be reproduced gets a
second witness `invalid_content`. Only a failed activity read stops a run, before it writes anything (R-AN-39).
"""
import datetime as dt
import re
import subprocess

from . import clock, paths, plan, rec, records, tables, weekly, witness
from .ghapi import ActivityReadFailed
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
ANALYST_REF = "refs/heads/claude/analyst-"
SHA_RE = re.compile(r"[0-9a-f]{40}")
WINDOW_DAYS = 14                     # pushes older than this are not processed (R-AN-32)
FETCH_TIMEOUT_S = 180                # per git fetch of a pushed commit (R-AN-32, fetch bound)
FETCH_TRIES = 3
FETCH_RETRY_S = 20
MAX_BLOB = 4 * 1024 * 1024           # an allowed blob above this is agent_scope and never read
NO_AUTH = ("-c", "http.https://github.com/.extraheader=")   # drops the token actions/checkout persisted
TERMINAL_STATUSES = ("called", "stay_out", "no_call:refusal", "no_call:invalid", "no_call:data", "no_call:d_ref",
                     "no_call:late", "no_call:run", "missed:trigger", "missed:push", "skipped:holiday")


def patterns(prefix):
    p = re.escape(prefix)
    return [
        ("attempt", re.compile(rf"{p}attempts/(?P<draw>{CID}|lesson-{CID}|revision-v[1-9][0-9]*)_(?P<n>[12])\.json")),
        ("call", re.compile(rf"{p}calls/(?P<cid>{CID})\.json")),
        ("lesson", re.compile(rf"{p}lessons/(?P<cid>{CID})\.json")),
        ("revision", re.compile(rf"{p}playbooks/v(?P<k>[1-9][0-9]*)\.md")),
    ]


CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def classify_path(path):
    """(kind, mode, match) or (None, None, None). The whole path must match one pattern and hold no control character
    (R-AN-33 clarification A3 of 2026-10-23: `$` with re.match also matched before a trailing newline)."""
    if CONTROL_RE.search(path):
        return None, None, None
    for mode in ("canary", "live"):          # canary first: its prefix is longer
        for kind, rx in patterns(paths.prefix(mode)):
            m = rx.fullmatch(path)
            if m:
                return kind, mode, m
    return None, None, None


def names_a_window(ident):
    """False when the path's call_id (in an attempt's draw, or in a call or lesson path) names no window: an
    impossible date or a weekend (R-AN-4: windows exist on weekdays only). Such a path is outside the allowlist
    (register §16 note of 2026-10-23, review blocker B2). Revision paths carry no call_id."""
    draw = ident.get("draw") or ""
    cid = ident.get("cid") or (draw[len("lesson-"):] if draw.startswith("lesson-") else
                               (None if draw.startswith("revision-") else draw or None))
    if cid is None:
        return True
    try:
        d, _w = clock.parse_call_id(cid)
    except ValueError:
        return False
    return clock.is_weekday(d)


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
    """The checkout of main the job writes into, plus git access to pushed commits as data. `inbox_url` is where
    pushed commits are fetched from (the public inbox); `sleep` paces the fetch retries."""

    def __init__(self, repo_dir, runner=subprocess.run, inbox_url=None, sleep=lambda s: None):
        self.repo_dir = str(repo_dir)
        self.run = runner
        self.view = DirView(repo_dir)
        self.inbox_url = inbox_url
        self.sleep = sleep

    def git(self, *args, check=True, input_bytes=None, timeout=None, pre=()):
        argv = ["git", "-C", self.repo_dir, *pre, *args]
        p = self.run(argv, capture_output=True, input=input_bytes, timeout=timeout) if timeout else \
            self.run(argv, capture_output=True, input=input_bytes)
        if check and p.returncode != 0:
            raise RuntimeError(f"git {args[0]} failed: {p.stderr.decode(errors='replace')[-300:]}")
        return p

    def head(self):
        return self.git("rev-parse", "HEAD").stdout.decode().strip()

    def has_commit(self, sha):
        return self.git("cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0

    def fetch_commit(self, sha, ref=None):
        """Fetch a pushed commit from the inbox, as data and without credentials (R-AN-32): by its sha, else the
        push's ref into refs/analyst-fetch/<sha>. Each git fetch has FETCH_TIMEOUT_S; a failed try is repeated up to
        FETCH_TRIES times, FETCH_RETRY_S apart. A timeout, or the last failed try, raises: the commit cannot be
        fetched."""
        if not SHA_RE.fullmatch(sha or "") or not self.inbox_url:
            raise RuntimeError("the pushed commit cannot be fetched")
        if self.has_commit(sha):
            return
        specs = [[sha]] + ([[f"+{ref}:refs/analyst-fetch/{sha}"]] if ref else [])
        for i in range(FETCH_TRIES):
            for spec in specs:
                try:
                    self.git("fetch", "--quiet", "--no-tags", self.inbox_url, *spec, check=False,
                             timeout=FETCH_TIMEOUT_S, pre=NO_AUTH)
                except subprocess.TimeoutExpired:
                    raise RuntimeError("the fetch of the pushed commit timed out") from None
                if self.has_commit(sha):
                    return
            if i < FETCH_TRIES - 1:
                self.sleep(FETCH_RETRY_S)
        raise RuntimeError("the pushed commit cannot be fetched")

    def blob_size(self, sha, path):
        """The size of the blob at `path` in commit `sha` (`git cat-file -s`), or None."""
        p = self.git("cat-file", "-s", f"{sha}:{path}", check=False)
        try:
            return int(p.stdout.decode().strip()) if p.returncode == 0 else None
        except ValueError:
            return None

    def diff_entries(self, after):
        base = self.git("merge-base", "HEAD", after).stdout.decode().strip()
        out = self.git("diff", "--no-renames", "--name-status", "-z", base, after).stdout.decode("utf-8")
        parts = [x for x in out.split("\0") if x != ""]
        return [(parts[i], parts[i + 1]) for i in range(0, len(parts) - 1, 2)]

    def show(self, sha, path):
        """The bytes of the regular file at `path` in commit `sha`; None when it is missing or not a regular file (a
        symlink or a submodule entry: amended R-AN-33)."""
        ls = self.git("ls-tree", "-z", sha, "--", path, check=False)
        if ls.returncode != 0 or not ls.stdout.startswith((b"100644 blob ", b"100755 blob ")):
            return None
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
    def __init__(self, ws, api, now, run_info, sleep=lambda s: None, inbox=None, log=lambda s: None):
        self.ws, self.api, self.now, self.sleep = ws, api, now, sleep
        self.inbox = inbox                   # the inbox activity reader (ghapi.InboxActivity)
        self.log = log                       # one enum per line; never data
        self.run_info = run_info            # {workflow, id, attempt, event}
        self.view = ws.view
        self.mode_doc = paths.read_mode(self.view)
        self.mode = self.mode_doc["mode"]
        self.pins = paths.read_pins(self.view) or {}
        self.issue = self.pins.get("witness_issue")
        self.inbox_repo = self.pins.get("inbox_repo")
        self.inbox_from = self.pins.get("inbox_from_utc")
        if ws.inbox_url is None and self.inbox_repo:
            ws.inbox_url = f"https://github.com/{self.inbox_repo}.git"
        self.head_sha = ws.head()
        self.holidays = tables.load_holidays(self.view)
        self.acts = None                     # the inbox activity entries of the complete read
        self.activity_read = None            # token | anonymous | failed
        self._all = None
        self._comments = None
        self._file_afters = None
        self._seq = None
        self.tracks = []
        self.subjects = []
        self.seqs = []                       # the witness seqs whose files this run wrote
        self._run_created = None
        self._acting = False                 # set once a push's processing has written or posted anything

    # ------------------------------------------------------------ the inbox's activity (R-AN-32, R-AN-39 step 1)
    def read_inbox(self):
        """The complete read of the inbox's activity. It comes before anything is written, completed or posted: a
        failed read logs `activity_read=failed` and raises ActivityReadFailed, and the run stops there."""
        try:
            if self.inbox is None or not self.inbox_repo:
                raise ActivityReadFailed("no inbox")
            acts, how = self.inbox.read_activity()
            if not isinstance(acts, list):
                raise ActivityReadFailed("not a list")
        except Exception as e:  # noqa: BLE001 - any failure of the read is a failed read
            self.activity_read = "failed"
            self.log("activity_read=failed")
            if isinstance(e, ActivityReadFailed):
                raise
            raise ActivityReadFailed(type(e).__name__) from None
        self.acts, self.activity_read = acts, how
        self.log(f"activity_read={how}")
        if not self.inbox_from:
            self.log("inbox_from_utc=unset")
        return acts

    def pushes_to_process(self):
        """The inbox pushes to witness, in ascending (timestamp, id) order (R-AN-32, R-AN-34): entries whose ref
        starts refs/heads/claude/analyst-, whose after is a commit (not 40 zeros) and whose timestamp is at or after
        PINS.inbox_from_utc. A sha's entry is its earliest by (timestamp, id); it is processed when that time is
        within the last WINDOW_DAYS days and no counting witness names the sha. Probe refs (claude/probe-*), main and
        anything else are never read, so they can neither add a push nor give one an earlier time."""
        if not self.inbox_from:
            return []
        first = {}
        for e in self.acts or []:
            if not isinstance(e, dict):
                continue
            ref, after = e.get("ref"), e.get("after")
            if not isinstance(ref, str) or not ref.startswith(ANALYST_REF) or not isinstance(after, str) \
                    or after == ZERO40:
                continue
            try:
                t = rec.normalize_gh(e.get("timestamp"))
            except (ValueError, TypeError):
                continue
            if t < self.inbox_from:
                continue
            aid = e.get("id")
            key = (t, aid if isinstance(aid, int) and not isinstance(aid, bool) else -1)
            if after not in first or key < first[after][0]:
                first[after] = (key, e)
        lim = rec.iso(self.now() - dt.timedelta(days=WINDOW_DAYS))
        todo = [(key, e) for key, e in first.values() if key[0] >= lim and not self.witnessed_after(e["after"])]
        return [e for _key, e in sorted(todo, key=lambda x: x[0])]

    # ------------------------------------------------------------ comments, seq
    def _read_comments(self):
        """Every github-actions[bot] comment whose first line is a witness header (for seq, R-AN-35), and among them
        the ones that count: exactly two lines, a JSON payload, and updated_at equal to created_at (never edited)."""
        self._all, self._comments = [], []
        for c in self.api.comments(self.issue) if self.issue else []:
            if (c.get("user") or {}).get("login") != BOT:
                continue
            lines = (c.get("body") or "").split("\n")
            m = HEADER_RE.match(lines[0]) if lines else None
            if not m:
                continue
            self._all.append(int(m.group(3)))
            if len(lines) != 2:
                continue
            try:
                payload = rec.loads(lines[1])
                created = rec.normalize_gh(c["created_at"])
                edited = rec.normalize_gh(c.get("updated_at")) != created
            except (ValueError, TypeError, KeyError):
                continue
            if edited or not isinstance(payload, dict):
                continue
            self._comments.append({"id": c["id"], "created_utc": created, "seq": int(m.group(3)),
                                   "payload": payload, "body": c["body"]})

    def bot_comments(self):
        """The witness comments that count (R-AN-35)."""
        if self._comments is None:
            self._read_comments()
        return self._comments

    def bot_seqs(self):
        """The seqs of every bot witness header, edited or not."""
        if self._all is None:
            self._read_comments()
        return self._all

    def next_seq(self):
        if self._seq is None:
            seqs = [0]
            for mode in ("live", "canary"):
                seqs += [w.get("seq", 0) for w in witness.witnesses(self.view, mode)]
            seqs += self.bot_seqs()
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
        """True when a witness file on main at the run's start, or a counting witness comment (those posted or
        completed by this run included), names the push's sha."""
        if self._file_afters is None:
            self._file_afters = {(w.get("push") or {}).get("after") for mode in ("live", "canary")
                                 for w in witness.witnesses(self.view, mode)}
        if after in self._file_afters:
            return True
        return any((c["payload"].get("push") or {}).get("after") == after for c in self.bot_comments())

    # ------------------------------------------------------------ completion of comments without files
    def complete_from_comments(self):
        """Write the files of every counting bot witness comment (R-AN-35: never edited) that has no witness file
        yet (a run that failed after posting). A write that cannot be reproduced (the agent's commit can no longer be
        fetched, or its bytes differ) does not stop the run: the witness file is still written from its comment, and
        the push gets a second, rejected witness `invalid_content` naming the missing path (register §16 note of
        2026-10-23, review blocker B2)."""
        have = set()
        for mode in ("live", "canary"):
            have |= {w.get("seq") for w in witness.witnesses(self.view, mode)}
        unrecovered = []
        for c in list(self.bot_comments()):
            if c["seq"] in have:
                continue
            doc = dict(c["payload"])
            doc["comment"] = {"issue": self.issue, "id": c["id"], "created_utc": c["created_utc"],
                              "body_sha256": rec.sha256_bytes(c["body"].encode("utf-8"))}
            missing = []
            for wr in doc.get("writes") or []:
                if self.view.exists(wr["path"]):
                    continue
                try:
                    data = self._completion_bytes(doc, wr)
                except Exception:  # noqa: BLE001 - an unfetchable or malformed source is logged below
                    data = None
                if data is None or rec.sha256_bytes(data) != wr["sha256"]:
                    missing.append(wr["path"])
                    continue
                self.ws.write(wr["path"], data)
            mode = doc.get("mode") or "live"
            self.ws.write(paths.witness_path(mode, c["seq"]), rec.record_bytes(doc))
            self.seqs.append(c["seq"])
            self.subjects.append(f"complete seq {c['seq']}")
            if missing:
                unrecovered.append((mode, doc, missing[0]))
        posted = {((c["payload"].get("push") or {}).get("after"), c["payload"].get("path"))
                  for c in self.bot_comments() if c["payload"].get("reason") == "invalid_content"}
        for mode, doc, path in unrecovered:
            if ((doc.get("push") or {}).get("after"), path) in posted:
                continue                          # an earlier run already posted it, then failed to push
            self.reject(mode, doc.get("kind") or "push", doc.get("id"), "invalid_content", path, doc.get("push"))

    def _completion_bytes(self, doc, wr):
        if wr.get("record") is not None:
            return rec.record_bytes(wr["record"])
        after = (doc.get("push") or {}).get("after")
        self.ws.fetch_commit(after, (doc.get("push") or {}).get("ref"))
        return self.ws.show(after, wr["path"])

    # ------------------------------------------------------------ witness posting
    def post_witness(self, doc, mode):
        """Post the comment, then write the witness file and its writes. Returns the witness doc (with comment)."""
        self._acting = True
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
        self.bot_seqs().append(doc["seq"])
        for wr in doc["writes"]:
            if wr.get("record") is not None:
                self.ws.write(wr["path"], rec.record_bytes(wr["record"]))
        self.ws.write(paths.witness_path(mode, doc["seq"]), rec.record_bytes(doc))
        self.seqs.append(doc["seq"])
        return doc

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

    def closer(self):
        now = self.now()
        # 1. the complete read of the inbox's activity (a failed read raises here, before anything is written),
        #    then every unwitnessed claude/analyst-* push of the last 14 days, in (timestamp, id) order. A failure
        #    after a witness comment was posted raises too, so no window is closed over a push not yet witnessed.
        if self.acts is None:
            self.read_inbox()
        for e in self.pushes_to_process():
            self.process_push(e)
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

    # ------------------------------------------------------------ dispatches (R-AN-32, R-AN-40, R-AN-75 item 4)
    def dispatch_after_commit(self):
        """After a commit reached main: analyst-track (ref main, input call_id) for each buy/sell `called` live
        terminal this run wrote, then analyst-deploy (ref main) once. Both are display only, so a failed dispatch is
        logged and does not fail the run."""
        for cid in self.tracks:
            try:
                self.api.dispatch("analyst-track.yml", "main", {"call_id": cid})
            except Exception:  # noqa: BLE001
                self.log("dispatch_failed analyst-track")
        try:
            self.api.dispatch("analyst-deploy.yml", "main", {})
        except Exception:  # noqa: BLE001
            self.log("dispatch_failed analyst-deploy")

    # ------------------------------------------------------------ one push (R-AN-33)
    def reject(self, mode, kind, id_, reason, path, push, window=None, status="rejected"):
        doc = {"seq": self.next_seq(), "kind": kind, "id": id_, "status": status, "reason": reason, "path": path,
               "file_sha256": None, "push": push, "window": window, "terminal": None, "writes": []}
        self.post_witness(doc, mode)
        self.subjects.append(f"{kind} {id_ or '-'} {status} {reason or ''}".strip())
        return doc

    def process_push(self, entry):
        """Witness one inbox push, given its activity entry (R-AN-32, R-AN-34): its time is the entry's timestamp
        (`time_source` activity, `push.repo` the inbox). Whatever the push contains, this returns a witness and
        never raises because of it (review blocker B2): a push whose diff cannot be computed (an orphan or
        unfetchable commit, a fetch beyond its bound), a path outside the allowlist (a call_id naming no window
        included), a blob over 4 MiB or a path that is not a regular file is `agent_scope`; an unexpected failure
        while an allowed file's content is checked, before anything was written or posted, is `invalid_content`.
        Failures after a witness comment was posted (the API, the workspace) still raise: the next run completes
        that comment."""
        ref, before, after = entry.get("ref"), entry.get("before"), entry.get("after")
        if not after or after == ZERO40 or self.witnessed_after(after):
            return None
        t_utc, t_src = rec.normalize_gh(entry["timestamp"]), "activity"
        push = {"repo": self.inbox_repo, "ref": ref, "before": before, "after": after,
                "activity_id": entry.get("id"), "activity_type": entry.get("activity_type"), "activity_utc": t_utc,
                "actor": (entry.get("actor") or {}).get("login")}
        try:
            self.ws.fetch_commit(after, ref)
            entries = self.ws.diff_entries(after)
        except Exception:  # noqa: BLE001 - checked below: no diff means not exactly one allowed added file
            entries = None
        wmode = self.mode if self.mode in ("live", "canary") else "live"
        if entries is None:
            return self.reject(wmode, "push", None, "agent_scope", None, push)
        if len(entries) != 1 or entries[0][0] != "A" or entries[0][1].startswith(".github/"):
            p = entries[0][1] if len(entries) == 1 else None
            return self.reject(wmode, "push", None, "agent_scope", p, push)
        path = entries[0][1]
        kind, pmode, m = classify_path(path)
        if kind is None or not names_a_window(m.groupdict()):
            return self.reject(wmode, "push", None, "agent_scope", path, push)
        ident = m.groupdict()
        id_ = ident.get("draw") or ident.get("cid") or (f"v{ident['k']}" if ident.get("k") else None)
        if pmode != self.mode:
            return self.reject(wmode, kind, id_, "mode", path, push)
        mode = pmode
        size = self.ws.blob_size(after, path)
        if size is not None and size > MAX_BLOB:  # never read (R-AN-32, fetch bound)
            return self.reject(wmode, "push", None, "agent_scope", path, push)
        data = self.ws.show(after, path)
        if data is None:                          # not a regular file (a symlink or a submodule entry)
            return self.reject(wmode, "push", None, "agent_scope", path, push)
        sha = rec.sha256_bytes(data)
        on_main = self.view.read(path)
        if on_main is not None:
            if on_main == data:
                return self.reject(mode, kind, id_, "duplicate", path, push, status="duplicate")
            return self.reject(mode, kind, id_, "rewrite", path, push)
        handler = {"attempt": self._attempt, "call": self._call, "lesson": self._lesson,
                   "revision": self._revision}[kind]
        self._acting = False
        try:
            return handler(mode, path, ident, id_, data, sha, push, t_utc, t_src)
        except Exception:  # noqa: BLE001
            if self._acting:                      # a witness or a file is already out: never contradict it
                raise
            return self.reject(mode, kind, id_, "invalid_content", path, push)

    def _accept(self, mode, kind, id_, path, data, sha, push, window):
        self._acting = True
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
        elif not _derivable(doc):
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


def _derivable(doc):
    """plan.derivable, where a check that raises on the agent's content means "not derivable" (R-AN-36 step 4): the
    call witness is already out at this point, so the failure must end in the terminal record, not in an exception."""
    try:
        return bool(plan.derivable(doc))
    except Exception:  # noqa: BLE001
        return False


ALLOWED_WRITE_PREFIXES = ("analyst/attempts/", "analyst/calls/", "analyst/lessons/", "analyst/playbooks/",
                          "analyst/witness/", "analyst/terminal/", "analyst/revisions/", "analyst/START.json",
                          "analyst/canary/")


def run_merge(ws, api, now, run_info, inbox=None, sleep=lambda s: None, dispatch=True, log=lambda s: None):
    """One analyst-merge run (R-AN-32, §16.18 A6): the complete read of the inbox's activity (a failed read raises
    ActivityReadFailed before anything is written), the completion of comments without files, the closer (every
    unwitnessed push first, then the windows and the revision bookkeeping), one commit to main, and, only when that
    commit reached main, the dispatches. `inbox` defaults to `api` (a test double serving both)."""
    m = Merge(ws, api, now, run_info, sleep=sleep, inbox=api if inbox is None else inbox, log=log)
    m.read_inbox()
    m.complete_from_comments()
    m.closer()
    seqs = sorted(m.seqs)
    subject = f"analyst-merge seq {seqs[0]}-{seqs[-1]}" if seqs else "analyst-merge closer"
    if ws.commit_push(subject, ALLOWED_WRITE_PREFIXES, sleep=sleep) and dispatch:
        m.dispatch_after_commit()
    return m
