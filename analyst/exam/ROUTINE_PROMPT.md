# gold-analyst exam routine prompt (frozen)

You run one window of a pre-registered historical exam. Run only the commands below, from the repository root. Read
only the files named here and write only `.analyst_run/plan.json`. Do not run git and do not use the web.

1. Read `analyst/prompts/system.md`, then `context.json`, then `analyst/playbooks/v0.md`. The context is anonymized:
   times are relative and prices are rescaled; treat them exactly as the system file describes.
2. Write only `.analyst_run/plan.json`, as system.md describes.
3. `python3 analyst/run_window.py lock --exam .analyst_run/plan.json`. If the output starts with `invalid`, correct
   plan.json and run step 3 again; the script stops after 3 invalid tries. To give no plan, run
   `python3 analyst/run_window.py lock --exam --decline`.
4. Stop.
