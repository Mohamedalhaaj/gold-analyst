"""Write analyst/FROZEN_ANALYST.sha256 (register R-AN-7). Build/freeze tool; prints the code_sha8."""
import sys

from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parent))
import _boot  # noqa: E402,F401

from ga import frozen


def main():
    text = frozen.write_manifest(_boot.ROOT)
    print(f"{len(text.splitlines())} files, code_sha8 {frozen.code_sha8(text.encode())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
