"""The analyst judge's tamper suite (prereg §8, §10 "The analyst judge's tamper suite"; R-AN-67).

A clean synthetic world must judge SOUND with no finding. Every tamper must be found by the check the register
assigns (R-AN-67), at its severity. Post-hoc tampers act on a copy of the built world (git commits, API double edits,
hour-file double edits); construction tampers rebuild a small world with a hook.
"""
import copy
import datetime as dt
import json
import os
import subprocess

import pytest

from jlib import report
from jlib.common import VIOLATION, WARNING, fmt_utc, record_form, sha256_bytes
from jlib.judge import EVIDENCE_HEADER, Judge
from jlib.sources import FakeGitHub, FakeHourFetcher, HourSource
from world import BOT, ISSUE, T, World


# ---------------------------------------------------------------- fixtures and helpers
@pytest.fixture(scope="session")
def base(tmp_path_factory):
    return World(str(tmp_path_factory.mktemp("world") / "w")).build()


@pytest.fixture
def w(base, tmp_path):
    return base.clone(str(tmp_path / "copy"))


def keys(res, check=None, severity=None, code=None):
    out = []
    for f in res.findings.items:
        if check and f.check != check:
            continue
        if severity and f.severity != severity:
            continue
        if code and f.code != code:
            continue
        out.append(f.key)
    return out


def has(res, check, code, severity):
    return bool(keys(res, check, severity, code))


def owner_commit(world, when="2026-12-07T10:00:00Z", msg="owner edit"):
    return world.repo.commit(msg, T(when))


def call_witness(world, call_id, mode="live"):
    pre = "analyst/" if mode == "live" else "analyst/canary/"
    for name in sorted(os.listdir(os.path.join(world.repo.path, pre + "witness"))):
        with open(os.path.join(world.repo.path, pre + "witness", name), encoding="utf-8") as f:
            r = json.load(f)
        if r["kind"] == "call" and r["id"] == call_id:
            return r
    raise KeyError(call_id)


def attempt_witnesses(world, call_id, mode="live"):
    pre = "analyst/" if mode == "live" else "analyst/canary/"
    out = []
    for name in sorted(os.listdir(os.path.join(world.repo.path, pre + "witness"))):
        with open(os.path.join(world.repo.path, pre + "witness", name), encoding="utf-8") as f:
            r = json.load(f)
        if r["kind"] == "attempt" and r["id"] == call_id:
            out.append(r)
    return out


def set_activity_time(world, after, when):
    for a in world.api["activity"]:
        if a["after"] == after:
            a["timestamp"] = when


def add_witness_with_comment(world, rec, mode="live"):
    """Append a witness file and its comment the way analyst-merge would (for rejected-push tampers)."""
    pre = "analyst/" if mode == "live" else "analyst/canary/"
    seq = max(int(n[:6]) for m in ("analyst/witness", "analyst/canary/witness")
              for n in os.listdir(os.path.join(world.repo.path, m))) + 1
    rec = dict(rec, seq=seq, schema="analyst-witness/1", mode=mode)
    body = Judge.comment_body(dict(rec, comment=None))
    cid = 999000 + seq
    world.api["comments"][str(ISSUE)].append({"id": cid, "body": body, "created_at": "2026-12-06T10:00:05Z",
                                              "user": {"login": BOT}})
    rec["comment"] = {"issue": ISSUE, "id": cid, "created_utc": "2026-12-06T10:00:05Z",
                      "body_sha256": sha256_bytes(body.encode())}
    world.repo.write(f"{pre}witness/{seq:06d}.json", record_form(rec))
    return rec


