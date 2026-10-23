"""analyst-merge entry point (workflow .github/workflows/analyst-merge.yml; register R-AN-32, §16.18 A6). Runs main's
code on a schedule or by hand; the pushed commits are fetched from the agent inbox as data only. Prints only kinds,
ids, seqs and enums (`activity_read=token|anonymous|failed`, `inbox_fetch=unreachable`, `stale_window <call_id>`)."""
import datetime as dt
import os
import sys
import time

from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parent))
import _boot  # noqa: E402,F401

import ga  # noqa: F401
from ga import ghapi, merge, paths, rec


def guard(env):
    """The run guard (R-AN-32): main's workflow file and code only, started by the schedule or by hand."""
    return env.get("GITHUB_REF") == "refs/heads/main" and \
        env.get("GITHUB_EVENT_NAME") in ("schedule", "workflow_dispatch")


def main():
    if not guard(os.environ):
        print("guard refused")
        return 1
    run_info = {"workflow": os.environ.get("GITHUB_WORKFLOW"), "id": int(os.environ.get("GITHUB_RUN_ID", "0")),
                "attempt": int(os.environ.get("GITHUB_RUN_ATTEMPT", "1")),
                "event": os.environ.get("GITHUB_EVENT_NAME")}
    ws = merge.Workspace(_boot.ROOT, sleep=time.sleep)
    api = ghapi.GitHubAPI(os.environ["GITHUB_REPOSITORY"])
    inbox = ghapi.InboxActivity((paths.read_pins(ws.view) or {}).get("inbox_repo"))
    try:
        m = merge.run_merge(ws, api, lambda: dt.datetime.now(rec.UTC).replace(microsecond=0), run_info,
                            inbox=inbox, sleep=time.sleep, log=print)
    except (ghapi.ActivityReadFailed, merge.InboxUnavailable):
        return 1                                   # nothing was committed; the next run retries (R-AN-39, GA-B1)
    for s in m.subjects:
        print(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
