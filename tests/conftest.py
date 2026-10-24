import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "analyst", ROOT / "analyst" / "display", ROOT / "analyst" / "jobs", ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# Git in the tests reaches local repositories only: a fetch or push to https://github.com (the merge's inbox URL, the
# probes' direct form) fails at once instead of going to the network.
os.environ["GIT_ALLOW_PROTOCOL"] = "file"

import ga  # noqa: E402,F401  (must be first: the vendored tzdata)
