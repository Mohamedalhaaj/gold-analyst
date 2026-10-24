"""The session guard (register R-AN-85, §16.18 A7). FROZEN.

A PreToolUse hook (.claude/settings.json, matcher "*", onFailure "block") that refuses every tool call of a routine
session that is not one of the routine's own steps: Bash only for the nine exact command lines of
analyst/ROUTINE_PROMPT.md (CALL, LESSON, REVISION) and `probe`; Read only inside the repository, outside .git; Write
only to .analyst_run/plan.json, lesson.json or playbook.md under the root. Everything else is refused, every unknown
tool included. Standard library only (json, os, stat, sys); no network, no subprocess, no file write.

Second review of A7 (decision 20): a Bash `timeout` below the 3600000 ms that `.claude/settings.json` sets as
BASH_DEFAULT_TIMEOUT_MS and BASH_MAX_TIMEOUT_MS is refused (`bash_option`), as a background run is: either would
let an allowed command move to the background, where the session cannot read its output and could run a second
command beside it.

Protocol: the hook's JSON event on stdin, CLAUDE_PROJECT_DIR in the environment. Allowed: exit 0, nothing printed (the
guard never answers "allow", so the permission rules and auto mode's classifier stay a second check). Refused: the
compact JSON permissionDecision "deny" on stdout, `guard: <code>: <message>` on stderr, exit 2. Fail closed: any
exception, unreadable input or missing CLAUDE_PROJECT_DIR is a refusal; a crash or timeout is blocked by onFailure.

`decide(event, root, fs)` is the decision; the run-log scan (analyst/jobs/scan_runs.py, R-AN-86) calls it with
fs=False, which applies the string rules only (checks 4, 6(d), 7(d) and 8(c) read the session's file system).
Interpretation (logged for the register): the event's `cwd` must also be an absolute path (6(d)); a relative one
has no realpath of its own.
"""
import json
import os
import stat
import sys

MAX_STDIN = 8388608                 # 8 MiB: a longer stdin is refused `input` (check 2)
MAX_WRITE = 4194304                 # 4 MiB of UTF-8: a longer Write content is refused `write_size` (check 8(d))
LINES = frozenset((
    "python3 analyst/run_window.py start",
    "python3 analyst/run_window.py context",
    "python3 analyst/run_window.py lock .analyst_run/plan.json",
    "python3 analyst/run_window.py lock --decline",
    "python3 analyst/run_window.py lesson-start",
    "python3 analyst/run_window.py lesson-lock .analyst_run/lesson.json",
    "python3 analyst/run_window.py revision-start",
    "python3 analyst/run_window.py revision-lock .analyst_run/playbook.md",
    "python3 analyst/run_window.py probe",
))
TOOLS = ("Bash", "Read", "Write")
BASH_KEYS = frozenset(("command", "description", "timeout", "run_in_background", "dangerouslyDisableSandbox"))
BASH_OFF = ("run_in_background", "dangerouslyDisableSandbox")
BASH_TIMEOUT_MIN_MS = 3600000       # the settings' BASH_DEFAULT_TIMEOUT_MS and BASH_MAX_TIMEOUT_MS (check 6(b))
DRAFTS = ("plan.json", "lesson.json", "playbook.md")
GUARD_REL = "/.claude/hooks/guard.py"
MESSAGES = {
    "input": "the tool call could not be checked, so it is refused.",
    "error": "the guard could not complete its checks, so the call is refused.",
    "tool": "only Bash, Read and Write are available in this session, as analyst/ROUTINE_PROMPT.md says. "
            "Do not try another way.",
    "bash_form": "run only the commands written in analyst/ROUTINE_PROMPT.md, each exactly as written and on its "
                 "own: no cd, nothing before or after it. Do not try another way.",
    "bash_option": "run the command in the foreground, with no option other than a description, and leave out the "
                   "timeout (one below 3600000 ms is refused).",
    "bash_cwd": "commands run only from the repository root.",
    "read_path": "read only files inside this repository, outside .git, by their absolute paths.",
    "write_path": "write only the one file your step names, .analyst_run/plan.json, .analyst_run/lesson.json or "
                  ".analyst_run/playbook.md under the repository root, by its absolute path; to correct it, write "
                  "the whole file again.",
    "write_size": "the file is larger than 4 MiB.",
}
HEX = "0123456789abcdef"
DIGITS = "0123456789"


