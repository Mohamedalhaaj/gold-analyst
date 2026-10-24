"""The routine session against the agent inbox (register §16.18 A6; R-AN-24 attempts in flight, R-AN-84 probes and
`run_window.py probe`, R-AN-71 canary probes).

The merge now polls, and inbox main trails gold-analyst main by one mirror run, so a session never waits for a
witness: an unwitnessed attempt branch in the inbox is a draw in progress (`concurrent`), unless this session pushed
it itself. The probes record only integers, enums and booleans, and give `pass`, `fail` or `incomplete`.
"""
import json
import os
import time
from pathlib import Path

import pytest

from ga import probes, rec, records
from harness import PLAN, Harness
from synth import INBOX_REPO, git

W1 = "2026-11-10_W1"
DUKAS = probes.DUKASCOPY_URL


def heads(h, prefix="refs/"):
    out = git(h.world.inbox, "for-each-ref", "--format=%(refname)", prefix)
    return out.splitlines() if out else []


# ---------------------------------------------------------------- attempts in flight (R-AN-24, C15)
def test_an_unwitnessed_attempt_branch_refuses_concurrent_until_its_witness_reaches_the_inbox(tmp_path):
    h = Harness(tmp_path)
    assert h.run("start") == 0 and h.log[-1] == f"attempt_pushed {W1} n=1 mode=canary"
    h.at(2026, 11, 10, 7, 22)
    b = h.new_session_dir("b")
    assert h.run("start", sess_dir=b) == 0 and h.log[-1] == "refused concurrent"        # in flight
    h.register_pushes(t=h.at(2026, 11, 10, 7, 20))
    h.auto_mirror = False
    h.at(2026, 11, 10, 7, 25)
    h.merge()                                                  # witnessed on gold-analyst main only
    assert h.world.inbox_main_sha() != h.world.main_sha()
    b2 = h.new_session_dir("b2")
    assert h.run("start", sess_dir=b2) == 0 and h.log[-1] == "refused concurrent"       # still in flight
    h.world.mirror()
    assert h.world.inbox_main_sha() == h.world.main_sha()
    b3 = h.new_session_dir("b3")
    assert h.run("start", sess_dir=b3) == 0 and h.log[-1] == "refused concurrent"       # by the attempt's time
    h.at(2026, 11, 10, 7, 41)
    b4 = h.new_session_dir("b4")
    assert h.run("start", sess_dir=b4) == 0 and h.log[-1] == f"attempt_pushed {W1} n=2 mode=canary"


def test_a_rejected_attempt_branch_is_not_in_flight(tmp_path):
    from ga import records
    from ga.repo import DirView
    h = Harness(tmp_path)
    doc = records.attempt("call", "canary", W1, 2, W1, "x" * 40, records.frozen_block(DirView(h.world.seed)),
                          {"note": "x"})
    h.agent_branch(f"claude/analyst-canary-attempt-{W1}-2",
                   {"analyst/canary/attempts/2026-11-10_W1_2.json": rec.record_bytes(doc)})
    h.push_and_merge()
    assert h.last_witness()["reason"] == "attempt_n"
    assert h.run("start") == 0 and h.log[-1] == f"attempt_pushed {W1} n=1 mode=canary"


def test_another_window_or_kind_is_not_in_flight(tmp_path):
    h = Harness(tmp_path, canary_from="2026-11-09")
    h.agent_branch("claude/analyst-canary-attempt-2026-11-09_W1-1", {"x.txt": b"x"})
    h.agent_branch(f"claude/analyst-canary-attempt-lesson-{W1}-1", {"y.txt": b"y"})
    h.agent_branch(f"claude/analyst-live-attempt-{W1}-1", {"z.txt": b"z"})
    assert h.run("start") == 0 and h.log[-1] == f"attempt_pushed {W1} n=1 mode=canary"


