"""analyst-merge and analyst-settle against the agent inbox (register §16.18 A6; R-AN-32, R-AN-34, R-AN-35, R-AN-39,
R-AN-40, R-AN-75 item 4).

- The inbox's activity is read in full or not at all: every page, the token first and then once without it, bounded
  tries. A failed read stops the run before anything is written, posted, committed or dispatched (review blocker B1).
- A push's time is its earliest inbox activity entry on a `claude/analyst-*` ref with timestamp >= inbox_from_utc.
- A pushed commit is fetched from the inbox as data, without gold-analyst's credential, within 180 s and 3 tries; a
  blob over 4 MiB is never read. Either bound gives `agent_scope`, never a halted run.
- An edited witness comment counts for nothing except seq.
- The closer witnesses every listed push before it closes a window.
- Probe pushes (`claude/probe-*`), however many, are never read.
- analyst-deploy is dispatched once after a run whose commit reached main, and never otherwise.
"""
import json
import subprocess

import pytest

from ga import ghapi, merge, rec, records, settle_run
from ga.repo import DirView
from harness import PLAN, Harness
from synth import INBOX_REPO, git

W1 = "2026-11-10_W1"
CANARY_ATT = "analyst/canary/attempts/2026-11-10_W1_1.json"
API = "https://api.github.com"
FIRST = f"{API}/repos/{INBOX_REPO}/activity?per_page=100&time_period=month"


def attempt_bytes(h, draw=W1, n=1):
    return rec.record_bytes(records.attempt("call", "canary", draw, n, draw, "x" * 40,
                                            records.frozen_block(DirView(h.world.seed)), {"note": "x"}))


# ---------------------------------------------------------------- the complete read (ghapi.InboxActivity)
class FakeHTTP:
    """Scripted answers: script(url, token, n) -> (status, link, body bytes)."""

    def __init__(self, script):
        self.script = script
        self.calls = []

    def __call__(self, method, url, token, timeout):
        self.calls.append((method, url, token, timeout))
        return self.script(url, token, len(self.calls))


def page(entries, nxt=None):
    return 200, (f'<{nxt}>; rel="next"' if nxt else ""), json.dumps(entries).encode()


def reader(script, token="tok"):
    sleeps = []
    http = FakeHTTP(script)
    return ghapi.InboxActivity(INBOX_REPO, token=token, http=http, sleep=sleeps.append), http, sleeps


def test_complete_read_follows_every_page_with_the_token():
    p2, p3 = f"{API}/repositories/1/activity?after=a", f"{API}/repositories/1/activity?after=b"
    pages = {FIRST: page([{"id": 1}], p2), p2: page([{"id": 2}], p3), p3: page([{"id": 3}])}
    r, http, sleeps = reader(lambda url, tok, n: pages[url])
    acts, how = r.read_activity()
    assert [a["id"] for a in acts] == [1, 2, 3] and how == "token"
    assert [c[1] for c in http.calls] == [FIRST, p2, p3]
    assert all(c[0] == "GET" and c[2] == "tok" and c[3] == 30 for c in http.calls) and sleeps == []


@pytest.mark.parametrize("bad", [(0, "", b""), (502, "", b""), (429, "", b""), (200, "", b'{"message": "x"}'),
                                 (200, "", b"not json")])
def test_a_failed_try_is_repeated_20_s_apart(bad):
    answers = [bad, bad, page([{"id": 1}])]
    r, http, sleeps = reader(lambda url, tok, n: answers[n - 1])
    acts, how = r.read_activity()
    assert acts == [{"id": 1}] and how == "token" and sleeps == [20, 20] and len(http.calls) == 3


@pytest.mark.parametrize("case", ["first_page", "second_page", "non_list"])
def test_three_failed_tries_fail_the_whole_read(case):
    p2 = f"{API}/repositories/1/activity?after=a"

    def script(url, tok, n):
        if case == "first_page":
            return 503, "", b""
        if case == "non_list":
            return 200, "", b'{"entries": []}'
        return page([{"id": 1}], p2) if url == FIRST else (500, "", b"")
    r, http, sleeps = reader(script)
    with pytest.raises(ghapi.ActivityReadFailed):
        r.read_activity()
    assert len(http.calls) == (4 if case == "second_page" else 3) and sleeps == [20, 20]


