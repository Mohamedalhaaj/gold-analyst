"""analyst-inbox-mirror (register R-AN-83): inbox main only ever moves by fast-forward to a gold-analyst main commit,
never by a force push; an inbox main that is ahead is left alone, and one that has diverged fails the run."""
import os
import stat
import subprocess

import pytest

import inbox_mirror
from synth import World, git


def world(tmp_path):
    return World(tmp_path, {"a.txt": "a"}, inbox=True)


def run_mirror(w, name="m", runner=None):
    d = w.clone(name)
    log = []
    kw = {"runner": runner} if runner else {}
    rc = inbox_mirror.mirror(d, str(w.inbox), env={"GA_TEST_DEPLOY_KEY": "1"}, log=log.append, **kw)
    return rc, log


def test_a_level_inbox_needs_nothing(tmp_path):
    w = world(tmp_path)
    rc, log = run_mirror(w)
    assert rc == 0 and log[-1].endswith(" equal")


def test_the_inbox_is_fast_forwarded_to_main(tmp_path):
    w = world(tmp_path)
    old = w.inbox_main_sha()
    new = w.owner_commit({"b.txt": "b"}, mirror=False)
    assert w.inbox_main_sha() == old
    rc, log = run_mirror(w)
    assert rc == 0 and w.inbox_main_sha() == new
    assert log[-1] == f"head {new} inbox {old} fast_forward pushed"


def test_an_inbox_ahead_of_this_checkout_is_left_alone(tmp_path):
    w = world(tmp_path)
    old = w.main_sha()
    stale = w.clone("stale")                                   # an older run's checkout
    new = w.owner_commit({"b.txt": "b"})                      # a newer run already pushed
    log = []
    rc = inbox_mirror.mirror(stale, str(w.inbox), env={"GA_TEST_DEPLOY_KEY": "1"}, log=log.append)
    assert rc == 0 and w.inbox_main_sha() == new
    assert log[-1] == f"head {old} inbox {new} inbox_ahead"


def test_a_diverged_inbox_fails_and_nothing_is_pushed(tmp_path):
    w = world(tmp_path)
    d = w.clone_inbox("foreign")
    (d / "x.txt").write_text("x")
    git(d, "add", "-A")
    git(d, "commit", "-q", "-m", "foreign")
    git(d, "push", "-q", "origin", "HEAD:main", env={"GA_TEST_DEPLOY_KEY": "1"})
    foreign = w.inbox_main_sha()
    w.owner_commit({"b.txt": "b"}, mirror=False)
    rc, log = run_mirror(w)
    assert rc == 1 and log[-1].endswith(" inbox_diverged") and w.inbox_main_sha() == foreign


def test_the_push_is_never_forced(tmp_path):
    w = world(tmp_path)
    w.owner_commit({"b.txt": "b"}, mirror=False)
    cmds = []

    def runner(argv, **kw):
        cmds.append(list(argv))
        return subprocess.run(argv, **kw)
    rc, _log = run_mirror(w, runner=runner)
    [push] = [c for c in cmds if "push" in c]
    assert rc == 0
    assert not any(a in ("-f", "--force", "--force-with-lease", "--mirror") or a.startswith("--force") for a in push)
    assert push[-1] == f"{w.main_sha()}:refs/heads/main"
    src = (inbox_mirror.__file__ and open(inbox_mirror.__file__).read())
    assert "--force" not in src and '"-f"' not in src


def test_a_refused_push_fails_the_run(tmp_path):
    w = world(tmp_path)
    w.owner_commit({"b.txt": "b"}, mirror=False)
    d = w.clone("m")
    log = []
    rc = inbox_mirror.mirror(d, str(w.inbox), env={}, log=log.append)          # no deploy key: the rules refuse
    assert rc == 1 and log[-1].endswith("fast_forward push_refused")


def test_guard():
    assert inbox_mirror.guard({"GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "push"})
    assert inbox_mirror.guard({"GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "workflow_run",
                               "WORKFLOW_RUN_BRANCH": "main"})
    assert not inbox_mirror.guard({"GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "workflow_run",
                                   "WORKFLOW_RUN_BRANCH": "claude/x"})
    assert not inbox_mirror.guard({"GITHUB_REF": "refs/heads/claude/x", "GITHUB_EVENT_NAME": "workflow_dispatch"})


def test_known_hosts_from_githubs_published_keys():
    meta = {"ssh_keys": ["ssh-ed25519 AAAAC3Nza", "ecdsa-sha2-nistp256 AAAAE2Vj+/=", "ssh-rsa AAAAB3Nz"]}
    assert inbox_mirror.known_hosts(meta) == ("github.com ssh-ed25519 AAAAC3Nza\n"
                                              "github.com ecdsa-sha2-nistp256 AAAAE2Vj+/=\n"
                                              "github.com ssh-rsa AAAAB3Nz\n")
    for bad in ({"ssh_keys": []}, {}, {"ssh_keys": ["ssh-ed25519 AA AA"]}, {"ssh_keys": ["ssh-dss AAAA"]},
                {"ssh_keys": ["ssh-ed25519 AAAA\nevil.example ssh-ed25519 BBBB"]}):
        with pytest.raises(ValueError):
            inbox_mirror.known_hosts(bad)


def test_the_key_file_is_private_and_ends_with_a_newline(tmp_path):
    p = inbox_mirror.write_key(tmp_path, "-----BEGIN X-----\nabc\n-----END X-----")
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert open(p).read().endswith("-----END X-----\n")
    inbox_mirror.remove_key(p)
    assert not os.path.exists(p)
