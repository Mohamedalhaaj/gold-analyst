"""Workflow safety (prereg §9; register R-AN-32, R-AN-75), no secrets in files, the CLI, and `lock --exam`."""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from ga import rec
from synth import World, git

ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / ".github" / "workflows"


def load(name):
    doc = yaml.safe_load((WF / name).read_text())
    doc["on"] = doc.pop(True) if True in doc else doc["on"]       # YAML 1.1 reads `on` as True
    return doc


NAMES = ["analyst-canary-poll.yml", "analyst-deploy.yml", "analyst-inbox-mirror.yml", "analyst-merge.yml",
         "analyst-settle.yml", "analyst-track.yml"]
FORBIDDEN_EVENTS = ("pull_request", "pull_request_target", "issue_comment", "issues", "repository_dispatch",
                    "pull_request_review", "pull_request_review_comment", "discussion", "discussion_comment")
MERGE_CRONS = ["*/5 6-8,12-14 * * 1-5", "*/10 16-17 * * 2-6", "*/10 18-19,22-23 * * 6", "33 * * * *"]


def guard_step(steps):
    """The first step of every job that writes main, posts on the issue or holds a secret: it exits 1 unless
    GITHUB_REF is refs/heads/main (register R-AN-75 item 4)."""
    g = steps[0]
    assert "uses" not in g and "refs/heads/main" in g["run"] and "exit 1" in g["run"], g
    return g


def test_every_workflow_is_script_only_and_declares_permissions():
    names = sorted(p.name for p in WF.glob("*.yml"))
    assert names == NAMES
    for n in names:
        d = load(n)
        assert "permissions" in d, n
        assert not set(d["on"]) & set(FORBIDDEN_EVENTS), n
        text = (WF / n).read_text()
        assert "anthropic" not in text.lower() and "claude " not in text.lower()


def test_the_trigger_table_of_r_an_75_item_4():
    want = {
        "analyst-merge.yml": {"schedule", "workflow_dispatch"},
        "analyst-settle.yml": {"push", "schedule", "workflow_dispatch"},
        "analyst-deploy.yml": {"workflow_dispatch", "schedule"},
        "analyst-track.yml": {"workflow_dispatch"},
        "analyst-canary-poll.yml": {"schedule", "workflow_dispatch"},
        "analyst-inbox-mirror.yml": {"push", "workflow_run", "schedule", "workflow_dispatch"},
    }
    perms = {
        "analyst-merge.yml": {"contents": "write", "issues": "write", "actions": "write"},
        "analyst-settle.yml": {"contents": "write", "issues": "write", "actions": "write"},
        "analyst-deploy.yml": {"contents": "read"},
        "analyst-track.yml": {"contents": "write", "actions": "write"},
        "analyst-canary-poll.yml": {"contents": "write"},
        "analyst-inbox-mirror.yml": {"contents": "read"},
    }
    for n in NAMES:
        d = load(n)
        assert set(d["on"]) == want[n], n
        assert d["permissions"] == perms[n], n
        for job in d["jobs"].values():
            guard_step(job["steps"])
            co = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout@")]
            assert co and co[0]["with"]["ref"] == "main", n


def test_merge_polls_and_runs_mains_code():
    d = load("analyst-merge.yml")
    assert "push" not in d["on"]
    assert [c["cron"] for c in d["on"]["schedule"]] == MERGE_CRONS
    assert d["concurrency"] == {"group": "analyst", "cancel-in-progress": False}
    steps = d["jobs"]["merge"]["steps"]
    g = guard_step(steps)
    assert "schedule" in g["run"] and "workflow_dispatch" in g["run"] and "GITHUB_EVENT_NAME" in g["run"]
    assert steps[1]["uses"].startswith("actions/checkout@") and steps[1]["with"]["ref"] == "main"
    assert "${{ secrets." not in (WF / "analyst-merge.yml").read_text()
    assert "environment" not in d["jobs"]["merge"]