# ---------------------------------------------------------------- the clean world
def test_clean_world_is_sound(base, tmp_path):
    res = base.judge(out_dir=str(tmp_path))
    assert [f.key for f in res.findings.items] == []
    assert res.summary["contexts_rebuilt"] == 19 and res.summary["settled_checked"] == 10
    assert res.summary["live_expected_windows"] == 20 and res.summary["canary_expected_windows"] == 4
    v = report.write_outputs(res, str(tmp_path), base.now, "testsha8")
    assert v["analyst_verdict"] == "SOUND" and v["violation_keys"] == []
    for name in ("FINDINGS.csv", "VERDICTS.csv", "evidence.csv", "analyst_verdict.json", "REPORT.md",
                 "ADJUDICATIONS.csv"):
        assert os.path.exists(tmp_path / name)
    ev = report.read_csv(str(tmp_path / "evidence.csv"), EVIDENCE_HEADER)
    assert {r["source"] for r in ev} == {"activity", "comment", "run"}
    # a second run with the evidence and the anchor: still SOUND, no evidence row repeated
    res2 = base.judge(out_dir=str(tmp_path), evidence=ev, prev_anchor=report.last_anchor(str(tmp_path)))
    assert res2.findings.items == [] and res2.evidence_new == []
    # model text never reaches the report
    text = (tmp_path / "REPORT.md").read_text(encoding="utf-8")
    assert all(ord(ch) < 128 for ch in text)


def test_evidence_replaces_an_unreachable_api(base, tmp_path):
    res = base.judge(out_dir=str(tmp_path))
    report.write_outputs(res, str(tmp_path), base.now, "x")
    ev = report.read_csv(str(tmp_path / "evidence.csv"), EVIDENCE_HEADER)
    res2 = base.judge(api=FakeGitHub(unreachable=True), evidence=ev)
    assert keys(res2) == ["A0|api_unreachable|api|-"]          # lock times recomputed from the evidence file
    v, *_ = report.decide(res2.findings, str(tmp_path), base.now)
    assert v == "WARNING"                                    # never SOUND


# ---------------------------------------------------------------- A1 lock time
def test_A1_late_lock_on_a_counted_call(w):
    cw = call_witness(w, "2026-11-30_W2")
    set_activity_time(w, cw["push"]["after"], "2026-11-30T14:20:00Z")    # deadline 14:15
    res = w.judge()
    assert has(res, "A1", "late_lock", VIOLATION)
    assert has(res, "A11", "server_time", WARNING)


# ---------------------------------------------------------------- A2 attempts
def test_A2_attempt_outside_span_marked_non_void(w):
    a = attempt_witnesses(w, "2026-11-30_W1")[0]
    set_activity_time(w, a["push"]["after"], "2026-11-30T07:10:00Z")    # entry - 50 min
    res = w.judge()
    assert has(res, "A2", "attempt_window", VIOLATION)


def test_A2_call_without_an_earlier_attempt(w):
    a = attempt_witnesses(w, "2026-12-01_W1")[0]
    cw = call_witness(w, "2026-12-01_W1")
    lock = cw["window"]["time_utc"]
    set_activity_time(w, a["push"]["after"], fmt_utc(T(lock) + dt.timedelta(seconds=30)))
    res = w.judge()
    assert has(res, "A2", "no_attempt", VIOLATION)


def test_A2_and_A3_a_third_draw(w):
    rec = {"kind": "attempt", "id": "2026-12-02_W2", "status": "rejected", "reason": "attempt_cap",
           "path": "analyst/attempts/2026-12-02_W2_3.json", "file_sha256": "0" * 64, "push": None,
           "run": {"workflow": "analyst-merge", "id": 1, "attempt": 1, "event": "push", "created_utc":
                   "2026-12-02T14:10:00Z"}, "window": None, "terminal": None, "written_utc": "2026-12-06T10:00:00Z"}
    add_witness_with_comment(w, rec)
    owner_commit(w)
    res = w.judge()
    assert has(res, "A3", "attempt_cap", VIOLATION)


# ---------------------------------------------------------------- A3 scope
def test_A3_agent_scope_witness(w):
    rec = {"kind": "push", "id": None, "status": "rejected", "reason": "agent_scope", "path": "analyst/STATE.json",
           "file_sha256": "1" * 64, "push": None, "run": None, "window": None, "terminal": None,
           "written_utc": "2026-12-06T10:00:00Z"}
    add_witness_with_comment(w, rec)
    owner_commit(w)
    res = w.judge()
    assert has(res, "A3", "agent_scope", VIOLATION)


