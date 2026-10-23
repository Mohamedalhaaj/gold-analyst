"""gold-analyst core (FROZEN; register §16, ANALYST_V1_PREREG.md DRAFT v0.5).

Importing this package puts analyst/vendor on sys.path and makes zoneinfo read ONLY the vendored tzdata package
(R-AN-8: `zoneinfo.reset_tzpath(to=())`). The session scripts use the standard library and analyst/vendor only, so a
routine session needs no package install (R1 Isolation (d): dependencies are vendored).
"""
import sys
import zoneinfo
from pathlib import Path

ANALYST_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = ANALYST_DIR.parent
VENDOR_DIR = ANALYST_DIR / "vendor"
TZDATA_VERSION = "2026.3"          # the pinned tzdata package (IANA 2026c), listed in FROZEN_ANALYST.sha256

if str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))

zoneinfo.reset_tzpath(to=())
zoneinfo.ZoneInfo.clear_cache()

import tzdata as _tzdata  # noqa: E402

if getattr(_tzdata, "__version__", None) != TZDATA_VERSION:
    raise ImportError(f"tzdata {getattr(_tzdata, '__version__', None)} loaded, the vendored {TZDATA_VERSION} is pinned "
                      f"(import ga before anything that imports tzdata)")