def test_settle_and_track_and_deploy():
    s = load("analyst-settle.yml")
    assert s["concurrency"] == {"group": "analyst", "cancel-in-progress": False}
    assert s["on"]["push"] == {"branches": ["main"], "paths": ["mirror/**"]}
    assert s["on"]["schedule"] == [{"cron": "15 23 * * *"}]
    t = load("analyst-track.yml")
    assert "schedule" not in t["on"] and t["jobs"]["track"]["timeout-minutes"] == 330
    assert "call_id" in t["on"]["workflow_dispatch"]["inputs"]
    assert "${{ inputs.call_id }}" not in t["jobs"]["track"]["steps"][-1]["run"]   # passed via env only
    dep = load("analyst-deploy.yml")
    assert "workflow_run" not in dep["on"] and dep["on"]["schedule"] == [{"cron": "50 23 * * *"}]
    assert dep["jobs"]["deploy"]["environment"] == "analyst-pages"
    assert dep["concurrency"] == {"group": "analyst-deploy", "cancel-in-progress": True}
    for n in ("analyst-merge.yml", "analyst-settle.yml", "analyst-track.yml", "analyst-canary-poll.yml"):
        assert "environment" not in json.dumps(load(n)["jobs"]), n
    refs = {}
    for p in WF.glob("*.yml"):
        for r in re.findall(r"secrets\.([A-Z_]+)", p.read_text()):
            refs.setdefault(r, set()).add(p.name)
    assert refs == {"CLOUDFLARE_API_TOKEN": {"analyst-deploy.yml"}, "INBOX_MIRROR_KEY": {"analyst-inbox-mirror.yml"}}


def test_inbox_mirror_workflow():
    d = load("analyst-inbox-mirror.yml")
    assert d["on"]["push"] == {"branches": ["main"]}
    wr = d["on"]["workflow_run"]
    assert sorted(wr["workflows"]) == ["analyst-canary-poll", "analyst-merge", "analyst-settle", "analyst-track"]
    assert wr["types"] == ["completed"] and wr["branches"] == ["main"]
    assert d["on"]["schedule"] == [{"cron": "10,40 * * * *"}]
    assert d["concurrency"]["group"] == "analyst-inbox-mirror"
    job = d["jobs"]["mirror"]
    assert job["environment"] == "analyst-inbox"
    g = guard_step(job["steps"])
    assert "workflow_run" in g["run"] and "head_branch" in json.dumps(g)
    co = job["steps"][1]
    assert co["uses"].startswith("actions/checkout@") and co["with"] == {"ref": "main", "fetch-depth": 0,
                                                                           "persist-credentials": False}
    text = (WF / "analyst-inbox-mirror.yml").read_text()
    assert "--force" not in text and "secrets.INBOX_MIRROR_KEY" in text
    run = job["steps"][-1]
    assert run["env"] == {"INBOX_MIRROR_KEY": "${{ secrets.INBOX_MIRROR_KEY }}",
                          "WORKFLOW_RUN_BRANCH": "${{ github.event.workflow_run.head_branch }}"}
    assert run["run"].strip() == "python -I analyst/jobs/inbox_mirror.py"


def test_the_workflow_names_match_the_mirror_triggers():
    for n in ("analyst-merge.yml", "analyst-settle.yml", "analyst-canary-poll.yml", "analyst-track.yml"):
        assert load(n)["name"] == n[:-4]


def test_no_secret_material_in_files():
    # a 32-hex run must stand alone: the tail of a 40-hex git sha (STATE.json's head_sha) is not a key
    pat = re.compile(r"(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-ant-[A-Za-z0-9-]{10,}|"
                     r"-----BEGIN [A-Z ]*PRIVATE KEY-----|(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f]))")
    allowed_hex = re.compile(r"^[0-9a-f]{64}$")
    for p in ROOT.rglob("*"):
        if not p.is_file() or ".venv" in p.parts or ".git" in p.parts or "vendor" in p.parts \
                or ".pytest_cache" in p.parts or "__pycache__" in p.parts \
                or p.suffix in (".pyc", ".lock") or p.name == "uv.lock":
            continue
        try:
            text = p.read_text("utf-8")
        except UnicodeDecodeError:
            continue
        for m in pat.finditer(text):
            tok = m.group(0)
            if re.fullmatch(r"[0-9a-f]{32}", tok):
                start = m.start()
                window = text[max(0, start - 40):start + 70]
                if re.search(r"[0-9a-f]{64}", window):          # part of a sha256
                    continue
            raise AssertionError(f"secret-like string in {p.relative_to(ROOT)}: {tok[:12]}...")
    assert "940e834e" not in (ROOT / "deploy/wrangler.jsonc").read_text()


