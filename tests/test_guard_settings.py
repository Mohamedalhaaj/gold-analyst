"""The guard's configuration (register R-AN-85 "Files", R-AN-2, R-AN-7; §16.18 A7 decisions 2, 6 and 8).

`.claude/settings.json` is exactly the register's block: one PreToolUse hook for every tool (matcher "*"), the shell
form `python3 -I "${CLAUDE_PROJECT_DIR}/.claude/hooks/guard.py"` with timeout 30 and onFailure "block", and the
deny rules WebFetch, WebSearch, Agent and Skill as a second layer. The guard's files and the probe session's prompt
are frozen (FROZEN_ANALYST.sha256), so a change is frozen drift for the judge (A11) as well as A13 `guard_config`.
"""
import json
import shlex

from ga import frozen
from guardkit import GUARD, ROOT, SETTINGS

EXPECTED = (
    '{\n'
    '  "hooks": {\n'
    '    "PreToolUse": [\n'
    '      {\n'
    '        "matcher": "*",\n'
    '        "hooks": [\n'
    '          {\n'
    '            "type": "command",\n'
    '            "command": "python3 -I \\"${CLAUDE_PROJECT_DIR}/.claude/hooks/guard.py\\"",\n'
    '            "timeout": 30,\n'
    '            "onFailure": "block"\n'
    '          }\n'
    '        ]\n'
    '      }\n'
    '    ]\n'
    '  },\n'
    '  "permissions": {\n'
    '    "deny": ["WebFetch", "WebSearch", "Agent", "Skill"]\n'
    '  }\n'
    '}\n'
)


def test_the_settings_file_is_the_registers_block_byte_for_byte():
    assert SETTINGS.read_bytes() == EXPECTED.encode("ascii")


def test_the_settings_object():
    s = json.loads(SETTINGS.read_text("utf-8"))
    assert set(s) == {"hooks", "permissions"}
    assert set(s["hooks"]) == {"PreToolUse"}
    [entry] = s["hooks"]["PreToolUse"]
    assert entry["matcher"] == "*" and set(entry) == {"matcher", "hooks"}
    [hook] = entry["hooks"]
    assert hook == {"type": "command", "command": 'python3 -I "${CLAUDE_PROJECT_DIR}/.claude/hooks/guard.py"',
                    "timeout": 30, "onFailure": "block"}
    assert s["permissions"] == {"deny": ["WebFetch", "WebSearch", "Agent", "Skill"]}


def test_the_command_names_the_guard_under_the_project_dir_with_isolated_python():
    argv = shlex.split(json.loads(SETTINGS.read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"])
    assert argv == ["python3", "-I", "${CLAUDE_PROJECT_DIR}/.claude/hooks/guard.py"]
    assert (ROOT / argv[2].replace("${CLAUDE_PROJECT_DIR}/", "")).resolve() == GUARD.resolve()


def test_the_guard_and_the_probe_prompt_are_frozen():
    lines = (ROOT / "analyst" / "FROZEN_ANALYST.sha256").read_text().splitlines()
    paths = {l.split("  ", 1)[1] for l in lines}
    assert {".claude/settings.json", ".claude/hooks/guard.py", "analyst/PROBE_SESSION_PROMPT.md"} <= paths
    assert {".claude/settings.json", ".claude/hooks/guard.py", "analyst/PROBE_SESSION_PROMPT.md"} <= \
        set(frozen.frozen_paths(ROOT))
    assert frozen.verify(ROOT) == []
