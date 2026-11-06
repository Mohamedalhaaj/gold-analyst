"""analyst-track entry point (workflow .github/workflows/analyst-track.yml, dispatched by analyst-merge with a
call_id). Display only. Each update commits site/track/<call_id>.json and dispatches analyst-deploy; this job never
holds the Cloudflare token. §16.18 A17-R12: a trade whose time window ended `review_extend` is tracked to day_end(d)
(analyst-merge dispatches it again with the time window's terminal)."""
import os
import re
import subprocess
import sys

from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parent))
import _boot  # noqa: E402,F401

import ga  # noqa: F401
from ga import clock, ghapi, paths, rec  # noqa: E402
from ga.repo import DirView  # noqa: E402

import track  # noqa: E402


def git(*a, check=True):
    return subprocess.run(["git", "-C", str(_boot.ROOT), *a], check=check, capture_output=True)


def main():
    cid = os.environ.get("CALL_ID", "")
    if not clock.CALL_ID_RE.match(cid):
        print("bad call_id")
        return 1
    view = DirView(_boot.ROOT)
    call = view.read_json(paths.call_path("live", cid))
    if not call or call.get("side") not in (1, -1):
        print("not a buy/sell live call")
        return 0
    L = int((paths.read_params(view) or {}).get("L_min", 50))
    w = clock.window_of(cid)
    xid = clock.call_id(w.day, clock.time_tag(w.exit_utc - clock.HALF))     # its time window (§16.18 A17-R12)
    xt = view.read_json(paths.terminal_path("live", xid)) if paths.a17_applies(paths.read_mode(view), w.day) else None
    if xt and xt.get("status") == "review_extend":
        call = dict(call, exit_utc=rec.iso(clock.day_end(w.day)))
    api = ghapi.GitHubAPI(os.environ["GITHUB_REPOSITORY"])
    out = _boot.ROOT / "site" / "track" / f"{cid}.json"

    def write(st):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(rec.record_bytes(st))

    def publish():
        git("add", "--", f"site/track/{cid}.json")
        if git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return
        git("-c", "user.name=github-actions[bot]",
            "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "commit", "-q", "-m", f"analyst-track {cid}")
        for _ in range(5):
            if git("pull", "-q", "--rebase", "origin", "main", check=False).returncode == 0 and \
                    git("push", "-q", "origin", "HEAD:main", check=False).returncode == 0:
                break
        try:
            api.dispatch("analyst-deploy.yml", "main", {})
        except Exception:  # noqa: BLE001
            pass

    st = track.run(call, L, write, publish)
    print(f"track {cid} {st['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
