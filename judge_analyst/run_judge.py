"""Analyst judge entry point (prereg §8, IMPLEMENTATION_REGISTER R-AN-66..R-AN-70).

Usage (from the gold-analyst root, a FULL clone of main):
    python -I judge_analyst/run_judge.py [--now YYYY-MM-DDTHH:MM:SSZ] [--out reports/judge_analyst]
        [--api github|none|fake:<file.json>] [--hours dukascopy|none|fake:<dir>] [--store <gold-forward-watch path>]
        [--fetch-pushes]

The GitHub token is read from the GITHUB_TOKEN environment variable and only placed in request headers. Nothing from
an agent-pushed commit is executed: pushed commits are fetched as data only (--fetch-pushes) and read with git.
Only enums, counts and hashes are printed; model text never reaches stdout.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from jlib import report  # noqa: E402
from jlib.common import UTC, fmt_utc, load_json_bytes  # noqa: E402
from jlib.gitrepo import GitRepo  # noqa: E402
from jlib.judge import EVIDENCE_HEADER, Judge  # noqa: E402
from jlib.sources import FakeGitHub, FakeHourFetcher, GitHubAPI, HourSource, HttpHourFetcher  # noqa: E402

MANIFEST = "FROZEN_JUDGE_ANALYST.sha256"


def manifest_status(root: str):
    """(ok, code_sha8): every file the judge's manifest lists has its sha256, and every judge source file is listed."""
    mpath = os.path.join(HERE, MANIFEST)
    if not os.path.exists(mpath):
        return False, "00000000"
    with open(mpath, "rb") as f:
        mb = f.read()
    code_sha8 = hashlib.sha256(mb).hexdigest()[:8]
    listed = set()
    ok = True
    for line in mb.decode("utf-8").split("\n"):
        if not line.strip():
            continue
        sha, _, path = line.partition("  ")
        listed.add(path)
        p = os.path.join(root, path)
        if not os.path.exists(p):
            ok = False
            continue
        with open(p, "rb") as f:
            if hashlib.sha256(f.read()).hexdigest() != sha:
                ok = False
    if set(judge_files(root)) - listed:
        ok = False
    return ok, code_sha8


WORKFLOW = ".github/workflows/analyst-judge.yml"


def judge_files(root: str):
    """Repository-relative paths the judge's manifest covers: every file of judge_analyst/ (caches excluded) and the
    judge's workflow."""
    out = []
    for dirpath, dirs, files in os.walk(HERE):
        dirs[:] = sorted(d for d in dirs if d not in ("__pycache__", ".pytest_cache"))
        for name in sorted(files):
            if name == MANIFEST or name.endswith(".pyc"):
                continue
            out.append(os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/"))
    if os.path.exists(os.path.join(root, WORKFLOW)):
        out.append(WORKFLOW)
    return sorted(out)


def write_manifest(root: str):
    lines = []
    for rel in judge_files(root):
        with open(os.path.join(root, rel), "rb") as f:
            lines.append(f"{hashlib.sha256(f.read()).hexdigest()}  {rel}\n")
    with open(os.path.join(HERE, MANIFEST), "w", encoding="utf-8", newline="\n") as f:
        f.write("".join(lines))
    return len(lines)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--out", default="reports/judge_analyst")
    ap.add_argument("--now", default=None)
    ap.add_argument("--api", default="github")
    ap.add_argument("--hours", default="dukascopy")
    ap.add_argument("--store", default=None)
    ap.add_argument("--fetch-pushes", action="store_true")
    ap.add_argument("--write-manifest", action="store_true", help="rewrite FROZEN_JUDGE_ANALYST.sha256 and exit")
    a = ap.parse_args(argv)
    root = os.path.abspath(a.repo)
    if a.write_manifest:
        print(f"manifest lines={write_manifest(root)}")
        return 0
    out = os.path.join(root, a.out) if not os.path.isabs(a.out) else a.out
    now = a.now or fmt_utc(dt.datetime.now(UTC).replace(microsecond=0))
    git = GitRepo(root)
    pins = {}
    pb = git.show(git.head(), "analyst/PINS.json")
    if pb:
        try:
            pins = load_json_bytes(pb)
        except Exception:  # noqa: BLE001
            pins = {}
    if a.api == "github":
        repo = pins.get("repo") or os.environ.get("GITHUB_REPOSITORY", "")
        api = GitHubAPI(repo, os.environ.get("GITHUB_TOKEN"))
    elif a.api.startswith("fake:"):
        with open(a.api[5:], encoding="utf-8") as f:
            api = FakeGitHub(json.load(f))
    else:
        api = None
    if a.hours == "dukascopy":
        fetcher = HttpHourFetcher()
    elif a.hours.startswith("fake:"):
        files = {}
        idx = os.path.join(a.hours[5:], "index.json")
        with open(idx, encoding="utf-8") as f:
            for url, (http, name) in json.load(f).items():
                body = b""
                if name:
                    with open(os.path.join(a.hours[5:], name), "rb") as g:
                        body = g.read()
                files[url] = (http, body)
        fetcher = FakeHourFetcher(files)
    else:
        fetcher = FakeHourFetcher({}, down=True)
    hours = HourSource(fetcher, os.path.join(out, "hours.csv"), now)
    store_git = GitRepo(os.path.abspath(a.store)) if a.store else None
    fetch = None
    if a.fetch_pushes:
        def fetch(sha):
            p = subprocess.run(["git", "-C", root, "fetch", "--no-tags", "-q", "origin", sha], capture_output=True)
            return p.returncode == 0
    ok, code_sha8 = manifest_status(root)
    evidence = report.read_csv(os.path.join(out, "evidence.csv"), EVIDENCE_HEADER)
    judge = Judge(git, now, api=api, hours=hours, store_git=store_git, fetch_commit=fetch, evidence_rows=evidence,
                  prev_anchor=report.last_anchor(out), judge_manifest_ok=ok)
    res = judge.run()
    v = report.write_outputs(res, out, now, code_sha8, hours)
    git.close()
    if store_git is not None:
        store_git.close()
    print(f"analyst_verdict={v['analyst_verdict']} violation_keys={len(v['violation_keys'])} "
          f"findings={len(res.findings.items)} head={res.head[:12]} code_sha8={code_sha8}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