def test_lesson_start_skips_a_draw_in_flight_but_not_its_own(tmp_path):
    from ga import paths as P
    fx = (__import__("pathlib").Path(__file__).resolve().parents[1] / P.CANARY_SETTLED_FIXTURE).read_bytes()
    h = Harness(tmp_path, canary_from="2026-11-09", extra={P.CANARY_SETTLED_FIXTURE: fx})
    h.at(2026, 11, 10, 16, 40)
    assert h.run("lesson_start") == 0 and h.log[-1].startswith("lesson_attempt_pushed 2026-10-16_W1 n=1")
    b = h.new_session_dir("b")
    assert h.run("lesson_start", sess_dir=b) == 0 and h.log[-1] == "none"              # in flight elsewhere
    lp = h.sess_dir / ".analyst_run" / "lesson.json"
    lp.write_text("{}")
    for _ in range(3):
        h.run("lesson_lock", str(lp))
    assert h.log[-1] == "attempt_ended"
    assert h.run("lesson_start") == 0 and h.log[-1].startswith("lesson_attempt_pushed 2026-10-16_W1 n=2")


def test_revision_start_refuses_a_revision_attempt_in_flight(tmp_path):
    h = Harness(tmp_path, canary_from="2026-11-09")
    h.at(2026, 11, 14, 18, 0)
    h.merge()                                                  # the closer closes the week
    assert h.run("revision_start") == 0 and h.log[-1].startswith("revision_attempt_pushed v1 n=1")
    b = h.new_session_dir("b")
    assert h.run("revision_start", sess_dir=b) == 0 and h.log[-1] == "refused concurrent"


# ---------------------------------------------------------------- the probes (R-AN-84)
def probe_world(tmp_path, **kw):
    h = Harness(tmp_path, **kw)
    h.world.owner_commit({"analyst/ops_changes.csv": "utc,file,sha256,head_sha,note\n"})   # main has a parent
    h.probe_get = lambda u, t: (200, b"x") if u == DUKAS else (0, None)
    h.at(2026, 10, 29, 10, 15)
    return h


def run_probe(h):
    rc = h.run("probe")
    line = h.log[-1] if h.log[-1].startswith("probe_") else h.log[-2]
    verdict, payload = line.split(" ", 1)
    return rc, verdict, json.loads(payload)


EXPECTED = {
    "net.dukascopy": 200, "net.google": 0, "net.raw_public": 0, "net.api_root": 0,
    "rest.inbox": "ok", "rest.ga": "scope", "rest.gfw": "scope", "rest.hooks": 0,
    "git.ls_remote_inbox": "accepted", "git.ls_remote_gfw": "refused", "git.ls_remote_gfw_proxy": "refused",
    "git.ls_remote_public": "refused", "git.ls_remote_public_proxy": "refused",
    "push_main": "refused_rule", "force_main": "refused_rule", "rest.force_main": "denied",
    "tag_push": "refused_rule", "rest.tag_create": "denied", "other_branch_push": "refused_rule",
    "branch_delete": "refused_rule", "activity_seen_s": 0, "rest.delete_branch": "denied",
    "ga_push": "refused", "ga_push_proxy": "refused", "workflow_push": "accepted", "admin_probe": "denied",
}


