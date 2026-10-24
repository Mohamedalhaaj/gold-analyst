"""The routine configuration (register R-AN-61, §16.18 A6 after its second review, SEC-2).

claude.ai attaches every account connector to a routine when it is created, so a routine could reach Gmail, Drive or
any other connected service, outside the network allowlist. Every routine (the probe routine included) must have
`mcp_connections` = [] and `enabled_plugins` = [] in its RemoteTrigger get response, which the export keeps in `raw`.
From the first export at or after PINS.inbox_from_utc, and whenever MODE.json sets canary_from or live_from, every
routine names the inbox and nothing else (`repo` and the trigger's `sources`).

Third review of §16.18 A7 (decision 21): no routine is created from, or runs as threads of, a claude.ai project (a
project's cloud threads run no repository hook). Each routine of the export has `project`, the project it belongs to,
null for none; `raw` keeps every field of the RemoteTrigger get whose name contains `project` (case ignored). Under
the `repo` rule's condition every routine must have the key `project`. Interpretation (logged for the register): a
`project` other than null, or such a `raw` field with a value other than null, "", [] or {}, is refused in every
committed export, whatever the condition, as the judge's A13 `routine_project` reads the version in force; only the
missing key waits for the condition, so the export in force (d1725d7, no `project` key) passes until then.
"""
import copy
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(p):
    return json.loads((ROOT / p).read_text("utf-8"))


