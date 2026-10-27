"""Repository rules: no agent instruction files (R1(a)), vendored files pinned, holiday table reproduced, manifest."""
import base64
import csv
import hashlib
import io
from pathlib import Path

import pytest

from ga import TZDATA_VERSION, frozen

ROOT = Path(__file__).resolve().parents[1]
EPROC_SHA = "ee8d456ae3b5ddd1112bad92d12fffa95d2b12e0559335233c6a9b3a68067973"
BLS_SHA = "7e829d6c5acd861c276552d4086a2c639cbec03fc66242b859cd9d4fabdef45d"
FOMC_SHA = "dd709aee9821d0e1b4a1e9e0a8939422e92bb9520961090e31e96f4d30dc10f5"
REGISTER_HOLIDAYS = {
    ("NYSE", "full"): "2026-11-26 2026-12-25 2027-01-01 2027-01-18 2027-02-15 2027-03-26 2027-05-31 2027-06-18 "
                      "2027-07-05 2027-09-06 2027-11-25 2027-12-24",
    ("NYSE", "early"): "2026-11-27 2026-12-24 2027-11-26",
    ("LSE", "full"): "2026-12-25 2026-12-28 2027-01-01 2027-03-26 2027-03-29 2027-05-03 2027-05-31 2027-08-30 "
                     "2027-12-27 2027-12-28",
    ("LSE", "early"): "2026-12-24 2026-12-31 2027-12-24 2027-12-31",
}


def sha(p):
    return hashlib.sha256((ROOT / p).read_bytes()).hexdigest()


AGENT_FILES = ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".mcp.json")
GUARD_FILES = {".claude/settings.json", ".claude/hooks/guard.py"}


def _tracked():
    import subprocess
    return subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True,
                          check=True).stdout.splitlines()


def test_no_agent_instruction_files():
    """R1(a), R-AN-2 as replaced by §16.18 A7: a one-repository cloud session loads CLAUDE.md, CLAUDE.local.md,
    AGENTS.md and .mcp.json, so none may exist anywhere; `.claude/` exists only at the root (the session guard)."""
    bad = [p for p in ROOT.rglob("*") if ".venv" not in p.parts and ".git" not in p.parts
           and (p.name in AGENT_FILES or (p.name == ".claude" and p != ROOT / ".claude"))]
    assert bad == []
    for p in _tracked():
        parts = p.split("/")
        assert parts[-1] not in AGENT_FILES and ".claude" not in parts[1:], p


def test_dot_claude_holds_only_the_guard():
    """R-AN-2 (§16.18 A7): `.claude/` holds exactly settings.json and hooks/guard.py: no settings.local.json, skills,
    agents, commands or rules. Bytecode caches are never written there (tests load the guard without them)."""
    files = {p.relative_to(ROOT).as_posix() for p in (ROOT / ".claude").rglob("*") if not p.is_dir()}
    dirs = {p.relative_to(ROOT).as_posix() for p in (ROOT / ".claude").rglob("*") if p.is_dir()}
    assert files == GUARD_FILES and dirs == {".claude/hooks"}
    assert {p for p in _tracked() if p.startswith(".claude/")} <= GUARD_FILES


def test_no_judge_and_no_reports_here():
    """R1(a) and R-AN-1: gold-analyst holds no reports, lab, L1 or judge outputs; the analyst judge (`judge_analyst/`,
    its manifest, workflow and outputs) lives in gold-forward-watch (review blockers B1, B3)."""
    import subprocess
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True).stdout.split()
    bad = [p for p in tracked if p.startswith(("judge_analyst/", "reports/", "judge/"))
           or p == ".github/workflows/analyst-judge.yml" or "FROZEN_JUDGE" in p]
    assert bad == []
    assert not (ROOT / "judge_analyst").exists() and not (ROOT / "reports").exists()


def test_vendored_eproc_is_the_labs():
    assert sha("analyst/vendor/eproc.py") == EPROC_SHA


def test_frozen_calendars_are_r_v1_15s():
    assert sha("analyst/calendars/bls_2026_2027.csv") == BLS_SHA
    assert sha("analyst/calendars/fomc_2026_2027.csv") == FOMC_SHA


def test_vendored_tzdata_is_pinned_and_used():
    import sys
    import zoneinfo
    import tzdata
    assert tzdata.__version__ == TZDATA_VERSION == "2026.3"
    assert Path(tzdata.__file__).resolve().is_relative_to(ROOT / "analyst" / "vendor")
    assert zoneinfo.TZPATH == ()
    assert "tzdata" in sys.modules


def test_holiday_table_has_the_registers_rows():
    rows = list(csv.DictReader(io.StringIO((ROOT / "analyst/tables/holidays.csv").read_text())))
    got = {}
    for r in rows:
        got.setdefault((r["exchange"], r["kind"]), []).append(r["date"])
        if r["kind"] == "early":
            assert r["close_utc"] == (r["date"] + ("T18:00:00Z" if r["exchange"] == "NYSE" else "T12:30:00Z"))
        else:
            assert r["close_utc"] == ""
    assert {k: " ".join(v) for k, v in got.items()} == REGISTER_HOLIDAYS


def test_holiday_table_regenerates_with_locked_pmc():
    pytest.importorskip("pandas_market_calendars")
    import importlib.util
    spec = importlib.util.spec_from_file_location("mh", ROOT / "tools" / "make_holidays.py")
    mh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mh)
    assert mh.render("2026-10-01", "2027-12-31") == (ROOT / "analyst/tables/holidays.csv").read_text()


def test_manifest_is_current():
    assert frozen.verify(ROOT) == [], "run: uv run python -I analyst/jobs/make_frozen.py"