def test_probe_subcommand_in_a_conforming_inbox(tmp_path):
    h = probe_world(tmp_path)
    main_before = h.world.inbox_main_sha()
    rc, verdict, res = run_probe(h)
    tag = "p20261029T101500Z"
    assert (rc, verdict) == (0, "probe_pass")
    assert set(res) == {"schema", "tag", "kind", "repo_head_sha", "probe_utc", "outcomes", "pass", "verdict",
                        "cc_version"}
    assert (res["schema"], res["tag"], res["kind"], res["verdict"]) == ("analyst-probe/1", tag, "probe", "pass")
    assert res["cc_version"] == "test-cc"                  # the session's version (third review of A7, decision 22)
    assert res["repo_head_sha"] == main_before and res["probe_utc"] == "2026-10-29T10:15:00Z"
    assert res["outcomes"] == EXPECTED
    assert set(res["outcomes"]) == set(res["pass"]) == set(probes.ORDER)
    assert res["pass"]["net.api_root"] is None and res["pass"]["activity_seen_s"] is None
    assert res["pass"]["workflow_push"] is None
    assert all(v is True for k, v in res["pass"].items() if k not in ("net.api_root", "activity_seen_s",
                                                                      "workflow_push"))
    # the record, as written and as pushed (one new commit on inbox main, on claude/probe-result-<tag>)
    local = (h.sess_dir / ".analyst_run" / "probe.json").read_bytes()
    assert local == rec.record_bytes(res)
    br = f"refs/heads/claude/probe-result-{tag}"
    assert git(h.world.inbox, "show", f"{br}:probes/{tag}.json").encode() + b"\n" == local
    assert git(h.world.inbox, "rev-parse", f"{br}^") == main_before
    assert git(h.world.inbox, "diff", "--name-only", main_before, br) == f"probes/{tag}.json"
    # nothing landed outside claude/probe-*, and inbox main did not move
    assert h.world.inbox_main_sha() == main_before and heads(h, "refs/tags/") == []
    names = sorted(r for r in heads(h, "refs/heads/") if r != "refs/heads/main")
    assert names == sorted(f"refs/heads/claude/probe-{p}-{tag}" for p in ("c3", "del", "result", "wf"))
    # the c3 commit is head's tree on head's first parent; the workflow commit adds exactly the probe workflow
    c3 = git(h.world.inbox, "rev-parse", f"refs/heads/claude/probe-c3-{tag}")
    tree = git(h.world.inbox, "rev-parse", f"{main_before}^{{tree}}")
    assert git(h.world.inbox, "rev-parse", f"{c3}^{{tree}}") == tree
    assert git(h.world.inbox, "rev-parse", f"{c3}^") == git(h.world.inbox, "rev-parse", f"{main_before}^")
    wf = f"refs/heads/claude/probe-wf-{tag}"
    assert git(h.world.inbox, "diff", "--name-only", main_before, wf) == ".github/workflows/probe.yml"
    assert git(h.world.inbox, "show", f"{wf}:.github/workflows/probe.yml").encode() + b"\n" == probes.WORKFLOW_YML
    # the REST probes, as R-AN-84 writes them
    calls = h.gh.calls
    assert ["-X", "PATCH", f"repos/{INBOX_REPO}/git/refs/heads/main", "-f", f"sha={c3}", "-F", "force=true"] in calls
    assert ["-X", "PUT", f"repos/{INBOX_REPO}/actions/permissions", "-F", "enabled=false"] == calls[-1]
    assert ["-X", "POST", f"repos/{INBOX_REPO}/git/refs", "-f", f"ref=refs/tags/probe-r-{tag}", "-f",
            f"sha={main_before}"] in calls
    assert ["-X", "DELETE", f"repos/{INBOX_REPO}/git/refs/heads/claude/probe-del-{tag}"] in calls
    assert [f"repos/{INBOX_REPO}/activity?ref=refs/heads/claude/probe-del-{tag}&per_page=10"] in calls


def test_probe_record_holds_no_text(tmp_path):
    h = probe_world(tmp_path)
    _rc, _v, res = run_probe(h)
    for k, v in res["outcomes"].items():
        assert v is None or isinstance(v, int) or v in ("ok", "scope", "denied", "none", "accepted", "refused",
                                                        "refused_rule", "error"), k


