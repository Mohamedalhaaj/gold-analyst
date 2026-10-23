# gold-analyst routine prompt (frozen)

You run one step of a pre-registered paper test. Run only the commands below, in order, from the repository
root, which is your working directory. Run each command exactly as written and on its own: no `cd`, nothing
before or after it. Read only the files named here. Write only the one file named in your step, with the Write
tool at its absolute path under the repository root; to correct it, write the whole file again. Do not edit,
delete, commit or push anything yourself, do not run git, and do not use the web. Never print the contents of
plan.json, lesson.json or a playbook. A guard (`.claude/hooks/guard.py`) refuses any other action; if it
refuses one, do not try another way.

## CALL (routines analyst-w1 and analyst-w2)
1. `python3 analyst/run_window.py start`. If the output starts with `refused` or `error`, stop.
2. `python3 analyst/run_window.py context`. If the output starts with `locked`, `refused` or `error`, stop.
3. Read `analyst/prompts/system.md`, then `.analyst_run/context.json`. Write only `.analyst_run/plan.json`, as
   system.md describes.
4. `python3 analyst/run_window.py lock .analyst_run/plan.json`. If the output starts with `invalid`, correct
   plan.json and run step 4 again; the script stops after 3 invalid tries. To give no plan, run
   `python3 analyst/run_window.py lock --decline` instead.
5. Stop.

## LESSON (routine analyst-lesson)
1. `python3 analyst/run_window.py lesson-start`. If the output is `none` or starts with `refused` or `error`, stop.
2. Read `analyst/prompts/lesson.md`, then `.analyst_run/lesson_input.json`. Write only `.analyst_run/lesson.json`.
3. `python3 analyst/run_window.py lesson-lock .analyst_run/lesson.json`. If the output starts with `invalid`, correct
   lesson.json and run step 3 again.
4. Go back to step 1 (one lesson at a time, oldest first).

## REVISION (routine analyst-revision)
1. `python3 analyst/run_window.py revision-start`. If the output starts with `refused` or `error`, stop.
2. Read `analyst/prompts/revision.md`, then `.analyst_run/revision_input.json`. Write only `.analyst_run/playbook.md`.
3. `python3 analyst/run_window.py revision-lock .analyst_run/playbook.md`. If the output starts with `invalid`,
   correct playbook.md and run step 3 again.
4. Stop.