@pytest.mark.parametrize("refusal", [401, 403, 404])
def test_a_token_refusal_restarts_the_read_once_without_the_token(refusal):
    p2 = f"{API}/repositories/1/activity?after=a"

    def script(url, tok, n):
        if tok and url == p2:
            return refusal, "", b""                         # the token is refused on the second page
        return page([{"id": 1}], p2) if url == FIRST else page([{"id": 2}])
    r, http, sleeps = reader(script)
    acts, how = r.read_activity()
    assert how == "anonymous" and [a["id"] for a in acts] == [1, 2]        # page 1 not counted twice
    assert [(c[1], c[2]) for c in http.calls] == [(FIRST, "tok"), (p2, "tok"), (FIRST, None), (p2, None)]


def test_the_fallback_happens_at_most_once():
    r, http, _ = reader(lambda url, tok, n: (403, "", b""))
    with pytest.raises(ghapi.ActivityReadFailed):
        r.read_activity()
    assert [c[2] for c in http.calls] == ["tok", None]


def test_without_a_token_the_read_is_anonymous():
    r, http, _ = reader(lambda url, tok, n: page([{"id": 1}]), token="")
    assert r.read_activity() == ([{"id": 1}], "anonymous") and http.calls[0][2] is None


@pytest.mark.parametrize("status", [400, 410, 422])
def test_another_client_error_fails_at_once(status):
    r, http, sleeps = reader(lambda url, tok, n: (status, "", b""))
    with pytest.raises(ghapi.ActivityReadFailed):
        r.read_activity()
    assert len(http.calls) == 1 and sleeps == []


def test_a_next_link_off_the_api_host_fails_the_read():
    r, _http, _ = reader(lambda url, tok, n: page([{"id": 1}], "https://example.com/x"))
    with pytest.raises(ghapi.ActivityReadFailed):
        r.read_activity()


def test_no_inbox_repository_fails_the_read():
    with pytest.raises(ghapi.ActivityReadFailed):
        ghapi.InboxActivity(None, token="t", http=FakeHTTP(lambda *a: page([]))).read_activity()


# ---------------------------------------------------------------- a failed read stops the run (B1)
def test_a_failed_read_stops_merge_before_anything_is_written(tmp_path):
    h = Harness(tmp_path)
    assert h.run("start") == 0
    h.register_pushes()
    h.at(2026, 11, 10, 8, 10)                                  # W1 is past deadline + 20 min
    before = h.world.main_sha()
    h.api.read_fail = 1
    with pytest.raises(ghapi.ActivityReadFailed):
        h.merge()
    assert h.world.main_sha() == before and h.api.issue_comments == [] and h.api.dispatched == []
    assert "activity_read=failed" in h.merge_log
    h.merge()                                                  # the next run retries
    assert "activity_read=token" in h.merge_log
    assert h.main_json(CANARY_ATT) is not None
    assert h.main_json(f"analyst/canary/terminal/{W1}.json")["status"] == "no_call:run"


def test_a_failed_read_stops_settle_before_any_record(tmp_path):
    h = Harness(tmp_path, mode="live", canary_from=None, live_from="2026-11-10")
    h.at(2026, 11, 10, 18, 0)
    before = h.world.main_sha()
    h.api.read_fail = 1
    log = []
    with pytest.raises(ghapi.ActivityReadFailed):
        settle_run.run_settle(h.workspace("settle"), h.api, h.now, {"workflow": "analyst-settle", "id": 7,
                                                                   "attempt": 1, "event": "schedule"}, log=log.append)
    assert h.world.main_sha() == before and h.api.dispatched == [] and "activity_read=failed" in log
    assert h.world.main_list("analyst/terminal/") == [] and h.world.main_file("analyst/STATE.json") is None
    settle_run.run_settle(h.workspace("settle"), h.api, h.now, {"workflow": "analyst-settle", "id": 8,
                                                               "attempt": 1, "event": "schedule"})
    assert len(h.world.main_list("analyst/terminal/")) == 2