@pytest.mark.parametrize("case,probe", [
    ("admin", "admin_probe"), ("hooks", "rest.hooks"), ("rest_ga", "rest.ga"), ("ga_proxy_writable", "ga_push_proxy"),
    ("ga_proxy_rule", "ga_push_proxy"), ("raw_public", "net.raw_public"), ("rest_force", "rest.force_main"),
    ("google", "net.google"), ("no_rules", "push_main"),
])
def test_a_failed_probe_fails_the_verdict(tmp_path, case, probe):
    h = probe_world(tmp_path)
    if case == "admin":
        h.gh.routes[("PUT", f"repos/{INBOX_REPO}/actions/permissions")] = (204, None)
    elif case == "hooks":
        h.gh.routes[("GET", f"repos/{INBOX_REPO}/hooks")] = (200, [{"id": 1}])
    elif case == "rest_ga":
        h.gh.routes[("GET", "repos/Mohamedalhaaj/gold-analyst")] = (200, {"id": 2})
    elif case.startswith("ga_proxy"):
        ga = tmp_path / "Mohamedalhaaj" / "gold-analyst.git"     # P(gold-analyst): reachable through the proxy form
        git(tmp_path, "init", "-q", "--bare", str(ga))
        if case == "ga_proxy_rule":
            (ga / "hooks" / "pre-receive").write_text("#!/bin/sh\necho 'error: GH013: rule' >&2\nexit 1\n")
            (ga / "hooks" / "pre-receive").chmod(0o755)
    elif case == "raw_public":
        h.probe_get = lambda u, t: (200, b"x") if u in (DUKAS, probes.RAW_PUBLIC_URL) else (0, None)
    elif case == "rest_force":
        h.gh.routes[("PATCH", f"repos/{INBOX_REPO}/git/refs/heads/main")] = (200, {})
    elif case == "google":
        h.probe_get = lambda u, t: (200, b"x") if u in (DUKAS, probes.GOOGLE_URL) else (0, None)
    elif case == "no_rules":
        (h.world.inbox / "hooks" / "pre-receive").unlink()
    rc, verdict, res = run_probe(h)
    assert (rc, verdict, res["verdict"]) == (4, "probe_fail", "fail")
    assert res["pass"][probe] is False


def test_gh_absent_makes_the_rest_probes_null_and_the_verdict_incomplete(tmp_path):
    h = probe_world(tmp_path)
    h.gh.absent = True
    rc, verdict, res = run_probe(h)
    assert (rc, verdict) == (5, "probe_incomplete")
    for p in probes.REST:
        assert res["outcomes"][p] == "none" and res["pass"][p] is None, p
    assert res["outcomes"]["activity_seen_s"] == -1


def test_the_rest_positive_control_nulls_every_rest_probe(tmp_path):
    h = probe_world(tmp_path)
    h.gh.routes[("GET", f"repos/{INBOX_REPO}")] = (404, {"message": "Not Found"})
    h.gh.routes[("PUT", f"repos/{INBOX_REPO}/actions/permissions")] = (204, None)     # would fail if it counted
    rc, verdict, res = run_probe(h)
    assert (rc, res["verdict"]) == (5, "incomplete")
    assert all(res["pass"][p] is None for p in probes.REST)
    assert res["outcomes"]["admin_probe"] == "ok"


def test_activity_seen_s_measures_the_listing_delay(tmp_path):
    h = probe_world(tmp_path)
    h.gh.seen_after_polls = 3
    _rc, _v, res = run_probe(h)
    assert res["outcomes"]["activity_seen_s"] == 15
    h2 = probe_world(tmp_path / "b")
    h2.gh.seen_after_polls = 10 ** 9
    _rc, verdict, res2 = run_probe(h2)
    assert res2["outcomes"]["activity_seen_s"] == -1 and verdict == "probe_pass"         # measured only
    assert h2.gh.polls == 121                                   # every 5 s up to the 600 s bound


@pytest.mark.parametrize("outcomes,probe,want", [
    ({"ga_push": "refused_rule"}, "ga_push", False),
    ({"ga_push": "accepted"}, "ga_push", False),
    ({"push_main": "refused"}, "push_main", True),
    ({"rest.hooks": "denied"}, "rest.hooks", None),
    ({"rest.hooks": 2}, "rest.hooks", False),
    ({"net.google": 302}, "net.google", False),
    ({"net.google": 403}, "net.google", True),
    ({"rest.force_main": "scope"}, "rest.force_main", True),         # refused either way (fourth review)
    ({"rest.tag_create": "scope"}, "rest.tag_create", True),
    ({"rest.delete_branch": "scope"}, "rest.delete_branch", True),
    ({"rest.force_main": "ok"}, "rest.force_main", False),
    ({"rest.delete_branch": "ok"}, "rest.delete_branch", False),
    ({"admin_probe": "scope"}, "admin_probe", True),
    ({"admin_probe": "none"}, "admin_probe", None),
    ({"branch_delete": "error"}, "branch_delete", None),
    ({"git.ls_remote_gfw": "refused_rule"}, "git.ls_remote_gfw", False),
    ({"workflow_push": "refused"}, "workflow_push", None),
])
def test_pass_rules(outcomes, probe, want):
    base = dict(EXPECTED)
    base.update(outcomes)
    assert probes.passes(base, "probe")[probe] is want