def _unwitnessed_push(world, files, branch):
    r = world.repo
    r("switch", "-q", "-c", branch, r.head())
    for p, data in files.items():
        r.write(p, data)
    sha = r.commit("session push", T("2026-12-05T07:00:00Z"))
    r("switch", "-q", "main")
    world.activity(f"refs/heads/{branch}", sha, T("2026-12-05T07:00:00Z"), "claude[bot]", "branch_creation")
    return sha


def test_A3_unwitnessed_push_out_of_scope(w):
    _unwitnessed_push(w, {"analyst/calls/2026-12-07_W1.json": b"{}\n", "analyst/STATE.json": b"{}\n"},
                      "claude/analyst-live-call-x")
    res = w.judge()
    assert has(res, "A3", "unwitnessed_out_of_scope", VIOLATION)


def test_A3_unwitnessed_conforming_push_is_a_warning(w):
    _unwitnessed_push(w, {"analyst/calls/2026-12-07_W1.json": b"{}\n"}, "claude/analyst-live-call-y")
    res = w.judge()
    assert has(res, "A3", "unwitnessed_push", WARNING) and not keys(res, severity=VIOLATION)


def test_A3_accepted_push_whose_diff_broke_scope(tmp_path):
    def extra(world):
        world.hooks["extra_push_file"] = {"analyst/calls/2026-11-30_W1.json": ("analyst/README_x.md", b"x\n")}
    world = small_world(tmp_path, {"build": extra})
    res = world.judge()
    assert has(res, "A3", "accepted_out_of_scope", VIOLATION)


def test_A3_record_on_main_without_witness_and_forbidden_files(w):
    w.repo.write("analyst/calls/2026-12-07_W2.json", b"{}\n")
    w.repo.write("docs/CLAUDE.md", b"x\n")
    owner_commit(w)
    res = w.judge()
    assert has(res, "A3", "unwitnessed_record", VIOLATION)
    assert has(res, "A3", "forbidden_file", VIOLATION)


def test_A3_push_to_main_by_another_actor(w):
    w.activity("refs/heads/main", "f" * 40, T("2026-12-05T08:00:00Z"), "claude[bot]", "push")
    res = w.judge()
    assert has(res, "A3", "main_push_actor", VIOLATION)


# ---------------------------------------------------------------- A4 / A5 contexts and derivations (construction)
SMALL = dict(canary_days=(), live_from="2026-11-30", live_to="2026-12-01", now="2026-12-02T23:00:00Z")


def small_world(tmp_path, hooks):
    return World(str(tmp_path / "small"), hooks=hooks, **SMALL).build()


def test_small_world_is_sound(tmp_path):
    res = small_world(tmp_path, {}).judge()
    assert keys(res) == []


def test_A4_context_bar_changed(tmp_path):
    def hook(world, win, ctx):
        if win.call_id == "2026-11-30_W2":
            ctx["bars_h1"][-3]["c"] += 0.5
    res = small_world(tmp_path, {"context": hook}).judge()
    assert has(res, "A4", "context_differs", VIOLATION)
    assert not keys(res, "A5")


def test_A4_and_A5_p_ref_changed(tmp_path):
    def hook(world, win, ctx):
        if win.call_id == "2026-11-30_W1":
            ctx["p_ref"]["p_ref"] += 1.0
    res = small_world(tmp_path, {"context": hook}).judge()
    assert has(res, "A4", "context_differs", VIOLATION)
    assert has(res, "A5", "d_ref_underivable", VIOLATION)


