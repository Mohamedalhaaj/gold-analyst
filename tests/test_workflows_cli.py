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


def test_every_workflow_is_script_only_and_declares_permissions():
    names = sorted(p.name for p in WF.glob("*.yml"))
    assert names == ["analyst-canary-poll.yml", "analyst-deploy.yml", "analyst-merge.yml", "analyst-settle.yml",
                     "analyst-track.yml"]
    for n in names:
        d = load(n)
        assert "permissions" in d, n
        assert "pull_request" not in d["on"] and "pull_request_target" not in d["on"], n
        text = (WF / n).read_text()
        assert "anthropic" not in text.lower() and "claude " not in text.lower()


def test_merge_runs_mains_code_on_claude_pushes():
    d = load("analyst-merge.yml")
    assert d["on"]["push"]["branches"] == ["claude/analyst-**"]
    assert d["concurrency"] == {"group": "analyst", "cancel-in-progress": False}
    steps = d["jobs"]["merge"]["steps"]
    assert steps[0]["uses"].startswith("actions/checkout@") and steps[0]["with"]["ref"] == "main"
    assert "${{ secrets." not in (WF / "analyst-merge.yml").read_text()
    assert d["permissions"] == {"contents": "write", "issues": "write", "actions": "write"}


def test_settle_and_track_and_deploy():
    s = load("analyst-settle.yml")
    assert s["concurrency"]["group"] == "analyst" and s["on"]["push"]["paths"] == ["mirror/**"]
    assert s["on"]["schedule"] == [{"cron": "15 23 * * *"}]
    t = load("analyst-track.yml")
    assert "schedule" not in t["on"] and t["jobs"]["track"]["timeout-minutes"] == 330
    assert "call_id" in t["on"]["workflow_dispatch"]["inputs"]
    assert "${{ inputs.call_id }}" not in t["jobs"]["track"]["steps"][-1]["run"]   # passed via env only
    dep = load("analyst-deploy.yml")
    assert dep["jobs"]["deploy"]["environment"] == "analyst-pages"
    for n in ("analyst-merge.yml", "analyst-settle.yml", "analyst-track.yml", "analyst-canary-poll.yml"):
        assert "environment" not in json.dumps(load(n)["jobs"]), n
    refs = set()
    for p in WF.glob("*.yml"):
        refs |= set(re.findall(r"secrets\.([A-Z_]+)", p.read_text()))
    assert refs == {"CLOUDFLARE_API_TOKEN"}
    assert "secrets.CLOUDFLARE_API_TOKEN" in (WF / "analyst-deploy.yml").read_text()


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
