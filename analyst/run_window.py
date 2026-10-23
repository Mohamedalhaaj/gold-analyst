"""The routine's only entry point (prereg R1; register R-AN-24..R-AN-31, R-AN-48, R-AN-49). FROZEN.

  python3 analyst/run_window.py start                 # push the attempt record, or refuse (prints the enum)
  python3 analyst/run_window.py context               # build the context; a no_call context is locked at once
  python3 analyst/run_window.py lock PLAN.json        # validate plan.json and push the call file
  python3 analyst/run_window.py lock --decline        # no_call:refusal
  python3 analyst/run_window.py lesson-start | lesson-lock LESSON.json
  python3 analyst/run_window.py revision-start | revision-lock PLAYBOOK.md
  python3 analyst/run_window.py lock --exam PLAN.json # the exam session (gold-analyst-exam)

Canary mode is read from analyst/MODE.json at origin/main; the same commands then write under analyst/canary/.
Only enums, ids, hashes and paths are printed.
"""
import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import ga  # noqa: E402,F401  (vendored tzdata, reset tzpath)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="run_window.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("start")
    sub.add_parser("context")
    lk = sub.add_parser("lock")
    lk.add_argument("plan", nargs="?")
    lk.add_argument("--decline", action="store_true")
    lk.add_argument("--exam", action="store_true")
    sub.add_parser("lesson-start")
    ll = sub.add_parser("lesson-lock")
    ll.add_argument("lesson")
    sub.add_parser("revision-start")
    rl = sub.add_parser("revision-lock")
    rl.add_argument("playbook")
    a = ap.parse_args(argv)
    repo = HERE.parent
    if a.cmd == "lock" and a.exam:
        from ga import examlock
        return examlock.run(repo, a.plan, decline=a.decline)
    from ga.session import Session
    s = Session(repo)
    if a.cmd == "start":
        return s.start()
    if a.cmd == "context":
        return s.context()
    if a.cmd == "lock":
        if not a.decline and not a.plan:
            print("error no_plan_path")
            return 1
        return s.lock(a.plan, decline=a.decline)
    if a.cmd == "lesson-start":
        return s.lesson_start()
    if a.cmd == "lesson-lock":
        return s.lesson_lock(a.lesson)
    if a.cmd == "revision-start":
        return s.revision_start()
    if a.cmd == "revision-lock":
        return s.revision_lock(a.playbook)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 - print only the exception type, never data
        print(f"error {type(e).__name__}")
        sys.exit(1)
