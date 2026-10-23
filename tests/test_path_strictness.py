"""A pushed path must match an allowlist pattern in full and hold no control character (re-review blocker of
2026-10-23; register R-AN-33 clarification A3 in §16.18).

Python's `$` also matches just before a trailing newline, so `calls/<cid>.json\\n` used to be accepted as a call: its
`called` terminal named a path that `paths.call_path` never produces, and every later analyst-settle run in live mode
raised on it. Adapted from the re-review's probes (scratchpad rereview/probes/test_rereview_probes.py)."""
import datetime as dt
import subprocess

import pytest

from ga import clock, merge, rec, records, settle_run
from ga.repo import DirView
from harness import PLAN, Harness
from synth import git, store_files

W1 = "2026-11-10_W1"
CANARY_ATT = "analyst/canary/attempts/2026-11-10_W1_1.json"


def attempt_bytes(h, draw, n=1):
    return rec.record_bytes(records.attempt("call", "canary", draw, n, draw, "x" * 40,
                                            records.frozen_block(DirView(h.world.seed)), {"note": "x"}))


def push_raw(h, path, body, branch):
    """Push one file under an exact (possibly odd) path, bypassing the working tree."""
    d = h.world.clone(f"raw-{branch}")
    git(d, "checkout", "-q", "-B", "work", "origin/main")
    blob = subprocess.run(["git", "-C", str(d), "hash-object", "-w", "--stdin"], input=body, capture_output=True,
                          check=True).stdout.decode().strip()
    subprocess.run(["git", "-C", str(d), "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}"],
                   check=True)
    git(d, "commit", "-q", "-m", "x")
    git(d, "push", "-q", "-f", "origin", f"HEAD:refs/heads/claude/analyst-{branch}")


def locked_call_body(h, mode):
    """Lock a real call for W1, then take its bytes off the routine's branch and delete that branch."""
    h.at(2026, 11, 10, 7, 21)
    assert h.run("context") == 0
    h.at(2026, 11, 10, 7, 25)
    assert h.run("lock", str(h.write_plan(PLAN))) == 0
    ref = f"refs/heads/claude/analyst-{mode}-call-{W1}"
    tip = git(h.world.origin, "rev-parse", ref)
    cpath = ("analyst/canary/calls/" if mode == "canary" else "analyst/calls/") + f"{W1}.json"
    body = subprocess.run(["git", "-C", str(h.world.origin), "show", f"{tip}:{cpath}"], capture_output=True,
                          check=True).stdout
    git(h.world.origin, "update-ref", "-d", ref)
    return cpath, body


@pytest.mark.parametrize("cid", [W1, f"{W1}\n", f"{W1}\r", f" {W1}", f"{W1}\t"])
def test_parse_call_id_takes_the_whole_string(cid):
    if cid == W1:
        assert clock.parse_call_id(cid)[0] == dt.date(2026, 11, 10)
    else:
        with pytest.raises(ValueError):
            clock.parse_call_id(cid)


@pytest.mark.parametrize("path", [
    f"analyst/canary/calls/{W1}.json\n",
    f"analyst/canary/attempts/{W1}_1.json\n",
    f"analyst/canary/attempts/{W1}_1.json\r",
    f"analyst/canary/lessons/{W1}.json\t",
    "analyst/canary/playbooks/v1.md\n",
    f"analyst/canary/calls/{W1}\n.json",
])
def test_classify_path_rejects_a_trailing_or_embedded_control_character(path):
    assert merge.classify_path(path) == (None, None, None)
    assert merge.classify_path(path.replace("\n", "").replace("\r", "").replace("\t", ""))[0] is not None


def test_a_call_pushed_under_a_trailing_newline_path_is_agent_scope(tmp_path):
    h = Harness(tmp_path)
    assert h.run("start") == 0
    h.push_and_merge()
    cpath, body = locked_call_body(h, "canary")
    push_raw(h, cpath + "\n", body, "nl")
    h.push_and_merge()                                                             # no exception
    w = h.last_witness()
    assert (w["kind"], w["status"], w["reason"], w["path"]) == ("push", "rejected", "agent_scope", cpath + "\n")
    assert h.world.main_list("analyst/canary/calls/") == []
    assert h.main_json(f"analyst/canary/terminal/{W1}.json") is None              # the window is still open


def test_live_settle_goes_on_after_a_trailing_newline_call_push(tmp_path):
    """The re-review's failure: the accepted newline call stopped every later live analyst-settle run."""
    h = Harness(tmp_path, mode="live", canary_from=None, live_from="2026-11-10")
    h.run("start")
    h.push_and_merge()
    cpath, body = locked_call_body(h, "live")
    push_raw(h, cpath + "\n", body, "nl")
    h.push_and_merge()
    assert h.last_witness("live")["reason"] == "agent_scope"
    h.at(2026, 11, 11, 9, 0)
    h.world.owner_commit(store_files(clock.days_range(dt.date(2026, 9, 1), dt.date(2026, 11, 10))), "analyst mirror")
    for i in range(2):
        ws = merge.Workspace(h.world.clone(f"settle{i}"))
        settle_run.run_settle(ws, h.api, h.now, {"workflow": "analyst-settle", "id": 9 + i, "attempt": 1,
                                                 "event": "push"}, verdict=None)   # no exception
    t = h.main_json(f"analyst/terminal/{W1}.json")
    assert t is not None and t["status"] != "called"


def test_a_symlink_at_an_allowed_path_is_agent_scope(tmp_path):
    """Amended R-AN-33: an allowed path that is not a regular file is `agent_scope` (a symlink used to be read as
    the link's target text and logged as `invalid_content`)."""
    h = Harness(tmp_path)
    d = h.world.clone("agent")
    git(d, "checkout", "-q", "-B", "work", "origin/main")
    p = d / CANARY_ATT
    p.parent.mkdir(parents=True, exist_ok=True)
    p.symlink_to("../../MODE.json")
    git(d, "add", "-A")
    git(d, "commit", "-q", "-m", "x")
    git(d, "push", "-q", "-f", "origin", "HEAD:refs/heads/claude/analyst-link")
    h.push_and_merge()
    w = h.last_witness()
    assert (w["kind"], w["status"], w["reason"], w["path"]) == ("push", "rejected", "agent_scope", CANARY_ATT)
    assert h.world.main_file(CANARY_ATT) is None
    assert h.run("start") == 0                                                     # a legitimate attempt still lands
    h.push_and_merge()
    assert h.world.main_file(CANARY_ATT) is not None


def test_one_invalid_content_witness_per_lost_file(tmp_path):
    """The completion pass posts the lost file's `invalid_content` witness; if that run then fails to push, the next
    run must not post a second one for the same push and path."""
    h = Harness(tmp_path)
    assert h.run("start") == 0
    h.merge_fail = True
    [(ref, sha)] = h.register_pushes()
    with pytest.raises(RuntimeError):
        h.merge((ref, sha))
    git(h.world.origin, "update-ref", "-d", ref)
    git(h.world.origin, "reflog", "expire", "--expire=now", "--all")
    git(h.world.origin, "gc", "-q", "--prune=now")
    h.at(2026, 11, 10, 7, 30)
    with pytest.raises(RuntimeError):
        h.merge()                                     # posts the invalid_content witness, then the push fails
    h.merge_fail = False
    h.merge()
    ic = [w for w in h.witnesses() if w["reason"] == "invalid_content"]
    assert len(ic) == 1
    assert (ic[0]["push"]["after"], ic[0]["path"]) == (sha, CANARY_ATT)
