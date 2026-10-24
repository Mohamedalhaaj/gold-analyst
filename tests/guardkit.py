"""Helpers for the session guard's tests (register R-AN-85, §16.18 A7): a repository root with the guard's files,
hook events as Claude Code sends them, and the hook run exactly as Claude Code runs it (the settings command in a
shell, the event on stdin, CLAUDE_PROJECT_DIR in the environment)."""
import json
import os
import shutil
import subprocess
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / ".claude" / "hooks" / "guard.py"
SETTINGS = ROOT / ".claude" / "settings.json"
B = "python3 analyst/run_window.py "
LINES = [B + "start", B + "context", B + "lock .analyst_run/plan.json", B + "lock --decline", B + "lesson-start",
         B + "lesson-lock .analyst_run/lesson.json", B + "revision-start",
         B + "revision-lock .analyst_run/playbook.md", B + "probe"]
DRAFTS = ("plan.json", "lesson.json", "playbook.md")


def load(path=GUARD):
    """The guard module, executed from `path` (so its `__file__` is that path) without writing bytecode: nothing but
    the guard's two files may appear under .claude/ (R-AN-2)."""
    mod = types.ModuleType("session_guard")
    mod.__file__ = str(path)
    exec(compile(Path(path).read_bytes(), str(path), "exec"), mod.__dict__)
    return mod


def hook_command():
    """The command Claude Code runs, from the committed settings."""
    return json.loads(SETTINGS.read_text("utf-8"))["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


def make_root(tmp):
    """(R, D0): a repository root R (a real path) holding copies of the guard's files and a few repository files, and
    D0, a symbolic link to R (CLAUDE_PROJECT_DIR need not be the real path)."""
    tmp = Path(tmp).resolve()
    r = tmp / "repo"
    files = {"analyst/ROUTINE_PROMPT.md": "x\n", "analyst/prompts/system.md": "x\n", "analyst/run_window.py": "x\n",
             "analyst/ga/session.py": "x\n", ".analyst_run/context.json": "{}\n", ".git/config": "[core]\n",
             ".gitignore": "x\n", ".github/workflows/analyst-merge.yml": "x\n"}
    for p, t in files.items():
        (r / p).parent.mkdir(parents=True, exist_ok=True)
        (r / p).write_text(t)
    (r / ".claude" / "hooks").mkdir(parents=True)
    shutil.copyfile(SETTINGS, r / ".claude" / "settings.json")
    shutil.copyfile(GUARD, r / ".claude" / "hooks" / "guard.py")
    (r / "analyst" / "outside").symlink_to("/etc")                 # a link inside the repository to outside it
    d0 = tmp / "link"
    d0.symlink_to(r)
    return str(r), str(d0)


def event(tool, tool_input, cwd=None, **kw):
    e = {"session_id": "test-session", "transcript_path": "/tmp/test.jsonl", "cwd": cwd,
         "permission_mode": "bypassPermissions", "hook_event_name": "PreToolUse", "tool_name": tool,
         "tool_input": tool_input, "tool_use_id": "toolu_test"}
    e.update(kw)
    return e


def data(e):
    return e if isinstance(e, bytes) else json.dumps(e, ensure_ascii=False).encode("utf-8")


def run_hook(env_root, payload, script=None, cwd=None, extra_env=None):
    """Run the hook as a subprocess: the settings command through /bin/sh (default), or `python3 -I <script>`.
    `env_root` is CLAUDE_PROJECT_DIR (None: unset). Returns (exit code, stdout, stderr) as text."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/")}
    if env_root is not None:
        env["CLAUDE_PROJECT_DIR"] = env_root
    env.update(extra_env or {})
    argv = ["/bin/sh", "-c", hook_command()] if script is None else ["python3", "-I", str(script)]
    p = subprocess.run(argv, input=data(payload), env=env, capture_output=True, timeout=120,
                       cwd=cwd or (env_root if env_root and os.path.isdir(env_root) else None))
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