def test_manifest_lists_the_frozen_set():
    paths = [l.split("  ", 1)[1] for l in (ROOT / "analyst/FROZEN_ANALYST.sha256").read_text().splitlines()]
    assert paths == sorted(paths)
    for need in ("analyst/ROUTINE_PROMPT.md", "analyst/prompts/system.md", "analyst/prompts/lesson.md",
                 "analyst/prompts/revision.md", "analyst/schema/plan.schema.json", "analyst/run_window.py",
                 "analyst/ga/context.py", "analyst/ga/dref.py", "analyst/ga/settle.py", "analyst/vendor/eproc.py",
                 "analyst/vendor/tzdata/__init__.py", "analyst/tables/holidays.csv", "analyst/playbooks/v0.md",
                 "analyst/PARAMS.json", "analyst/calendars/bls_2026_2027.csv", ".claude/settings.json",
                 ".claude/hooks/guard.py", "analyst/PROBE_SESSION_PROMPT.md",
                 "watcher/worker.js", "watcher/wrangler.toml"):          # watcher/** frozen (§16.18 A14, R-AN-91)
        assert need in paths
    assert not any(p.startswith("analyst/display/") for p in paths)
    assert "analyst/calendars/macro_2026_2027.csv" not in paths          # data, refreshed by appending (A12-D19)
    # §16.18 A14 (R-AN-91, R-AN-2): every tracked file under watcher/ is frozen with the analyst
    assert "watcher/" in frozen.TREES
    tracked = [p for p in _tracked() if p.startswith("watcher/")]
    assert tracked and set(tracked) <= set(paths)


def test_frozen_trees_skip_tool_state(tmp_path):
    """wrangler's `.wrangler/` (git-ignored; `wrangler deploy` writes it under watcher/), `node_modules/` and bytecode
    caches are never part of the frozen set, so a deploy from a checkout leaves the manifest unchanged."""
    for f in frozen.FILES:
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text("x")
    for f in ("watcher/worker.js", "watcher/.wrangler/tmp/bundle.js", "watcher/node_modules/x/index.js",
              "watcher/tests/__pycache__/ref_wakes.cpython-312.pyc", "analyst/ga/__init__.py"):
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text("x")
    got = [p for p in frozen.frozen_paths(tmp_path) if p.startswith(("watcher/", "analyst/ga/"))]
    assert got == ["analyst/ga/__init__.py", "watcher/worker.js"]


def test_tzdata_files_match_the_wheel_record():
    """The vendored tzdata files are the tzdata 2026.3 wheel's (RECORD hashes copied here at the build)."""
    rec = ROOT / "analyst" / "vendor" / "tzdata.RECORD"
    for line in rec.read_text().splitlines():
        p, h, _ = line.split(",")
        if not p.startswith("tzdata/") or not h:
            continue
        dig = base64.urlsafe_b64encode(hashlib.sha256((ROOT / "analyst/vendor" / p).read_bytes()).digest())
        assert h == "sha256=" + dig.rstrip(b"=").decode(), p


SESSION_MODULES = ("__init__", "clock", "context", "dref", "examlock", "gitops", "hours", "memory", "paths", "plan",
                   "probes", "rec", "records", "repo", "session", "store", "tables", "td", "weekly", "witness")


def test_session_modules_use_only_the_standard_library():
    """The routine session imports nothing outside the stdlib and analyst/vendor (R1 Isolation (d))."""
    import ast
    stdlib = {"argparse", "csv", "datetime", "hashlib", "io", "json", "lzma", "math", "os", "re", "shutil",
              "statistics", "struct", "subprocess", "sys", "tempfile", "time", "urllib", "zoneinfo", "pathlib"}
    for f in ["run_window.py"] + [f"ga/{n}.py" for n in SESSION_MODULES]:
        tree = ast.parse((ROOT / "analyst" / f).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                mods = [node.module.split(".")[0]]
            else:
                continue
            for m in mods:
                assert m in stdlib or m in ("ga", "tzdata"), (f, m)


BLOCKER = r"""
import sys, importlib.abc
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("numpy", "pandas", "yaml", "pytest", "pandas_market_calendars"):
            raise ImportError("blocked: " + name)
        return None
sys.meta_path.insert(0, Block())
sys.path.insert(0, "analyst")
before = set(sys.modules)
import run_window
from ga import session, probes, examlock, weekly, gitops, memory, context, hours, store, tables, td
import ga.session as s
for step in ("start", "context", "lock", "lesson_start", "lesson_lock", "revision_start", "revision_lock", "probe"):
    assert callable(getattr(s.Session, step)), step
std = set(sys.stdlib_module_names)
new = sorted({m.split(".")[0] for m in set(sys.modules) - before})
bad = [m for m in new if m not in std and not m.startswith("_sysconfigdata_")
       and m not in ("ga", "tzdata", "run_window")]
print("modules", " ".join(new))
print("bad", " ".join(bad))
for m in new:
    f = getattr(sys.modules.get(m), "__file__", None) or ""
    if m in ("ga", "tzdata", "run_window"):
        assert "/analyst/" in f, (m, f)
"""


def test_session_steps_import_with_numpy_and_pandas_blocked():
    """R-AN-61 (§16.18 A6): the session steps (start, context, lock, lesson-*, revision-*, lock --exam, probe) need
    no package install: imported with numpy and pandas blocked, they load only the standard library, ga/ and the
    vendored tzdata, so the routine network needs no package manager (since §16.18 A9: access level None, with
    the one network secret for api.twelvedata.com)."""
    import subprocess
    import sys
    p = subprocess.run([sys.executable, "-I", "-c", BLOCKER], cwd=ROOT, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr[-2000:]
    bad = [l for l in p.stdout.splitlines() if l.startswith("bad")][0].strip()
    assert bad == "bad", p.stdout