@pytest.mark.parametrize("kind,code,want", [
    ("canary", 200, True), ("canary", 404, True), ("canary", 0, None),     # null when every fetch gave 0 (R-AN-84,
    ("probe", 200, True), ("probe", 404, False), ("probe", 0, False),      # fourth review); `probe` keeps 200
])
def test_net_dukascopy_pass_rule(kind, code, want):
    p = probes.passes(dict(EXPECTED, **{"net.dukascopy": code}), kind)
    assert p["net.dukascopy"] is want
    assert probes.verdict(p) == {True: "pass", False: "fail", None: "incomplete"}[want]


def test_the_git_positive_control_nulls_every_git_probe():
    base = dict(EXPECTED, **{"git.ls_remote_inbox": "refused", "ga_push": "refused_rule"})
    p = probes.passes(base, "probe")
    assert all(p[k] is None for k in probes.GIT)
    assert probes.verdict(p) == "incomplete"


def test_verdict_rules():
    p = probes.passes(EXPECTED, "probe")
    assert probes.verdict(p) == "pass"
    p2 = dict(p, **{"net.api_root": None, "activity_seen_s": None, "workflow_push": None})
    assert probes.verdict(p2) == "pass"
    assert probes.verdict(dict(p, push_main=None)) == "incomplete"
    assert probes.verdict(dict(p, push_main=None, ga_push=False)) == "fail"


def test_proxy_url_form():
    o = "https://proxy.example/git/Mohamedalhaaj/gold-analyst-inbox"
    assert probes.proxy_url(o, INBOX_REPO, "octocat/Hello-World") == "https://proxy.example/git/octocat/Hello-World"
    assert probes.proxy_url(o + ".git", INBOX_REPO, "Mohamedalhaaj/gold-analyst") == \
        "https://proxy.example/git/Mohamedalhaaj/gold-analyst.git"
    assert probes.proxy_url("https://x/other/repo", INBOX_REPO, "a/b") is None


def test_unknown_origin_url_form_makes_the_proxy_probes_error(tmp_path):
    h = probe_world(tmp_path)
    other = tmp_path / "elsewhere.git"
    other.symlink_to(h.world.inbox)                             # the same inbox under a URL of another form
    git(h.sess_dir, "remote", "set-url", "origin", str(other))
    _rc, _v, res = run_probe(h)
    for p in ("git.ls_remote_gfw_proxy", "git.ls_remote_public_proxy", "ga_push_proxy"):
        assert res["outcomes"][p] == "error" and res["pass"][p] is None, p


# ---------------------------------------------------------------- the session's Claude Code version (decision 22)
# Third review of §16.18 A7 (R-AN-84, R-AN-85 "Claude Code version"): both kinds of the probe record hold
# `cc_version`, read by records.cc_version, the function of the attempt and call records' `self_reported.cc_version`:
# the first line of `claude --version`, at most 100 characters, null when `claude` is absent, fails or takes more than
# 10 s. It is not a probe: it is in neither `outcomes` nor `pass`, and the verdict and the exit code do not depend on
# it. Here the session reads it itself (Harness.cc_version None) from a stub `claude` on PATH.
CC = "2.1.309 (Claude Code)"
LONG = "2.1.309 " + "x" * 150
STUBS = {
    "version": ("echo '2.1.309 (Claude Code)'\n", CC),
    "long_first_line": (f"echo '{LONG}'\necho second\n", LONG[:100]),
    "none": (None, None),
    "exits_1": ("echo '2.1.309 (Claude Code)'\nexit 1\n", None),
    "hangs": ("exec sleep 30\n", None),
}


