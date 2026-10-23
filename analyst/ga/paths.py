"""Record paths, modes and the owner/frozen config files (register R-AN-2, R-AN-5, R-AN-6, R-AN-7). FROZEN.

In canary mode every session- and script-created record goes under `analyst/canary/` with the same sub-path; in live
mode under `analyst/`.
"""
import datetime as dt

MODE_PATH = "analyst/MODE.json"
PARAMS_PATH = "analyst/PARAMS.json"
PINS_PATH = "analyst/PINS.json"
STATE_PATH = "analyst/STATE.json"
START_PATH = "analyst/START.json"
MANIFEST_PATH = "analyst/FROZEN_ANALYST.sha256"
ROUTINE_PROMPT_PATH = "analyst/ROUTINE_PROMPT.md"
EXAM_PROMPT_PATH = "analyst/exam/ROUTINE_PROMPT.md"
SCHEMA_PATH = "analyst/schema/plan.schema.json"
PROMPTS = {"system": "analyst/prompts/system.md", "lesson": "analyst/prompts/lesson.md",
           "revision": "analyst/prompts/revision.md"}
CANARY_SETTLED_FIXTURE = "analyst/tests/fixtures/canary_settled.json"
LEDGER_PATH = "analyst/ledger.csv"
MODES = ("canary", "live", "off")


def prefix(mode):
    if mode == "live":
        return "analyst/"
    if mode == "canary":
        return "analyst/canary/"
    raise ValueError(f"no record prefix for mode {mode!r}")


def attempt_path(mode, draw_id, n):
    return f"{prefix(mode)}attempts/{draw_id}_{n}.json"


def call_path(mode, call_id):
    return f"{prefix(mode)}calls/{call_id}.json"


def lesson_path(mode, call_id):
    return f"{prefix(mode)}lessons/{call_id}.json"


def playbook_path(mode, k):
    if k == 0:
        return "analyst/playbooks/v0.md"
    return f"{prefix(mode)}playbooks/v{k}.md"


def terminal_path(mode, call_id):
    return f"{prefix(mode)}terminal/{call_id}.json"


def witness_path(mode, seq):
    return f"{prefix(mode)}witness/{seq:06d}.json"


def revision_record_path(mode, iso_week):
    return f"{prefix(mode)}revisions/{iso_week}.json"


def settled_path(call_id):
    return f"analyst/settled/{call_id}.json"


def twin_path(call_id):
    return f"analyst/twins/{call_id}.json"


def state_event_path(seq):
    return f"analyst/state/{seq:06d}.json"


def read_mode(view):
    m = view.read_json(MODE_PATH) or {"mode": "off"}
    if m.get("mode") not in MODES:
        raise ValueError(f"MODE.json mode {m.get('mode')!r}")
    return m


def mode_period(mode_doc, mode=None):
    """(first_day, last_day or None) of the expected windows of `mode` (default: the current mode; for `off`, the
    live period when live_from is set, else the canary period)."""
    mode = mode or mode_doc["mode"]
    if mode == "off":
        mode = "live" if mode_doc.get("live_from") else "canary"
    if mode == "canary":
        a, b = mode_doc.get("canary_from"), mode_doc.get("canary_to")
    else:
        a, b = mode_doc.get("live_from"), None
    if not a:
        return mode, None, None
    return mode, dt.date.fromisoformat(a), (dt.date.fromisoformat(b) if b else None)


def read_params(view):
    return view.read_json(PARAMS_PATH)


def read_pins(view):
    return view.read_json(PINS_PATH)