def test_A4_inadmissible_hour(tmp_path):
    def hook(world, win, files):
        if win.call_id == "2026-12-01_W1":
            f = dict(files[0])
            h = T(files[len([x for x in files if x["sym"] == "XAUUSD"]) - 1]["hour_utc"]) + dt.timedelta(hours=1)
            f["hour_utc"] = fmt_utc(h)
            from jlib import ticks
            f["url"] = ticks.hour_url("XAUUSD", h)
            body, n = world.tick_file("XAUUSD", h)
            world.hour_files[f["url"]] = (200, body)
            f.update(bytes=len(body), sha256=sha256_bytes(body), ticks=n)
            files.insert(len([x for x in files if x["sym"] == "XAUUSD"]), f)
    res = small_world(tmp_path, {"files": hook}).judge()
    assert has(res, "A4", "inadmissible_hour", VIOLATION)


def test_A4_asof_after_lock(tmp_path):
    def hook(world):
        win_lock = T("2026-11-30T07:18:00Z")    # asof is 07:22:00
        world.hooks["lock_time"] = {"2026-11-30_W1": win_lock}
    res = small_world(tmp_path, {"build": hook}).judge()
    assert has(res, "A4", "asof_after_lock", VIOLATION)


def test_A5_s_off_by_one_dime(tmp_path):
    def hook(world, win, call):
        if win.call_id == "2026-11-30_W2":
            call["S_dimes"] += 1
            call["S"] = call["S_dimes"] / 10
    res = small_world(tmp_path, {"call": hook}).judge()
    assert has(res, "A5", "st_underivable", VIOLATION)


def test_A5_invalid_plan(tmp_path):
    def hook(world, win, call):
        if win.call_id == "2026-12-01_W1":
            call["plan"]["stop_mult"] = 1.25
    res = small_world(tmp_path, {"call": hook}).judge()
    assert has(res, "A5", "plan_invalid", VIOLATION)


def test_A4_A5_d_ref_changed_in_context_and_call(tmp_path):
    def hook(world, win, call):
        if win.call_id == "2026-11-30_W1":
            call["context"]["d_ref"]["D_ref"] += 1.0
            call["context"]["d_ref"]["D_ref_cents"] += 100
            call["d_ref"] = call["context"]["d_ref"]
            from jlib.common import sha256_compact
            call["context_sha256"] = sha256_compact(call["context"])
    res = small_world(tmp_path, {"call": hook}).judge()
    assert has(res, "A4", "context_differs", VIOLATION)
    assert has(res, "A5", "d_ref_underivable", VIOLATION)


def test_A11_hour_redownload_differs_is_a_warning_not_A4(base, tmp_path):
    url = sorted(u for u in base.hour_files if "/XAUUSD/" in u)[5]
    files = dict(base.hour_files)
    http, body = files[url]
    from world import lzma_alone
    import lzma
    raw = lzma.decompress(body, format=lzma.FORMAT_ALONE)
    files[url] = (200, lzma_alone(raw[:-20]))
    hs = HourSource(FakeHourFetcher(files), None, base.now)
    res = base.judge(hours=hs)
    assert has(res, "A11", "hour_sha", WARNING)
    assert not keys(res, "A4")


def test_A0_hours_unavailable(base):
    hs = HourSource(FakeHourFetcher({}, down=True), None, base.now)
    res = base.judge(hours=hs)
    assert has(res, "A0", "hours_unavailable", WARNING) and not keys(res, severity=VIOLATION)


# ---------------------------------------------------------------- A6 chain and witness comments
def test_A6_broken_chain(tmp_path):
    def hook(world, t):
        if t["call_id"] == "2026-12-01_W1":
            t["prev_chain_sha256"] = "e" * 64
    res = small_world(tmp_path, {"terminal": hook}).judge()
    assert has(res, "A6", "broken_chain", VIOLATION)


def test_A6_comment_edited(w):
    c = w.api["comments"][str(ISSUE)][3]
    c["body"] = c["body"].replace('"status":"accepted"', '"status":"rejected"')
    res = w.judge()
    assert has(res, "A6", "payload_differs", VIOLATION)