def path_with(tmp_path, stub):
    """PATH without this machine's `claude`, and with a stub `claude` first when `stub` (a shell body) is given."""
    dirs = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d and not (Path(d) / "claude").exists()]
    if stub is not None:
        b = tmp_path / "stub_bin"
        b.mkdir()
        (b / "claude").write_text("#!/bin/sh\n" + stub)
        (b / "claude").chmod(0o755)
        dirs.insert(0, str(b))
    return os.pathsep.join(dirs)


def timed_cc_version(monkeypatch):
    """records.cc_version, timed at each call: the session's own function, run for real."""
    real, spent = records.cc_version, []

    def timed(*a, **k):
        t0 = time.monotonic()
        try:
            return real(*a, **k)
        finally:
            spent.append(time.monotonic() - t0)
    monkeypatch.setattr(records, "cc_version", timed)
    return spent


@pytest.mark.parametrize("case", list(STUBS))
def test_the_probe_record_holds_the_sessions_claude_code_version(tmp_path, monkeypatch, case):
    stub, want = STUBS[case]
    monkeypatch.setenv("PATH", path_with(tmp_path, stub))
    spent = timed_cc_version(monkeypatch)
    h = probe_world(tmp_path)
    h.cc_version = None
    rc, verdict, res = run_probe(h)
    assert res["cc_version"] == want and len(spent) == 1               # read once, by the records' function
    assert json.loads((h.sess_dir / ".analyst_run" / "probe.json").read_text("utf-8"))["cc_version"] == want
    tag = "p20261029T101500Z"
    pushed = git(h.world.inbox, "show", f"refs/heads/claude/probe-result-{tag}:probes/{tag}.json")
    assert json.loads(pushed)["cc_version"] == want
    # not a probe: the outcomes, the passes, the verdict and the exit code are those of any other version
    assert "cc_version" not in res["outcomes"] and "cc_version" not in res["pass"]
    assert (rc, verdict, res["verdict"], res["outcomes"]) == (0, "probe_pass", "pass", EXPECTED)
    if case == "hangs":
        assert 10 <= spent[0] < 20                                     # given up after 10 s
    else:
        assert spent[0] < 10


@pytest.mark.parametrize("stub", ["echo '2.1.309 (Claude Code)'\n", "echo '2.0.1 (Claude Code)'\n", None])
def test_the_version_changes_no_verdict_and_no_exit_code(tmp_path, monkeypatch, stub):
    """A failing probe set fails, and gives exit 4, with any version or none."""
    monkeypatch.setenv("PATH", path_with(tmp_path, stub))
    h = probe_world(tmp_path)
    h.cc_version = None
    h.gh.routes[("PUT", f"repos/{INBOX_REPO}/actions/permissions")] = (204, None)
    rc, verdict, res = run_probe(h)
    assert (rc, verdict, res["verdict"], res["pass"]["admin_probe"]) == (4, "probe_fail", "fail", False)
    assert res["cc_version"] == (stub.split("'")[1] if stub else None)


def test_a_canary_call_holds_the_version_in_its_probe_record_and_its_self_reports(tmp_path, monkeypatch):
    """The canary probe record (in the call file's `canary_probe`) and the attempt and call records' self-reports
    take the version from the same function."""
    monkeypatch.setenv("PATH", path_with(tmp_path, "echo '2.1.309 (Claude Code)'\n"))
    h = Harness(tmp_path)
    h.cc_version = None
    assert h.run("start") == 0
    h.push_and_merge()
    h.at(2026, 11, 10, 7, 21)
    assert h.run("context") == 0
    h.at(2026, 11, 10, 7, 25)
    assert h.run("lock", str(h.write_plan(PLAN))) == 0
    h.push_and_merge()
    att = h.main_json(f"analyst/canary/attempts/{W1}_1.json")
    call = h.main_json(f"analyst/canary/calls/{W1}.json")
    cp = call["canary_probe"]
    assert cp["cc_version"] == call["self_reported"]["cc_version"] == att["self_reported"]["cc_version"] == CC
    assert "cc_version" not in cp["outcomes"] and "cc_version" not in cp["pass"] and cp["verdict"] == "pass"


