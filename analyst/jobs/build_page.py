"""Build site/analyst/index.html from main's records (analyst-deploy)."""
import sys

from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parent))
import _boot  # noqa: E402,F401

import page


if __name__ == "__main__":
    sys.exit(page.main(["--root", str(_boot.ROOT)]))
