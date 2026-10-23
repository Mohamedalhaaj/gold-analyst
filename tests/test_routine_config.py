"""The routine configuration (register R-AN-61, §16.18 A6 after its second review, SEC-2).

claude.ai attaches every account connector to a routine when it is created, so a routine could reach Gmail, Drive or
any other connected service, outside the network allowlist. Every routine (the probe routine included) must have
`mcp_connections` = [] and `enabled_plugins` = [] in its RemoteTrigger get response, which the export keeps in `raw`.
From the first export at or after PINS.inbox_from_utc, and whenever MODE.json sets canary_from or live_from, every
routine names the inbox and nothing else (`repo` and the trigger's `sources`).
"""
import copy
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(p):
    return json.loads((ROOT / p).read_text("utf-8"))


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
    return out


def test_the_committed_export_has_no_connector_or_plugin():
    assert problems(load("analyst/ROUTINE_CONFIG.json"), load("analyst/PINS.json"), load("analyst/MODE.json")) == []


def test_the_template_names_empty_connector_and_plugin_lists():
    t = load("analyst/ROUTINE_CONFIG.template.json")
    for r in t["routines"] + [t["probe_session"]]:
        assert (r["mcp_connections"], r["enabled_plugins"]) == ([], []), r.get("name")


def _good():
    pins = {"inbox_repo": "Mohamedalhaaj/gold-analyst-inbox", "inbox_from_utc": "2026-10-24T10:00:00Z"}
    raw = {"mcp_connections": [], "enabled_plugins": [], "job_config": {"ccr": {"session_context": {
        "sources": [{"git_repository": {"url": "https://github.com/Mohamedalhaaj/gold-analyst-inbox"}}]}}}}
    export = {"exported_utc": "2026-10-24T11:00:00Z",
              "routines": [{"name": "analyst-w1", "repo": "Mohamedalhaaj/gold-analyst-inbox", "raw": raw}]}
    return export, pins, {"canary_from": None, "live_from": None}


def test_a_conforming_export_passes():
    assert problems(*_good()) == []


@pytest.mark.parametrize("plant", ["connector", "plugin", "no_field", "repo", "sources", "old_repo_in_canary"])
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
    else:                                       # an export from before the inbox, while the canary is set
        export["exported_utc"] = "2026-10-23T08:52:00Z"
        r["repo"] = "Mohamedalhaaj/gold-analyst"
        mode = {"canary_from": "2026-10-26", "live_from": None}
    assert problems(export, pins, mode) != []