# ---------------------------------------------------------------- canary calls carry the probes (R-AN-71, R-AN-84)
def test_canary_context_runs_the_probes_after_writing_the_context(tmp_path):
    h = Harness(tmp_path)
    seen = {}

    def first_call(args):
        seen.setdefault("context_written", (h.sess_dir / ".analyst_run" / "context.json").exists())
    h.gh.on_call = first_call
    assert h.run("start") == 0
    h.push_and_merge()
    h.at(2026, 11, 10, 7, 21)
    assert h.run("context") == 0 and h.log[-1].startswith(f"context_ok {W1}")
    assert seen == {"context_written": True}
    h.at(2026, 11, 10, 7, 25)
    assert h.run("lock", str(h.write_plan(PLAN))) == 0
    h.push_and_merge()
    cp = h.main_json(f"analyst/canary/calls/{W1}.json")["canary_probe"]
    assert (cp["schema"], cp["kind"], cp["tag"], cp["verdict"]) == ("analyst-probe/1", "canary", W1, "pass")
    assert cp["outcomes"]["net.dukascopy"] == 200                 # the context's own fetches
    assert cp["probe_utc"] == "2026-11-10T07:21:00Z"
    assert cp["cc_version"] == "test-cc"                  # both kinds of the record (third review of A7)
    assert sorted(r for r in heads(h, "refs/heads/claude/probe-*")) == sorted(
        f"refs/heads/claude/probe-{p}-{W1}" for p in ("c3", "del", "wf"))


def test_canary_probes_are_bounded_at_120_s(tmp_path):
    h = Harness(tmp_path)
    h.gh.on_call = lambda args: h.clk.sleep(31)                  # every REST call takes 31 s
    assert h.run("start") == 0
    h.push_and_merge()
    h.at(2026, 11, 10, 7, 21)
    assert h.run("context") == 0
    st = json.loads((h.sess_dir / ".analyst_run" / "state.json").read_text())
    cp = st["canary_probe"]
    assert cp["verdict"] == "incomplete"
    assert cp["outcomes"]["rest.hooks"] == 0                     # started at 93 s, inside the bound
    assert cp["outcomes"]["git.ls_remote_inbox"] is None and cp["pass"]["git.ls_remote_inbox"] is None
    assert cp["outcomes"]["admin_probe"] is None and cp["pass"]["admin_probe"] is None      # not run in time
    assert heads(h, "refs/heads/claude/probe-*") == []


def test_canary_activity_seen_bound_is_20_s(tmp_path):
    h = Harness(tmp_path)
    h.gh.seen_after_polls = 10 ** 9
    assert h.run("start") == 0
    h.push_and_merge()
    h.at(2026, 11, 10, 7, 21)
    assert h.run("context") == 0
    st = json.loads((h.sess_dir / ".analyst_run" / "state.json").read_text())
    assert st["canary_probe"]["outcomes"]["activity_seen_s"] == -1 and h.gh.polls == 5


def test_live_calls_never_run_the_probes(tmp_path):
    h = Harness(tmp_path, mode="live", canary_from=None, live_from="2026-11-10")
    assert h.run("start") == 0
    h.push_and_merge()
    h.at(2026, 11, 10, 7, 21)
    assert h.run("context") == 0
    assert h.gh.calls == [] and heads(h, "refs/heads/claude/probe-*") == []


def test_the_merge_never_reads_a_probe_push(tmp_path):
    h = probe_world(tmp_path)
    run_probe(h)
    new = h.register_pushes(analyst_only=False)
    assert len(new) == 4 and all("/claude/probe-" in r for r, _ in new)
    h.merge()
    assert h.api.issue_comments == [] and h.world.main_list("analyst/canary/witness/") == []


def test_run_window_has_the_probe_subcommand(monkeypatch):
    import run_window
    from ga import session
    monkeypatch.setattr(session.Session, "probe", lambda self: 5)
    assert run_window.main(["probe"]) == 5
