"""Put analyst/ on sys.path for the job scripts (they run with `python -I`)."""
import sys
from pathlib import Path

ANALYST = Path(__file__).resolve().parents[1]
ROOT = ANALYST.parent
for p in (ANALYST, ANALYST / "display"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
