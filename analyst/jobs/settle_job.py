"""analyst-settle entry point (workflow .github/workflows/analyst-settle.yml): the closer, settlement, the state and
the ledger. Prints only ids, states, counts and enums (`activity_read=token|anonymous|failed`)."""
import datetime as dt
import os
import sys
import time

from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parent))
import _boot  # noqa: E402,F401

import ga  # noqa: F401
from ga import ghapi, merge, paths, rec, settle_run


def guard(env):
    """The run guard (R-AN-40, §16.18 A6): main only."""
    return env.get("GITHUB_REF") == "refs/heads/main"


def main():
    if not guard(os.environ):
        print("guard refused")
        return 1
    run_info = {"workflow": os.environ.get("GITHUB_WORKFLOW"), "id": int(os.environ.get("GITHUB_RUN_ID", "0")),
                "attempt": int(os.environ.get("GITHUB_RUN_ATTEMPT", "1")),
                "event": os.environ.get("GITHUB_EVENT_NAME")}
    ws = merge.Workspace(_boot.ROOT, sleep=time.sleep)
    api = ghapi.GitHubAPI(os.environ["GITHUB_REPOSITORY"])
    pins = paths.read_pins(ws.view)
    inbox = ghapi.InboxActivity((pins or {}).get("inbox_repo"))
    verdict = settle_run.fetch_verdict(pins)
    try:
        st, done = settle_run.run_settle(ws, api, lambda: dt.datetime.now(rec.UTC).replace(microsecond=0), run_info,
                                         verdict=verdict, sleep=time.sleep, inbox=inbox, log=print)
    except (ghapi.ActivityReadFailed, merge.InboxUnavailable):
        return 1                                   # nothing was committed; the next settle run retries (GA-B1)
    print(f"state {st['state']} n {st['n']} processed {len(done)} calls_enabled {st['calls_enabled']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
