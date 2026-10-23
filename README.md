# gold-analyst

A pre-registered paper test of an LLM "analyst" for XAU/USD (xauusd-quant `research/analyst/ANALYST_V1_PREREG.md`,
DRAFT v0.5, and `research/system/IMPLEMENTATION_REGISTER.md` §16). It is not advice and it never trades. Nobody has
shown that the analyst has an edge; the expected result is "not promoted".

## How it runs
- Two scheduled Claude Code routines (W1 London, W2 New York) run only the frozen scripts named in
  `analyst/ROUTINE_PROMPT.md`: `start` pushes an attempt record, `context` builds the context from the mirrored
  system store and Dukascopy hour files, the agent writes `plan.json`, and `lock` validates it and pushes the call file.
  Sessions push only to `claude/analyst-*` branches.
- `analyst-merge` (script only) runs main's code on every such push: it checks the path allowlist, records the push's
  server time, posts a witness comment on the pinned issue, copies the one allowed file to main, writes the terminal
  record and the hash chain, and runs the closer.
- `analyst-settle` settles on the mirrored Dukascopy M1 store (lab v2's execution rules with the plan's target),
  writes the drift twins, the e-process state (the lab's frozen `eproc.py`, E* = 400) and `analyst/ledger.csv`.
- `analyst-track` follows an open paper trade hour by hour for display only; `analyst-deploy` builds and deploys the
  Arabic page `/analyst/`.
- `analyst/MODE.json` selects `canary` (records under `analyst/canary/`, never settled or shown), `live` or `off`.

## Layout
- `analyst/ga/`: the frozen code (session scripts, context, D_ref, merge, settlement, state). The session uses only the
  standard library and `analyst/vendor/` (tzdata 2026.3, the lab's `eproc.py`).
- `analyst/FROZEN_ANALYST.sha256`: the frozen set (`python -I analyst/jobs/make_frozen.py`).
- `analyst/display/`: the page and the tracker (display only).
- `mirror/`: inputs copied by a script from the system store (forecast rows of slots 9, 10, 15 and the M1 day files).
- `tests/`: `uv run --group dev pytest`.

No secret is stored in this repository; workflows reference them by name only.