class Refuse(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def message(code):
    """M, the refusal message: `guard: <code>: <message>` (ASCII)."""
    return "guard: " + code + ": " + MESSAGES[code]


def _no_repeat(pairs):
    keys = [k for k, _v in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("repeated key")
    return dict(pairs)


def _no_constant(name):
    raise ValueError("not JSON: " + name)


def parse(data):
    """The hook event from stdin's bytes (check 2): at most 8 MiB, strict UTF-8, one JSON object, no repeated key."""
    if not isinstance(data, bytes) or len(data) > MAX_STDIN:
        raise Refuse("input")
    try:
        text = data.decode("utf-8", "strict")
        obj = json.loads(text, object_pairs_hook=_no_repeat, parse_constant=_no_constant)
    except ValueError:
        raise Refuse("input")
    if not isinstance(obj, dict):
        raise Refuse("input")
    return obj


def _inside(path, d):
    """`path` lies inside `d`: it equals d or starts with d + "/"."""
    return path == d or path.startswith(d + "/")


def _lstat(path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _content_size(content, logged):
    """The UTF-8 size of a Write content. In a logged call (fs=False, R-AN-86) the export replaces it by
    `sha256:<64 hex> bytes:<n>`, which counts as n bytes."""
    if logged and len(content) > 78 and content.startswith("sha256:") and content[71:78] == " bytes:":
        h, n = content[7:71], content[78:]
        if all(c in HEX for c in h) and all(c in DIGITS for c in n):
            return int(n)
    return len(content.encode("utf-8", "strict"))


def _decide(event, root, fs):
    # 3. the event
    if not isinstance(event, dict) or event.get("hook_event_name") != "PreToolUse" \
            or not isinstance(event.get("tool_name"), str) or not isinstance(event.get("tool_input"), dict):
        return "input"
    # 4. the root and the guard's own place (a file-system rule)
    d0 = root.rstrip("/") if isinstance(root, str) else None
    if fs:
        if not d0 or not d0.startswith("/") or not os.path.isdir(d0):
            return "error"
        r = os.path.realpath(d0)
        if os.path.realpath(__file__) != r + GUARD_REL:
            return "error"
        roots = (d0, r)
    else:
        r = None
        roots = (d0,) if d0 else ()
    # 5. the tool
    tool, ti = event["tool_name"], event["tool_input"]
    if tool not in TOOLS:
        return "tool"
    # 6. Bash
    if tool == "Bash":
        cmd = ti.get("command")
        if not isinstance(cmd, str):
            return "input"
        if any(k not in BASH_KEYS for k in ti):
            return "bash_option"
        for k in BASH_OFF:
            v = ti.get(k)
            if v is not None and v is not False:
                return "bash_option"
        to = ti.get("timeout")                     # absent, null, or a number of at least BASH_TIMEOUT_MIN_MS
        if to is not None and (isinstance(to, bool) or not isinstance(to, (int, float))
                               or not to >= BASH_TIMEOUT_MIN_MS):
            return "bash_option"
        if cmd.strip(" ") not in LINES:
            return "bash_form"
        if fs:
            cwd = event.get("cwd")
            if not isinstance(cwd, str) or not cwd.startswith("/") or os.path.realpath(cwd) != r:
                return "bash_cwd"
        return None
    # 7. Read
    if tool == "Read":
        p = ti.get("file_path")
        if not isinstance(p, str):
            return "input"
        if not p.startswith("/") or any(ord(c) < 0x20 or ord(c) == 0x7F for c in p) or ".." in p.split("/"):
            return "read_path"
        n = os.path.normpath(p)
        if not any(_inside(n, d) and not _inside(n, d + "/.git") for d in roots):
            return "read_path"
        if fs:
            rp = os.path.realpath(p)
            if not _inside(rp, r) or _inside(rp, r + "/.git"):
                return "read_path"
        return None
    # 8. Write
    p, content = ti.get("file_path"), ti.get("content")
    if not isinstance(p, str) or not isinstance(content, str):
        return "input"
    if not any(p == d + "/.analyst_run/" + f for d in roots for f in DRAFTS):
        return "write_path"
    if fs:
        run = r + "/.analyst_run"
        st = _lstat(run)
        if st is not None and not stat.S_ISDIR(st.st_mode):
            return "write_path"
        if os.path.realpath(os.path.dirname(p)) != run:
            return "write_path"
        st = _lstat(p)
        if st is not None and not stat.S_ISREG(st.st_mode):
            return "write_path"
    if _content_size(content, not fs) > MAX_WRITE:
        return "write_size"
    return None


def decide(event, root, fs=True):
    """None when the call is allowed, else the refusal code (R-AN-85 checks 3 to 8; any exception: `error`).
    `event` is the parsed hook event, `root` the value of CLAUDE_PROJECT_DIR (or, for the scan, the session's
    repository root as its log shows it). With `fs` false only the string rules apply (R-AN-86)."""
    try:
        return _decide(event, root, fs)
    except Exception:  # noqa: BLE001 - fail closed (check 1)
        return "error"


def check(data, root):
    """The guard's whole decision on stdin's bytes and CLAUDE_PROJECT_DIR: None (allow) or a refusal code."""
    try:
        return decide(parse(data), root, fs=True)
    except Refuse as r:
        return r.code
    except Exception:  # noqa: BLE001
        return "error"


def main():
    code = "error"
    try:
        code = check(sys.stdin.buffer.read(MAX_STDIN + 1), os.environ.get("CLAUDE_PROJECT_DIR"))
    except BaseException:  # noqa: BLE001 - fail closed (check 1)
        code = "error"
    if code is None:
        return 0
    m = message(code)
    out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                  "permissionDecisionReason": m}}
    try:
        sys.stdout.write(json.dumps(out, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False) + "\n")
        sys.stdout.flush()
    except BaseException:  # noqa: BLE001 - a failure to write still refuses
        pass
    try:
        sys.stderr.write(m + "\n")
        sys.stderr.flush()
    except BaseException:  # noqa: BLE001
        pass
    return 2


if __name__ == "__main__":
    sys.exit(main())
