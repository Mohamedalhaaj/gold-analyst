"""The WATCHER_CONFIG.json build test (register §16.18 A14; R-AN-91 Export, R-AN-5 `a14_from`).

`analyst/WATCHER_CONFIG.json` (`analyst-watcher-config/1`) is the owner's snapshot of the deployed watcher, written at
the deploy and after every change of the Worker. This test checks its keys, `crons` == ["1,31 * * * *"], the three
secret names with `set` true, and that `script_sha256` is the sha256 of watcher/worker.js at `source_commit` (the
gold-analyst main commit deployed). It is the only machine check that the deployed bytes are the frozen worker.js:
the judge's A14 `a14_from` check reads only source_commit and deployed_utc. MODE.json `a14_from` may be set only once
the file exists (R-AN-5).
"""
import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG = "analyst/WATCHER_CONFIG.json"
MODE = "analyst/MODE.json"
SCHEMA = "analyst-watcher-config/1"
KEYS = {"schema", "exported_utc", "worker_name", "source_commit", "script_sha256", "crons", "secrets",
        "kv_namespace", "deployed_utc", "note"}
CRONS = ["1,31 * * * *"]
SECRET_NAMES = ("FIRE_TOKEN", "FIRE_URL", "TD_API_KEY")


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True)


def worker_at(root, commit):
    """watcher/worker.js at `commit` (bytes), or None when the commit or the file is not there."""
    r = _git(root, "show", f"{commit}:watcher/worker.js")
    return r.stdout if r.returncode == 0 else None


def config_problems(doc, root=ROOT):
    """[] when `doc` is a valid WATCHER_CONFIG.json for the repository at `root`; else what is wrong."""
    if not isinstance(doc, dict):
        return ["not a JSON object"]
    out = []
    if set(doc) != KEYS:
        out.append(f"keys: missing {sorted(KEYS - set(doc))}, extra {sorted(set(doc) - KEYS)}")
    if doc.get("schema") != SCHEMA:
        out.append("schema")
    if doc.get("crons") != CRONS:
        out.append("crons")
    sec = doc.get("secrets")
    if not (isinstance(sec, list) and all(isinstance(e, dict) and set(e) == {"name", "set"} for e in sec)
            and sorted(str(e["name"]) for e in sec) == list(SECRET_NAMES) and all(e["set"] is True for e in sec)):
        out.append("secrets")
    c = doc.get("source_commit")
    if not (isinstance(c, str) and re.fullmatch(r"[0-9a-f]{40}", c)):
        out.append("source_commit: not a full commit sha")
        return out
    b = worker_at(root, c)
    if b is None:
        out.append("source_commit: no watcher/worker.js at it")
    elif doc.get("script_sha256") != hashlib.sha256(b).hexdigest():
        out.append("script_sha256")
    if _git(root, "merge-base", "--is-ancestor", c, "HEAD").returncode != 0:
        out.append("source_commit: not an ancestor of HEAD")
    return out


def a14_problems(root=ROOT):
    """R-AN-5: MODE.json `a14_from` set (not null) needs analyst/WATCHER_CONFIG.json; the file, when present, must
    be valid."""
    root = Path(root)
    mode = json.loads((root / MODE).read_text("utf-8"))
    cfg = root / CONFIG
    if not cfg.is_file():
        return ["a14_from is set but analyst/WATCHER_CONFIG.json is absent"] if mode.get("a14_from") else []
    return config_problems(json.loads(cfg.read_text("utf-8")), root)


def _head(root=ROOT):
    return _git(root, "rev-parse", "HEAD").stdout.decode().strip()


def good_config(root=ROOT):
    c = _head(root)
    return {"schema": SCHEMA, "exported_utc": "2026-10-28T10:00:00Z", "worker_name": "gold-analyst-watcher",
            "source_commit": c, "script_sha256": hashlib.sha256(worker_at(root, c)).hexdigest(),
            "crons": ["1,31 * * * *"], "secrets": [{"name": n, "set": True} for n in ("TD_API_KEY", "FIRE_URL",
                                                                                        "FIRE_TOKEN")],
            "kv_namespace": "kv-namespace-id", "deployed_utc": "2026-10-28T09:55:00Z",
            "note": "test"}


# ---------------------------------------------------------------- the repository's own files
def test_watcher_config_when_present():
    if not (ROOT / CONFIG).is_file():
        pytest.skip("no analyst/WATCHER_CONFIG.json yet (written by the owner at the deploy)")
    assert config_problems(json.loads((ROOT / CONFIG).read_text("utf-8"))) == []


def test_a14_from_needs_the_watcher_config():
    assert a14_problems() == []


# ---------------------------------------------------------------- the check itself
def test_a_good_config_passes():
    assert config_problems(good_config()) == []


@pytest.mark.parametrize("change, want", [
    (lambda d: d.pop("note"), "keys"),
    (lambda d: d.update(extra=1), "keys"),
    (lambda d: d.update(schema="analyst-watcher-config/2"), "schema"),
    (lambda d: d.update(crons=["*/30 * * * *"]), "crons"),
    (lambda d: d.update(crons=["1,31 * * * *", "0 0 * * *"]), "crons"),
    (lambda d: d["secrets"].pop(), "secrets"),
    (lambda d: d["secrets"].append({"name": "OTHER", "set": True}), "secrets"),
    (lambda d: d["secrets"].__setitem__(1, {"name": "FIRE_URL", "set": False}), "secrets"),
    (lambda d: d["secrets"].__setitem__(1, {"name": "FIRE_URL", "set": True, "value": "x"}), "secrets"),
    (lambda d: d["secrets"].__setitem__(2, {"name": "TD_API_KEY", "set": True}), "secrets"),
    (lambda d: d.update(script_sha256="0" * 64), "script_sha256"),
    (lambda d: d.update(source_commit="main"), "source_commit"),
    (lambda d: d.update(source_commit=d["source_commit"][:12]), "source_commit"),
    (lambda d: d.update(source_commit="0" * 40), "source_commit"),
])
def test_a_bad_config_fails(change, want):
    d = good_config()
    change(d)
    probs = config_problems(d)
    assert probs and any(p.startswith(want) for p in probs), probs


def test_source_commit_without_the_watcher_fails():
    root_commit = _git(ROOT, "rev-list", "--max-parents=0", "HEAD").stdout.decode().split()[0]
    assert worker_at(ROOT, root_commit) is None
    d = good_config()
    d["source_commit"] = root_commit
    assert "source_commit: no watcher/worker.js at it" in config_problems(d)


def _mode_root(tmp_path, a14_from, config=None):
    (tmp_path / "analyst").mkdir()
    (tmp_path / MODE).write_text(json.dumps({"mode": "canary", "a14_from": a14_from}))
    if config is not None:
        (tmp_path / CONFIG).write_text(json.dumps(config))
    return tmp_path


def test_a14_from_without_the_config_fails(tmp_path):
    assert a14_problems(_mode_root(tmp_path, "2026-10-29")) == \
        ["a14_from is set but analyst/WATCHER_CONFIG.json is absent"]


def test_a14_from_null_or_absent_without_the_config_passes(tmp_path):
    assert a14_problems(_mode_root(tmp_path, None)) == []
    (tmp_path / MODE).write_text(json.dumps({"mode": "canary"}))
    assert a14_problems(tmp_path) == []


def test_a_present_config_is_checked_even_without_a14_from(tmp_path):
    bad = dict(good_config(), crons=[])
    assert a14_problems(_mode_root(tmp_path, None, bad)) != []