def test_cli_start_in_a_local_world(tmp_path):
    """The real entry point, with the real clock: it refuses outside a span or pushes an attempt (never fails)."""
    files = {}
    for p in ROOT.joinpath("analyst").rglob("*"):
        if p.is_file() and "__pycache__" not in p.parts:
            files[p.relative_to(ROOT).as_posix()] = p.read_bytes()
    w = World(tmp_path, files)
    d = w.clone("s")
    out = subprocess.run([sys.executable, "-I", "analyst/run_window.py", "start"], cwd=d, capture_output=True,
                         text=True, env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": str(tmp_path),
                                         "GIT_AUTHOR_NAME": "t", "GIT_COMMITTER_NAME": "t"})
    assert out.returncode == 0, out.stdout + out.stderr
    assert re.fullmatch(r"(refused \S+|attempt_pushed \S+ n=1 mode=canary)\n", out.stdout), out.stdout


def test_exam_lock(tmp_path):
    """`lock --exam` in a repository holding exactly what the exam reset puts there (no settlement code)."""
    keep = ["analyst/run_window.py", "analyst/ga/__init__.py", "analyst/ga/rec.py", "analyst/ga/plan.py",
            "analyst/ga/gitops.py", "analyst/ga/examlock.py", "analyst/schema/plan.schema.json",
            "analyst/prompts/system.md", "analyst/playbooks/v0.md"]
    files = {p: (ROOT / p).read_bytes() for p in keep}
    for p in (ROOT / "analyst/vendor/tzdata").rglob("*"):
        if p.is_file() and "__pycache__" not in p.parts:
            files[p.relative_to(ROOT).as_posix()] = p.read_bytes()
    files["ROUTINE_PROMPT.md"] = (ROOT / "analyst/exam/ROUTINE_PROMPT.md").read_bytes()
    ctx = {"schema": "analyst-exam-context/1", "exam_id": "0123456789ab", "bars_h1": [], "d_ref": {"D_ref": 5.0}}
    files["context.json"] = rec.record_bytes(ctx)
    w = World(tmp_path, files)
    d = w.clone("exam")
    (d / ".analyst_run").mkdir()
    plan = d / ".analyst_run" / "plan.json"
    env = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": str(tmp_path), "GIT_AUTHOR_NAME": "t",
           "GIT_COMMITTER_NAME": "t"}
    plan.write_text(json.dumps({"action": "sell", "stop_mult": 9, "target_mult": 1.0, "confidence": "low",
                                "reason_ar": "س", "key_risks_ar": ""}))
    out = subprocess.run([sys.executable, "-I", "analyst/run_window.py", "lock", "--exam", str(plan)], cwd=d,
                         capture_output=True, text=True, env=env)
    assert out.returncode == 2 and out.stdout.startswith("invalid stop_mult")
    plan.write_text(json.dumps({"action": "sell", "stop_mult": 2.0, "target_mult": 1.0, "confidence": "low",
                                "reason_ar": "س", "key_risks_ar": ""}))
    out = subprocess.run([sys.executable, "-I", "analyst/run_window.py", "lock", "--exam", str(plan)], cwd=d,
                         capture_output=True, text=True, env=env)
    assert out.returncode == 0 and out.stdout == "exam_locked 0123456789ab status=called\n", out.stdout + out.stderr
    rec_b = subprocess.run(["git", "-C", str(w.origin), "show", "claude/exam-0123456789ab:plans/0123456789ab.json"],
                           capture_output=True).stdout
    doc = json.loads(rec_b)
    assert doc["plan"]["action"] == "sell" and doc["lock_validation_failures"] == 1
    assert doc["context_sha256"] == rec.compact_sha256(ctx)