def test_A6_comment_deleted(w):
    del w.api["comments"][str(ISSUE)][5]
    res = w.judge()
    assert has(res, "A6", "file_without_comment", VIOLATION)


def test_A6_comment_without_file(w):
    w.api["comments"][str(ISSUE)].append({"id": 1, "body": "analyst-witness/1 call x seq 9999\n{}",
                                          "created_at": "2026-12-01T00:00:00Z", "user": {"login": BOT}})
    w.api["comments"][str(ISSUE)].append({"id": 2, "body": "analyst-witness/1 call x seq 9998\n{}",
                                          "created_at": "2026-12-01T00:00:00Z", "user": {"login": "someone"}})
    res = w.judge()
    assert keys(res, "A6", VIOLATION, "comment_without_file") == ["A6|comment_without_file|comment/1|-"]


# ---------------------------------------------------------------- A7 history and anchor
def test_A7_create_only_modified_and_deleted(w):
    p = os.path.join(w.repo.path, "analyst/attempts/2026-11-30_W1_1.json")
    with open(p, "ab") as f:
        f.write(b" ")
    os.remove(os.path.join(w.repo.path, "analyst/twins/2026-11-30_W1.json"))
    sha = owner_commit(w)
    res = w.judge()
    assert f"A7|modified|analyst/attempts/2026-11-30_W1_1.json|{sha}" in keys(res)
    assert f"A7|deleted|analyst/twins/2026-11-30_W1.json|{sha}" in keys(res)


def test_A7_append_only_break(w):
    p = os.path.join(w.repo.path, "mirror/system/days.csv")
    data = open(p, "rb").read().replace(b",missing,", b",missinG,", 1)
    with open(p, "wb") as f:
        f.write(data)
    owner_commit(w)
    res = w.judge()
    assert has(res, "A7", "append_break", VIOLATION)


def test_A7_anchor_not_an_ancestor(base):
    res = base.judge(prev_anchor=base.src_sha)
    assert has(res, "A7", "anchor", VIOLATION)


def test_A7_main_force_push(w):
    w.activity("refs/heads/main", "a" * 40, T("2026-12-05T08:00:00Z"), "Mohamedalhaaj", "force_push")
    res = w.judge()
    assert has(res, "A7", "main_rewrite", VIOLATION)


# ---------------------------------------------------------------- A8 completeness
def test_A8_window_without_terminal(tmp_path):
    world = World(str(tmp_path / "s8"), hooks={}, plans={"2026-11-30_W2": ("missed",)}, **SMALL)
    world.hooks["skip_close"] = {"2026-11-30_W2"}
    res = world.build().judge()
    assert keys(res, "A8", VIOLATION, "missing_terminal") == ["A8|missing_terminal|analyst/terminal/2026-11-30_W2.json|-"]


def test_A8_two_terminal_witnesses(w):
    t = json.load(open(os.path.join(w.repo.path, "analyst/terminal/2026-12-02_W1.json"), encoding="utf-8"))
    tb = record_form(t)
    rec = {"kind": "terminal", "id": "2026-12-02_W1", "status": "accepted", "reason": None,
           "path": "analyst/terminal/2026-12-02_W1.json", "file_sha256": sha256_bytes(tb), "push": None,
           "run": None, "window": None,
           "terminal": {"status": t["status"], "detail": t["detail"], "terminal_sha256": sha256_bytes(tb),
                        "prev_chain_sha256": t["prev_chain_sha256"], "call_sha256": None, "push_sha": None},
           "written_utc": "2026-12-06T10:00:00Z"}
    add_witness_with_comment(w, rec)
    owner_commit(w)
    res = w.judge()
    assert has(res, "A8", "terminal_witness_count", VIOLATION)


# ---------------------------------------------------------------- A9 START
def test_A9_wrong_start(tmp_path):
    def hook(world, st):
        st["first_day"] = "2026-12-14"
    res = small_world(tmp_path, {"start": hook}).judge()
    assert has(res, "A9", "start_differs", VIOLATION)