def test_the_job_guards():
    import merge_job
    import settle_job
    assert merge_job.guard({"GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "schedule"})
    assert merge_job.guard({"GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "workflow_dispatch"})
    assert not merge_job.guard({"GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "push"})
    assert not merge_job.guard({"GITHUB_REF": "refs/heads/claude/analyst-x", "GITHUB_EVENT_NAME": "schedule"})
    assert not merge_job.guard({})
    for ev in ("push", "schedule", "workflow_dispatch"):
        assert settle_job.guard({"GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": ev})
    assert not settle_job.guard({"GITHUB_REF": "refs/heads/claude/x", "GITHUB_EVENT_NAME": "workflow_dispatch"})


# ---------------------------------------------------------------- the push and its time (R-AN-34)
def test_the_witness_names_the_inbox_and_the_activity_time(tmp_path):
    h = Harness(tmp_path)
    assert h.run("start") == 0
    h.at(2026, 11, 10, 7, 31)                                   # the merge polls 11 minutes later
    h.register_pushes(t=h.at(2026, 11, 10, 7, 20, 7))
    h.at(2026, 11, 10, 7, 31)
    h.merge()
    w = h.last_witness()
    assert w["push"]["repo"] == INBOX_REPO and w["push"]["activity_utc"] == "2026-11-10T07:20:07Z"
    assert (w["window"]["time_utc"], w["window"]["time_source"]) == ("2026-11-10T07:20:07Z", "activity")
    assert w["run"]["event"] == "schedule" and w["run"]["created_utc"] == "2026-11-10T07:31:00Z"


def test_the_earliest_analyst_entry_gives_the_time_and_other_refs_do_not_count(tmp_path):
    h = Harness(tmp_path)
    assert h.run("start") == 0
    [(ref, sha)] = h.register_pushes(t=h.at(2026, 11, 10, 7, 22))
    git(h.world.inbox, "update-ref", "refs/heads/claude/probe-early", sha)
    git(h.world.inbox, "update-ref", "refs/heads/claude/analyst-copy", sha)
    t = h.at(2026, 11, 10, 7, 21)
    h.api.add_push("refs/heads/claude/probe-early", "0" * 40, sha, h.at(2026, 11, 10, 7, 14))   # a probe ref: ignored
    h.api.add_push("refs/heads/claude/analyst-copy", "0" * 40, sha, t, act_id=5)               # earlier, analyst
    h.api.add_push("refs/heads/main", "0" * 40, sha, h.at(2026, 11, 10, 7, 10))                # main: ignored
    h.at(2026, 11, 10, 7, 30)
    h.merge()
    ws = h.witnesses()
    assert len(ws) == 1
    assert (ws[0]["push"]["ref"], ws[0]["window"]["time_utc"], ws[0]["push"]["activity_id"]) == \
        ("refs/heads/claude/analyst-copy", "2026-11-10T07:21:00Z", 5)
    assert ws[0]["window"]["void"] is False                    # 07:14 would have been void (before entry − 45)


def test_a_timestamp_tie_goes_to_the_smaller_activity_id(tmp_path):
    h = Harness(tmp_path)
    assert h.run("start") == 0
    [(ref, sha)] = h.register_pushes()
    git(h.world.inbox, "update-ref", "refs/heads/claude/analyst-tie", sha)
    h.api.add_push("refs/heads/claude/analyst-tie", "0" * 40, sha, h.now(), act_id=3)
    h.merge()
    assert h.last_witness()["push"]["activity_id"] == 3


@pytest.mark.parametrize("inbox_from", [None, "2026-11-10T07:20:01Z"])
def test_entries_before_inbox_from_utc_are_never_processed(tmp_path, inbox_from):
    h = Harness(tmp_path, inbox_from_utc=inbox_from)
    assert h.run("start") == 0
    h.register_pushes()                                        # 07:20:00
    h.at(2026, 11, 10, 7, 30)
    h.merge()
    assert h.api.issue_comments == [] and h.world.main_file(CANARY_ATT) is None
    assert ("inbox_from_utc=unset" in h.merge_log) == (inbox_from is None)


def test_entries_older_than_14_days_are_not_processed(tmp_path):
    h = Harness(tmp_path, canary_from="2026-11-25", slots_until=dt_(2026, 12, 4))
    sha = h.agent_branch("claude/analyst-old", {CANARY_ATT: attempt_bytes(h)})
    h.api.add_push("refs/heads/claude/analyst-old", "0" * 40, sha, h.at(2026, 11, 10, 7, 20))
    h.at(2026, 11, 24, 7, 20, 1)
    h.merge()
    assert h.api.issue_comments == []


def dt_(*a):
    import datetime as dt
    return dt.date(*a)


# ---------------------------------------------------------------- fetch as data, and its bounds (R-AN-32)
class Recorder:
    """Wraps subprocess.run for the merge workspace: records git argv; `fetch` answers can be replaced."""

    def __init__(self, fetch=None):
        self.cmds = []
        self.fetch = fetch

    def __call__(self, argv, **kw):
        self.cmds.append(list(argv))
        if "fetch" in argv and self.fetch is not None:
            return self.fetch(argv, kw)
        return subprocess.run(argv, **kw)

    def fetches(self):
        return [c for c in self.cmds if "fetch" in c]


def test_the_fetch_is_from_the_inbox_without_credentials(tmp_path):
    h = Harness(tmp_path)
    h.runner = Recorder()
    assert h.run("start") == 0
    h.push_and_merge()
    [f] = h.runner.fetches()
    i = f.index("fetch")
    assert f[i - 2:i] == ["-c", "http.https://github.com/.extraheader="]
    assert f[i + 1:] == ["--quiet", "--no-tags", str(h.world.inbox), h.last_witness()["push"]["after"]]


def test_a_fetch_timeout_is_agent_scope_without_more_tries(tmp_path):
    def timeout(argv, kw):
        raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
    h = Harness(tmp_path)
    h.runner = Recorder(fetch=timeout)
    assert h.run("start") == 0
    h.push_and_merge()                                          # no exception
    w = h.last_witness()
    assert (w["kind"], w["id"], w["status"], w["reason"], w["path"]) == ("push", None, "rejected", "agent_scope",
                                                                        None)
    assert len(h.runner.fetches()) == 1 and h.runner.fetches()[0] and h.world.main_file(CANARY_ATT) is None


def test_a_failing_fetch_is_tried_3_times_20_s_apart(tmp_path):
    def fail(argv, kw):
        return subprocess.CompletedProcess(argv, 1, b"", b"fatal: x")
    h = Harness(tmp_path)
    rec_ = Recorder(fetch=fail)
    sleeps = []
    assert h.run("start") == 0
    h.register_pushes()
    ws = merge.Workspace(h.world.clone("m"), runner=rec_, inbox_url=str(h.world.inbox), sleep=sleeps.append)
    merge.run_merge(ws, h.api, h.now, {"workflow": "analyst-merge", "id": 1, "attempt": 1, "event": "schedule"})
    assert sleeps == [20, 20]
    assert len(rec_.fetches()) == 6                              # 3 tries of (the sha, then the push's ref)
    assert all(c[c.index("fetch") + 4] in (h.api.acts[0]["after"], f"+{h.api.acts[0]['ref']}:refs/analyst-fetch/"
                                                                   f"{h.api.acts[0]['after']}")
               for c in rec_.fetches())
    assert h.last_witness()["reason"] == "agent_scope"


def test_every_fetch_command_has_the_180_s_timeout(tmp_path):
    seen = []

    def spy(argv, kw):
        seen.append(kw.get("timeout"))
        return subprocess.run(argv, **kw)
    h = Harness(tmp_path)
    h.runner = Recorder(fetch=spy)
    assert h.run("start") == 0
    h.push_and_merge()
    assert seen == [180]


@pytest.mark.parametrize("size,reason", [(4 * 1024 * 1024 + 1, "agent_scope"), (4 * 1024 * 1024, "invalid_content")])
def test_a_blob_over_4_mib_is_agent_scope_and_never_read(tmp_path, size, reason):
    h = Harness(tmp_path)
    h.runner = Recorder()
    h.agent_branch("claude/analyst-big", {CANARY_ATT: b"{" + b" " * (size - 2) + b"}"})
    h.push_and_merge()
    w = h.last_witness()
    assert (w["status"], w["reason"], w["path"]) == ("rejected", reason, CANARY_ATT)
    shows = [c for c in h.runner.cmds if "show" in c]
    assert (shows == []) == (reason == "agent_scope")


# ---------------------------------------------------------------- edited witness comments (R-AN-35)
def test_an_edited_comment_counts_only_for_seq(tmp_path):
    h = Harness(tmp_path)
    assert h.run("start") == 0
    h.register_pushes()
    h.merge_fail = True
    with pytest.raises(RuntimeError):
        h.merge()                                              # the comment (seq 1) is out, its file is not
    h.merge_fail = False
    h.api.issue_comments[0]["updated_at"] = "2026-11-10T07:21:00Z"   # edited after it was posted
    h.at(2026, 11, 10, 7, 22)
    h.merge()
    ws = h.witnesses()
    assert [w["seq"] for w in ws] == [2]                          # not completed from the edited comment; seq 1 unused
    assert ws[0]["status"] == "accepted" and h.main_json(CANARY_ATT) is not None
    assert len(h.api.issue_comments) == 2


def test_seq_counts_every_bot_header_even_a_malformed_comment(tmp_path):
    h = Harness(tmp_path)
    h.api.issue_comments.append({"id": 1, "created_at": "2026-11-10T07:00:00Z", "updated_at": "2026-11-10T07:00:00Z",
                                 "user": {"login": "github-actions[bot]"},
                                 "body": "analyst-witness/1 attempt 2026-11-10_W1 seq 7\n{}\nextra"})
    assert h.run("start") == 0
    h.push_and_merge()
    assert h.last_witness()["seq"] == 8


def test_an_edited_comment_does_not_count_as_a_witness_of_its_push(tmp_path):
    """The push named by an edited comment (whose file is missing) is processed again, from its activity entry."""
    h = Harness(tmp_path)
    assert h.run("start") == 0
    [(_ref, sha)] = h.register_pushes()
    body = {"push": {"after": sha}, "kind": "attempt", "seq": 3}
    h.api.issue_comments.append({"id": 9, "created_at": "2026-11-10T07:20:30Z", "updated_at": "2026-11-10T07:25:00Z",
                                 "user": {"login": "github-actions[bot]"},
                                 "body": f"analyst-witness/1 attempt {W1} seq 3\n{rec.compact(body)}"})
    h.merge()
    assert [w["seq"] for w in h.witnesses()] == [4] and h.main_json(CANARY_ATT) is not None


# ---------------------------------------------------------------- the closer processes every push first (R-AN-39)
def test_a_timely_lock_listed_before_the_closer_is_never_closed_over(tmp_path):
    h = Harness(tmp_path)
    assert h.run("start") == 0
    h.register_pushes()                                        # 07:20, no merge run yet
    h.at(2026, 11, 10, 7, 21)
    assert h.run("context") == 0
    h.at(2026, 11, 10, 7, 44, 30)
    assert h.run("lock", str(h.write_plan(PLAN))) == 0
    h.register_pushes()                                        # 07:44:30, before the deadline
    h.at(2026, 11, 10, 8, 30)                                  # the first merge run is late: after deadline + 20
    h.merge()
    t = h.main_json(f"analyst/canary/terminal/{W1}.json")
    assert (t["status"], t["writer"], t["call"]["lock_utc"]) == ("called", "merge", "2026-11-10T07:44:30Z")
    assert [w["kind"] for w in h.witnesses()] == ["attempt", "call", "terminal"]


def test_a_failure_while_witnessing_a_push_closes_no_window(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    assert h.run("start") == 0
    h.register_pushes()
    h.at(2026, 11, 10, 8, 30)

    def down(issue, body):
        raise ghapi.GitHubError("POST comments -> 502")
    monkeypatch.setattr(h.api, "post_comment", down)
    with pytest.raises(ghapi.GitHubError):
        h.merge()
    assert h.world.main_list("analyst/canary/terminal/") == []


# ---------------------------------------------------------------- probe pushes and floods
def test_a_flood_of_probe_branches_is_read_but_never_processed(tmp_path):
    h = Harness(tmp_path)
    for i in range(2500):
        h.api.add_push(f"refs/heads/claude/probe-flood-{i}", "0" * 40, f"{i:040x}", h.now())
    h.api.add_push("refs/heads/claude/other-x", "0" * 40, "f" * 40, h.now())
    assert h.run("start") == 0
    h.register_pushes()
    acts = list(h.api.acts)

    def script(url, tok, n):                                    # the real reader, 100 entries a page
        k = int(url.rsplit("after=", 1)[1]) if "after=" in url else 0
        nxt = f"{API}/repositories/1/activity?per_page=100&after={k + 100}" if k + 100 < len(acts) else None
        return page(acts[k:k + 100], nxt)
    inbox = ghapi.InboxActivity(INBOX_REPO, token="t", http=FakeHTTP(script), sleep=lambda s: None)
    log = []
    merge.run_merge(h.workspace(), h.api, h.now, {"workflow": "analyst-merge", "id": 1, "attempt": 1,
                                                   "event": "schedule"}, inbox=inbox, log=log.append)
    assert "activity_read=token" in log
    assert [(w["kind"], w["status"]) for w in h.witnesses()] == [("attempt", "accepted")]
    assert len(h.api.issue_comments) == 1


# ---------------------------------------------------------------- commit subject and dispatches (R-AN-32, R-AN-40)
def subject(h):
    return git(h.world.origin, "log", "-1", "--format=%s", "main")


def test_commit_subject_and_deploy_dispatch_after_a_commit_only(tmp_path):
    h = Harness(tmp_path)
    assert h.run("start") == 0
    h.push_and_merge()
    assert subject(h) == "analyst-merge seq 1-1"
    assert h.api.dispatched == [("analyst-deploy.yml", "main", {})]       # canary: no tracker, but the page deploy
    h.at(2026, 11, 10, 7, 30)
    h.merge()                                                  # nothing new: no commit, no dispatch
    assert h.api.dispatched == [("analyst-deploy.yml", "main", {})]
    h.at(2026, 11, 10, 8, 10)
    h.merge()                                                  # the closer's terminal
    assert subject(h) == "analyst-merge seq 2-2" and len(h.api.dispatched) == 2


def test_a_closer_run_without_a_witness_has_the_closer_subject(tmp_path):
    h = Harness(tmp_path, canary_from="2026-11-09")
    h.world.owner_commit({"analyst/MODE.json": json.dumps({"mode": "canary", "canary_from": "2026-11-09",
                                                           "canary_to": "2026-11-13"})})
    h.at(2026, 11, 14, 12, 0)
    h.merge()                                                  # closes the week's ten windows
    assert subject(h) == "analyst-merge seq 1-10"
    h.at(2026, 11, 15, 0, 1)
    h.merge()                                                  # only the W46 revision skip: no witness
    assert subject(h) == "analyst-merge closer"
    assert h.main_json("analyst/canary/revisions/2026-W46.json")["status"] == "skipped"


def test_live_lock_dispatches_track_then_deploy(tmp_path):
    h = Harness(tmp_path, mode="live", canary_from=None, live_from="2026-11-10")
    assert h.run("start") == 0
    h.push_and_merge()
    h.at(2026, 11, 10, 7, 21)
    assert h.run("context") == 0
    h.at(2026, 11, 10, 7, 25)
    assert h.run("lock", str(h.write_plan(PLAN))) == 0
    h.api.dispatched.clear()
    h.push_and_merge()
    assert h.api.dispatched == [("analyst-track.yml", "main", {"call_id": W1}), ("analyst-deploy.yml", "main", {})]


def test_settle_dispatches_track_for_a_lock_its_closer_witnessed_then_deploy(tmp_path):
    h = Harness(tmp_path, mode="live", canary_from=None, live_from="2026-11-10")
    assert h.run("start") == 0
    h.push_and_merge()
    h.at(2026, 11, 10, 7, 21)
    assert h.run("context") == 0
    h.at(2026, 11, 10, 7, 25)
    assert h.run("lock", str(h.write_plan(PLAN))) == 0
    h.register_pushes()                                        # no merge run: settle's closer witnesses it
    h.api.dispatched.clear()
    settle_run.run_settle(h.workspace("settle"), h.api, h.now, {"workflow": "analyst-settle", "id": 3,
                                                               "attempt": 1, "event": "push"})
    assert h.main_json(f"analyst/terminal/{W1}.json")["status"] == "called"
    assert h.api.dispatched == [("analyst-track.yml", "main", {"call_id": W1}), ("analyst-deploy.yml", "main", {})]
