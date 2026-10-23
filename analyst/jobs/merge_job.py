"""analyst-merge entry point (workflow .github/workflows/analyst-merge.yml). Runs main's code; the pushed commit is
data only. Prints only kinds, ids, seqs and enums."""
import datetime as dt
import json
import os
import sys
import time

from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parent))
import _boot  # noqa: E402,F401

import ga  # noqa: F401
from ga import ghapi, merge, rec


def main():
    ev = {}
    p = os.environ.get("GITHUB_EVENT_PATH")
    if p and os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            ev = json.load(f)
    event = {"ref": ev.get("ref"), "before": ev.get("before"), "after": ev.get("after")}
    if os.environ.get("GITHUB_EVENT_NAME") != "push":
        event = {}
    run_info = {"workflow": os.environ.get("GITHUB_WORKFLOW"), "id": int(os.environ.get("GITHUB_RUN_ID", "0")),
                "attempt": int(os.environ.get("GITHUB_RUN_ATTEMPT", "1")),
                "event": os.environ.get("GITHUB_EVENT_NAME")}
    ws = merge.Workspace(_boot.ROOT)
    api = ghapi.GitHubAPI(os.environ["GITHUB_REPOSITORY"])
    m = merge.run_merge(ws, api, lambda: dt.datetime.now(rec.UTC).replace(microsecond=0), run_info, event,
                        sleep=time.sleep)
    for s in m.subjects:
        print(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
