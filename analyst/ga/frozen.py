"""The frozen set and its manifest (prereg §5, §11; register R-AN-7). FROZEN.

`analyst/FROZEN_ANALYST.sha256` has one line per file, `<sha256>  <path>` (two spaces), sorted by path, LF. It lists
the routine prompts, the system, lesson and revision prompts, the plan schema, run_window.py and every module of
analyst/ga (the context builder, D_ref, settlement, the e-process use, merge and state), the vendored code and data
(analyst/vendor/**, tzdata included), the tables and calendars, playbook v0, PARAMS.json and the canary lesson fixture;
and (§16.18 A7) the session guard's two files and the probe session's prompt (R-AN-85, R-AN-74 C20).
code_sha8 = the first 8 hex of sha256(manifest bytes). Display code (analyst/display) and owner files are outside it.
§16.18 A12-D19: the live macro calendar (DATA_FILES) is data, refreshed by appending issuer-published rows (a logged
data update, analyst/ops_changes.csv), so it is outside the manifest; the judge checks it at each call's
repo_head_sha instead (append-only against the A12 build commit's file).
§16.18 A14 (R-AN-91, R-AN-2): the watcher's code, `watcher/**` (worker.js, wrangler.toml, tests), is frozen with the
analyst; wrangler's git-ignored `.wrangler/` state and any `node_modules/` are never part of the set.
"""
import hashlib
from pathlib import Path

FILES = ("analyst/ROUTINE_PROMPT.md", "analyst/exam/ROUTINE_PROMPT.md", "analyst/prompts/system.md",
         "analyst/prompts/lesson.md", "analyst/prompts/revision.md", "analyst/schema/plan.schema.json",
         "analyst/run_window.py", "analyst/playbooks/v0.md", "analyst/PARAMS.json",
         "analyst/tests/fixtures/canary_settled.json", "analyst/PROBE_SESSION_PROMPT.md", ".claude/settings.json",
         ".claude/hooks/guard.py")
TREES = ("analyst/ga/", "analyst/vendor/", "analyst/tables/", "analyst/calendars/", "watcher/")
SKIP_PARTS = ("__pycache__", ".wrangler", "node_modules")
DATA_FILES = ("analyst/calendars/macro_2026_2027.csv",)


def frozen_paths(root):
    root = Path(root)
    out = set()
    for f in FILES:
        if (root / f).is_file():
            out.add(f)
        else:
            raise FileNotFoundError(f)
    for t in TREES:
        for p in (root / t).rglob("*"):
            rel = p.relative_to(root).as_posix()
            if p.is_file() and not any(s in p.parts for s in SKIP_PARTS) and p.suffix != ".pyc" \
                    and rel not in DATA_FILES:
                out.add(rel)
    return sorted(out)


def manifest_text(root):
    root = Path(root)
    lines = [f"{hashlib.sha256((root / p).read_bytes()).hexdigest()}  {p}\n" for p in frozen_paths(root)]
    return "".join(lines)


def write_manifest(root):
    text = manifest_text(root)
    (Path(root) / "analyst" / "FROZEN_ANALYST.sha256").write_text(text, encoding="utf-8", newline="\n")
    return text


def verify(root):
    """[] if the manifest matches the files; else the differing paths."""
    root = Path(root)
    have = (root / "analyst" / "FROZEN_ANALYST.sha256").read_text("utf-8")
    want = manifest_text(root)
    if have == want:
        return []
    a = {l.split("  ", 1)[1]: l for l in have.splitlines()}
    b = {l.split("  ", 1)[1]: l for l in want.splitlines()}
    return sorted(p for p in set(a) | set(b) if a.get(p) != b.get(p))


def code_sha8(manifest_bytes):
    return hashlib.sha256(manifest_bytes).hexdigest()[:8]