# ---------------------------------------------------------------- A10 owner-attested runs
def test_A10_in_span_run_without_attempt_and_other_model(tmp_path):
    def runs(world, weeks):
        wk = sorted(weeks)[0]
        weeks[wk].append({"routine": "analyst-w2", "run_id": "r-extra", "session_id": "s-extra",
                          "created_utc": "2026-12-01T13:50:00Z", "status": "success", "model": "claude-opus-5-5",
                          "model_source": "get_run_log"})
        for x in weeks[wk]:
            if x["run_id"] == "r-2026-11-30_W2-1":
                x["model"] = "claude-sonnet-5"
    world = World(str(tmp_path / "s10"), hooks={"runs": runs}, plans={"2026-12-01_W2": ("missed",)}, **SMALL)
    res = world.build().judge()
    assert has(res, "A10", "in_span_without_attempt", VIOLATION)
    assert has(res, "A10", "run_model", VIOLATION)
    f = [x for x in res.findings.items if x.check == "A10"]
    assert all(x.owner_attested for x in f)
    assert has(res, "A11", "overlay_missing", WARNING)         # no PAUSED model:<run_id> event recorded


# ---------------------------------------------------------------- A11 recomputations
def test_A11_settled_record_changed(tmp_path):
    def hook(world, win, rec):
        if win.call_id == "2026-11-30_W2":
            rec["outcome"]["R"] += 0.01
    res = small_world(tmp_path, {"settled": hook}).judge()
    assert has(res, "A11", "settled_mismatch", WARNING)
    assert not keys(res, severity=VIOLATION)


def test_A11_state_and_ledger_snapshots(w):
    p = os.path.join(w.repo.path, "analyst/STATE.json")
    s = json.load(open(p, encoding="utf-8"))
    s["n"] += 1
    with open(p, "wb") as f:
        f.write(record_form(s))
    lp = os.path.join(w.repo.path, "analyst/ledger.csv")
    data = open(lp, "rb").read().replace(b"called,called,,1,", b"called,called,,2,", 1)
    with open(lp, "wb") as f:
        f.write(data)
    owner_commit(w)
    res = w.judge()
    assert has(res, "A11", "state_snapshot", WARNING)
    assert has(res, "A11", "ledger_mismatch", WARNING)
    assert not keys(res, severity=VIOLATION)


def test_A11_mirror_differs_from_source(w):
    p = os.path.join(w.src.path, "data/system/raw/2026/11/30/BID.bi5")
    with open(p, "ab") as f:
        f.write(b"\0")
    w.src.commit("source edit", T("2026-12-07T10:00:00Z"))
    res = w.judge()
    assert has(res, "A11", "mirror_raw", WARNING)


def test_A0_source_store_unavailable(base):
    res = base.judge(store=False)
    assert keys(res) == ["A0|source_store_unavailable|mirror/system/|-"]


def test_A11_frozen_inputs(w):
    with open(os.path.join(w.repo.path, "analyst/vendor/eproc.py"), "ab") as f:
        f.write(b"# edit\n")
    p = os.path.join(w.repo.path, "analyst/tables/holidays.csv")
    data = open(p, "rb").read().replace(b"2026-11-26,NYSE,full,\n", b"")
    with open(p, "wb") as f:
        f.write(data)
    owner_commit(w)
    res = w.judge()
    assert has(res, "A11", "eproc_sha", WARNING)
    assert has(res, "A11", "holiday_table", WARNING)
    assert has(res, "A11", "frozen_drift", WARNING)


# ---------------------------------------------------------------- A0 shallow clone
def test_A0_shallow_clone(base, tmp_path):
    dest = str(tmp_path / "shallow")
    subprocess.run(["git", "clone", "-q", "--depth", "3", f"file://{base.repo.path}", dest], check=True)
    from jlib.gitrepo import GitRepo
    git = GitRepo(dest)
    j = Judge(git, base.now, api=FakeGitHub(copy.deepcopy(base.api)),
              hours=HourSource(FakeHourFetcher(base.hour_files), None, base.now))
    res = j.run()
    git.close()
    assert has(res, "A0", "shallow_clone", WARNING)


