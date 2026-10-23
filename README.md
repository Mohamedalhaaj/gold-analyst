# gold-analyst

A pre-registered paper test of an LLM "analyst" for XAU/USD (xauusd-quant `research/analyst/ANALYST_V1_PREREG.md`,
DRAFT v0.6, and `research/system/IMPLEMENTATION_REGISTER.md` §16 with its amendment §16.18 A6). It is not advice and
it never trades. Nobody has shown that the analyst has an edge; the expected result is "not promoted".

## How it runs
- Two scheduled Claude Code routines (W1 London, W2 New York) run only the frozen scripts named in
  `analyst/ROUTINE_PROMPT.md`: `start` pushes an attempt record, `context` builds the context from the mirrored
  system store and Dukascopy hour files, the agent writes `plan.json`, and `lock` validates it and pushes the call file.
- The routines are attached only to the agent inbox `Mohamedalhaaj/gold-analyst-inbox` (public, GitHub Actions
  disabled, no secret). Its `main` is a fast-forward copy of this repository's main, kept level by
  `analyst-inbox-mirror`; sessions clone it and push only to its `claude/analyst-*` branches. No session can write
  this repository. No routine carries a claude.ai connector or plugin (`analyst/ROUTINE_CONFIG.json`, checked by
  `tests/test_routine_config.py`).
- `analyst-merge` (script only) runs main's code on a schedule (every 5 minutes around the windows, around the lesson
  and revision routines, and hourly) and by hand. It reads the inbox's activity in full or stops before writing
  anything, fetches each pushed commit from the inbox as data, checks the path allowlist, takes the push's server
  time from the inbox's activity, posts a witness comment on this repository's pinned issue, copies the one allowed
  file to main, writes the terminal record and the hash chain, and runs the closer. A GitHub outage delays a record
  and does not change it: a run that cannot reach the inbox stops and retries, and the closer closes only windows
  whose pushes are all within its 28-day push range (a longer outage leaves them open for the owner). Time budgets
  bound what one push can cost a run (at most about 13 minutes); a run past its 15-minute budget commits what it
  wrote and leaves the rest, and the closing of windows, to the next run. GitHub may start these scheduled runs
  hours late: a call then reaches the page late, but its lock time does not change.
- `analyst-settle` settles on the mirrored Dukascopy M1 store (lab v2's execution rules with the plan's target),
  writes the drift twins, the e-process state (the lab's frozen `eproc.py`, E* = 400) and `analyst/ledger.csv`.
- `analyst-track` follows an open paper trade hour by hour for display only; `analyst-deploy` builds and deploys the
  Arabic page `/analyst/` when merge, settle or track dispatch it after a commit (and once a day).
- `python3 analyst/run_window.py probe` runs the session probes (register R-AN-84) without a window; canary calls run
  them inside `context`.
- `analyst/MODE.json` selects `canary` (records under `analyst/canary/`, never settled or shown), `live` or `off`.

## Layout
- `analyst/ga/`: the frozen code (session scripts, probes, context, D_ref, merge, settlement, state). The session uses
  only the standard library and `analyst/vendor/` (tzdata 2026.3, the lab's `eproc.py`), so the routine network can
  be `datafeed.dukascopy.com` only with the package managers off.
- `analyst/FROZEN_ANALYST.sha256`: the frozen set (`python -I analyst/jobs/make_frozen.py`).
- `analyst/display/`: the page and the tracker (display only).
- `mirror/`: inputs copied by a script from the system store (forecast rows of slots 9, 10, 15 and the M1 day files).
- `tests/`: `uv run --group dev pytest`.

No secret is stored in this repository; workflows reference them by name only (`CLOUDFLARE_API_TOKEN` in the
environment `analyst-pages`, `INBOX_MIRROR_KEY` in the environment `analyst-inbox`, both main only).

The analyst judge is not here (prereg R1(a), register R-AN-1): it lives in gold-forward-watch (`judge_analyst/`),
reads this public repository and the inbox as data only, and publishes its verdict at
`<PINS.judge_site_url>/judge_analyst/analyst_verdict.json`, which `analyst-settle` reads (R-AN-70). A verdict counts
only when it is at most 48 hours old. From the first canary day on, a violation pauses the calls while the verdict
carries it; only in the live period can a new integrity violation, after one the owner cleared, retire the analyst
(R-AN-55). After 48 hours without a fresh verdict the calls pause (`judge_silent`) until the next one.