def project_fields(obj, where="raw"):
    """Every field of `obj`, at any depth, whose name contains `project` (case ignored), as (where, value)."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            w = f"{where}.{k}"
            if "project" in str(k).lower():
                out.append((w, v))
            out.extend(project_fields(v, w))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(project_fields(v, f"{where}[{i}]"))
    return out


def no_value(v):
    """null, "", [] or {}: a `raw` project field that names no project (R-AN-61, decision 21)."""
    return v is None or (type(v) in (str, list, dict) and len(v) == 0)


def problems(export, pins, mode):
    """Why an `analyst/ROUTINE_CONFIG.json` export breaks R-AN-61; [] when it holds."""
    out = []
    inbox = pins.get("inbox_repo")
    since = pins.get("inbox_from_utc")
    in_use = bool(mode.get("canary_from") or mode.get("live_from")) or \
        bool(since and (export.get("exported_utc") or "") >= since)
    if not export.get("routines"):
        out.append("no routines")
    for r in export.get("routines") or []:
        raw = r.get("raw") if isinstance(r.get("raw"), dict) else {}
        for k in ("mcp_connections", "enabled_plugins"):
            if raw.get(k) != []:
                out.append(f"{r.get('name')}: {k}")
        if in_use:
            ctx = ((raw.get("job_config") or {}).get("ccr") or {}).get("session_context") or {}
            if r.get("repo") != inbox or ctx.get("sources") != [{"git_repository":
                                                                  {"url": f"https://github.com/{inbox}"}}]:
                out.append(f"{r.get('name')}: repo")
        # no routine in a claude.ai project (third review of §16.18 A7, decision 21)
        if ("project" in r and r["project"] is not None) or (in_use and "project" not in r):
            out.append(f"{r.get('name')}: project")
        out.extend(f"{r.get('name')}: {w}" for w, v in project_fields(raw) if not no_value(v))
    return out


def test_the_committed_export_has_no_connector_or_plugin():
    assert problems(load("analyst/ROUTINE_CONFIG.json"), load("analyst/PINS.json"), load("analyst/MODE.json")) == []


def test_the_template_names_empty_connector_and_plugin_lists():
    t = load("analyst/ROUTINE_CONFIG.template.json")
    for r in t["routines"] + [t["probe_session"]]:
        assert (r["mcp_connections"], r["enabled_plugins"]) == ([], []), r.get("name")


def test_the_template_names_no_project_for_any_routine():
    """Decision 21: the template names `"project": null` for each routine, the probe routine included, and says
    where the owner reads it and that no routine is made in a project."""
    t = load("analyst/ROUTINE_CONFIG.template.json")
    for r in t["routines"] + [t["probe_session"]]:
        assert "project" in r and r["project"] is None, r.get("name")
    assert "claude.ai project" in t["note"] and "Scheduled tasks" in t["note"]


def _good():
    pins = {"inbox_repo": "Mohamedalhaaj/gold-analyst-inbox", "inbox_from_utc": "2026-10-24T10:00:00Z"}
    raw = {"mcp_connections": [], "enabled_plugins": [], "job_config": {"ccr": {"session_context": {
        "sources": [{"git_repository": {"url": "https://github.com/Mohamedalhaaj/gold-analyst-inbox"}}]}}}}
    export = {"exported_utc": "2026-10-24T11:00:00Z",
              "routines": [{"name": "analyst-w1", "repo": "Mohamedalhaaj/gold-analyst-inbox", "project": None,
                            "raw": raw}]}
    return export, pins, {"canary_from": None, "live_from": None}


def test_a_conforming_export_passes():
    assert problems(*_good()) == []


@pytest.mark.parametrize("plant", ["connector", "plugin", "no_field", "repo", "sources", "old_repo_in_canary",
                                   "project", "project_empty_string", "project_false", "no_project_key",
                                   "raw_project", "raw_project_nested", "raw_project_case", "raw_projects_list",
                                   "no_project_key_in_canary"])
def test_a_planted_export_is_caught(plant):
    export, pins, mode = _good()
    export = copy.deepcopy(export)
    r = export["routines"][0]
    if plant == "connector":
        r["raw"]["mcp_connections"] = [{"name": "Gmail", "url": "https://gmail.mcp.claude.com/mcp"}]
    elif plant == "plugin":
        r["raw"]["enabled_plugins"] = ["some-plugin"]
    elif plant == "no_field":
        del r["raw"]["mcp_connections"]
    elif plant == "repo":
        r["repo"] = "Mohamedalhaaj/gold-analyst"
    elif plant == "sources":
        r["raw"]["job_config"]["ccr"]["session_context"]["sources"].append(
            {"git_repository": {"url": "https://github.com/Mohamedalhaaj/gold-forward-watch"}})
    elif plant == "old_repo_in_canary":         # an export from before the inbox, while the canary is set
        export["exported_utc"] = "2026-10-23T08:52:00Z"
        r["repo"] = "Mohamedalhaaj/gold-analyst"
        mode = {"canary_from": "2026-10-26", "live_from": None}
    # third review of §16.18 A7, decision 21: a routine in a claude.ai project
    elif plant == "project":
        r["project"] = "Gold analyst"
    elif plant == "project_empty_string":       # only null says "no project"
        r["project"] = ""
    elif plant == "project_false":
        r["project"] = False
    elif plant == "no_project_key":             # an export at or after PINS.inbox_from_utc
        del r["project"]
    elif plant == "raw_project":
        r["raw"]["project_id"] = "proj_01AbC"
    elif plant == "raw_project_nested":
        r["raw"]["job_config"]["ccr"]["session_context"]["claude_project"] = {"uuid": "x", "name": "Gold analyst"}
    elif plant == "raw_project_case":
        r["raw"]["job_config"]["ProjectUUID"] = "4d6f"
    elif plant == "raw_projects_list":
        r["raw"]["job_config"]["ccr"]["session_context"]["sources"].append({"projects": ["Gold analyst"]})
    else:                                       # an export from before PINS.inbox_from_utc, while the canary is set
        export["exported_utc"] = "2026-10-23T08:52:00Z"
        del r["project"]
        mode = {"canary_from": "2026-10-26", "live_from": None}
    assert problems(export, pins, mode) != []


@pytest.mark.parametrize("value", [None, "", [], {}])
def test_a_raw_project_field_without_a_value_passes(value):
    export, pins, mode = _good()
    export = copy.deepcopy(export)
    r = export["routines"][0]
    r["raw"]["project_id"] = value
    r["raw"]["job_config"]["ccr"]["session_context"]["Projects"] = value
    assert problems(export, pins, mode) == []


def test_the_project_key_is_needed_only_from_the_repo_rules_condition():
    """The export in force (d1725d7, routines created through the API) has no `project` key: it passes before the
    condition holds (an export before PINS.inbox_from_utc, no canary_from or live_from), and not after."""
    export, pins, mode = _good()
    export = copy.deepcopy(export)
    del export["routines"][0]["project"]
    export["exported_utc"] = "2026-10-23T08:52:00Z"
    assert problems(export, pins, mode) == []
    assert problems(export, {**pins, "inbox_from_utc": None}, mode) == []
    assert problems(export, pins, {"canary_from": None, "live_from": "2026-11-09"}) == ["analyst-w1: project"]
    export["routines"][0]["project"] = "Gold analyst"                   # a project is refused whatever the time
    assert problems(export, pins, mode) == ["analyst-w1: project"]
    export["routines"][0]["project"] = None
    export["routines"][0]["raw"]["projectId"] = "p1"
    assert problems(export, pins, mode) == ["analyst-w1: raw.projectId"]


def test_the_template_prompts():
    """The four routines' prompts point to sections of ROUTINE_PROMPT.md and do not change with §16.18 A7; the probe
    routine follows the frozen PROBE_SESSION_PROMPT.md (R-AN-61, R-AN-74 C20) and has the routines' tools."""
    t = load("analyst/ROUTINE_CONFIG.template.json")
    assert {r["name"]: r["prompt"] for r in t["routines"]} == {
        "analyst-w1": "Follow the section CALL of analyst/ROUTINE_PROMPT.md exactly.",
        "analyst-w2": "Follow the section CALL of analyst/ROUTINE_PROMPT.md exactly.",
        "analyst-lesson": "Follow the section LESSON of analyst/ROUTINE_PROMPT.md exactly.",
        "analyst-revision": "Follow the section REVISION of analyst/ROUTINE_PROMPT.md exactly."}
    p = t["probe_session"]
    assert (p["name"], p["prompt"]) == ("analyst-probe", "Follow analyst/PROBE_SESSION_PROMPT.md exactly.")
    assert p["allowed_tools"] == ["Bash", "Read", "Write"] and "C20" in p["note"]