# ---------------------------------------------------------------- latching and clearing (R-JV-1..4 analogues)
def test_violation_latches_until_cleared(base, w, tmp_path):
    out = str(tmp_path / "out")
    cw = call_witness(w, "2026-11-30_W2")
    set_activity_time(w, cw["push"]["after"], "2026-11-30T14:20:00Z")
    r1 = w.judge()
    v1 = report.write_outputs(r1, out, w.now, "x")
    assert v1["analyst_verdict"] == "VIOLATION"
    key = [k for k in v1["violation_keys"] if k.startswith("A1|late_lock|")][0]
    # the API entry is restored (it "aged out"): the key stays in force
    r2 = base.judge()
    v2 = report.write_outputs(r2, out, "2026-12-07T13:00:00Z", "x")
    assert v2["analyst_verdict"] == "VIOLATION" and key in v2["violation_keys"]
    # a clear row naming another key clears nothing; the exact key clears it
    with open(os.path.join(out, "ADJUDICATIONS.csv"), "a", encoding="utf-8") as f:
        f.write("2026-12-07T13:30:00Z,latch,A1,A1|late_lock|other|-,clear,wrong key\n")
    assert report.write_outputs(base.judge(), out, "2026-12-07T13:40:00Z", "x")["analyst_verdict"] == "VIOLATION"
    with open(os.path.join(out, "ADJUDICATIONS.csv"), "a", encoding="utf-8") as f:
        f.write(f"2026-12-07T14:00:00Z,latch,A1,{key},clear,adjudicated\n")
    r3 = base.judge()
    v3 = report.write_outputs(r3, out, "2026-12-07T14:10:00Z", "x")
    assert v3["analyst_verdict"] != "VIOLATION" and key in v3["cleared_keys"]
    assert "cleared 2026-12-07T14:00:00Z" in open(os.path.join(out, "REPORT.md"), encoding="utf-8").read()


def test_warning_adjudication_and_system_error(w, tmp_path):
    out = str(tmp_path / "out")
    p = os.path.join(w.repo.path, "analyst/STATE.json")
    s = json.load(open(p, encoding="utf-8"))
    s["n"] += 1
    with open(p, "wb") as f:
        f.write(record_form(s))
    owner_commit(w)
    v = report.write_outputs(w.judge(), out, w.now, "x")
    assert v["analyst_verdict"] == "WARNING"
    with open(os.path.join(out, "ADJUDICATIONS.csv"), "a", encoding="utf-8") as f:
        f.write("2026-12-07T13:00:00Z,warning,A11,A11|state_snapshot|analyst/STATE.json|-,system_error,bug\n")
    v = report.write_outputs(w.judge(), out, "2026-12-07T13:10:00Z", "x")
    assert v["analyst_verdict"] == "VIOLATION"
    with open(os.path.join(out, "ADJUDICATIONS.csv"), "a", encoding="utf-8") as f:
        f.write("2026-12-07T13:20:00Z,warning,A11,A11|state_snapshot|analyst/STATE.json|-,resolved,fixed\n")
    v = report.write_outputs(w.judge(), out, "2026-12-07T13:30:00Z", "x")
    assert v["analyst_verdict"] == "SOUND"


# ---------------------------------------------------------------- robustness: malformed records never crash the run
def test_malformed_records_are_findings_not_crashes(w):
    w.repo.write("analyst/witness/999999.json", b"[1, 2]\n")
    w.repo.write("analyst/terminal/2026-12-07_W1.json", b"not json\n")
    w.repo.write("analyst/settled/2026-12-02_W2.json", b"{}\n")
    owner_commit(w)
    res = w.judge()
    assert has(res, "A6", "unreadable_witness", VIOLATION)
    assert has(res, "A6", "unreadable_terminal", VIOLATION)
    assert not keys(res, "A0", code="check_crashed")
