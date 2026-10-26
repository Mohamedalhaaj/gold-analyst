# gold-analyst

A pre-registered paper test of an LLM "analyst" for XAU/USD (xauusd-quant `research/analyst/ANALYST_V1_PREREG.md`,
DRAFT v0.6, and `research/system/IMPLEMENTATION_REGISTER.md` §16 with its amendments §16.18 A6 to A12). It is not
advice and it never trades. Nobody has shown that the analyst has an edge; the expected result is "not promoted".

## How it runs
- Two scheduled Claude Code routines (W1 London, W2 New York) run only the frozen scripts named in
  `analyst/ROUTINE_PROMPT.md`: `start` pushes an attempt record, `context` builds the context, the agent writes
  `plan.json`, and `lock` validates it and pushes the call file.
- The context (`analyst-context/3`, register R-AN-87, §16.18 A9 and A12): past days come from the mirrored system
  store (Dukascopy M1); the model day's hours, P_ref, the five pairs of the dollar proxy and the AUD/JPY risk proxy
  (`risk`, null when its request fails, never a reason to void the window) come from Twelve Data `time_series` 1h
  bars in UTC (`analyst/ga/td.py`), plus one 1-minute `fresh` bar that measures how far Twelve Data lags. At most 8
  requests a window (XAUUSD, the five pairs, AUDJPY, `fresh`), 8 s apart from start to start (the plan allows 8 a
  minute and 800 a day), 10 s a try, 2 tries only after no response or a 5xx, 120 s in all. The requests carry no
  key: the routines' environment holds one network secret for `api.twelvedata.com`, which Anthropic's agent proxy
  adds as `Authorization: apikey <key>` outside the session, so the code never sees the key. The context keeps, per request, the URL, the HTTP
  status, Twelve Data's code, the classification, the body's size and sha256, and the parsed bars it uses, never the
  response text. An XAUUSD failure while a listed hour is required is `no_call:data` (`td_key`, `td_credits`, ...),
  with no further request. Levels are shown as Twelve Data gives them, with a fixed `sources` note; settlement, the
  twins, the tracker and the judge's checks stay on Dukascopy, which the GitHub runners fetch. The Dukascopy version
  of the session code is kept at the tag `analyst-dukascopy-v0.6` (79b9247, created by the owner before the merge).
- Since §16.18 A12 the context also has `levels` (`analyst/ga/levels.py`, R-AN-88), computed from its own bars with
  no request: the previous day's high, low and close, the week's open and today's Asia range (server 01:00-10:00),
  each with its distance from P_ref in D_ref units, the 1, 5 and 20-day returns, the Asia width ratio and the 5/20-day
  range ratio; `bars_d1` covers 25 history weekdays (`bars_h1` still 10). The system prompt frames a discretionary
  session trader under test, playbook v0 holds the validated facts, the measured cost and a framework labelled
  "hypotheses, not proven", and a `stay_out` plan (`analyst-plan/2`) names one `stay_out_reason`: `event_risk`,
  `no_setup`, `conflicting_signals` or `mid_range`. The A12 text awaits the client's approval before it is pushed.
- The routines are attached only to the agent inbox `Mohamedalhaaj/gold-analyst-inbox` (public, GitHub Actions
  disabled, no secret). Its `main` is a fast-forward copy of this repository's main, kept level by
  `analyst-inbox-mirror`; sessions clone it and push only to its `claude/analyst-*` branches. No session can write
  this repository. No routine carries a claude.ai connector or plugin, and none is created from, or runs as threads
  of, a claude.ai project, whose cloud threads run no repository hook (third review of A7): each routine of
  `analyst/ROUTINE_CONFIG.json` records `project` null, checked by `tests/test_routine_config.py`.
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
  them inside `context`. Since §16.18 A9 `net.twelvedata` (one keyless 1-minute request in `probe`; the context's own
  first request in a canary, at no extra credit) is the data control, and `net.dukascopy` must be refused. The record also holds `cc_version`, the session's Claude Code version (the first line of
  `claude --version`, as in the attempt and call records); it is not a probe and changes no verdict. C20 passes only
  with 2.1.295 or later, which the hook's `onFailure` requires.
- The session guard (register R-AN-85, §16.18 A7): `.claude/settings.json` runs `.claude/hooks/guard.py` before every
  tool call of a session (a PreToolUse hook for every tool, `onFailure: block`) and denies WebFetch, WebSearch, Agent
  and Skill as a second layer. The guard lets through only the nine command lines of `analyst/ROUTINE_PROMPT.md` and
  `probe`, each exactly as written and run from the repository root, reads inside the repository outside `.git`, and
  a Write to `.analyst_run/plan.json`, `lesson.json` or `playbook.md`; it refuses everything else (other commands,
  `gh`, `git`, `curl`, any shell construct, other tools, MCP and GitHub tools, writes to code or configuration) and
  fails closed. The settings file also sets the Bash timeouts (`BASH_DEFAULT_TIMEOUT_MS`, `BASH_MAX_TIMEOUT_MS`) to
  3 600 000 ms, above the longest any window command runs by its own timeouts, and the guard refuses a shorter
  `timeout` or a background run, so no step is moved to the background where its output could not be read (second
  review of A7). Its files are frozen, and `.claude/` holds nothing else. The probe routine `analyst-probe` follows
  `analyst/PROBE_SESSION_PROMPT.md` (C20): the probe, two allowed controls, then harmless forbidden steps that the
  guard must refuse before the canary may start.
- The run-log scan (register R-AN-86, owner tool): `python3 analyst/jobs/scan_runs.py analyst/runs/<iso_week>.json`
  (or a C20 record) classifies every tool call of the week's runs with the guard's own rules and prints
  `<VIOLATION|WARNING> <code> <run_id>#<seq>` lines and `scan <violations> <warnings> <calls>`; exit 3 on any
  VIOLATION, 1 for a file that breaks the schema (Write and Edit text must be copied as hashes, never as text). A
  field the run log does not show is left out of the call's input; a call whose input lacks a field the rules need
  (Bash `command`, a file tool's path, Write's `content`) is `scan_unclassifiable`, a WARNING. A chain of harmless
  commands (`cat x; ls`, `cat x | head`) is a harmless near-miss, as a `cd` prefix is; since §16.18 A11 so are the
  read-only `git status` and `git log` forms of R-AN-86 (a)(iv) and a `PushNotification` (d). A runs export also gives
  `VIOLATION routine_project <name>` for a routine whose `project` is missing or not null, and a C20 record's verdict
  must agree with its `cc_version` (pass needs 2.1.295 or later).
- `analyst/MODE.json` selects `canary` (records under `analyst/canary/`, never settled or shown), `live` or `off`.

## Layout
- `analyst/ga/`: the frozen code (session scripts, probes, context, D_ref, merge, settlement, state). The session uses
  only the standard library and `analyst/vendor/` (tzdata 2026.3, the lab's `eproc.py`), so the routine environment
  needs no package manager: access level None, no environment variable, no setup script, and the one network secret
  (`analyst/ROUTINE_CONFIG.template.json`, checked in the export by `tests/test_routine_config.py`).
- `analyst/FROZEN_ANALYST.sha256`: the frozen set (`python -I analyst/jobs/make_frozen.py`).
- `.claude/`: the session guard, and nothing else (no CLAUDE.md, AGENTS.md or .mcp.json anywhere). A Claude Code session
  whose project directory is this repository, a local one included, runs under the guard and can do nothing but the
  routine's steps: maintain the repository from a session started outside it.
- `analyst/display/`: the page and the tracker (display only).
- `mirror/`: inputs copied by a script from the system store (forecast rows of slots 9, 10, 15 and the M1 day files).
- `tests/`: `uv run --group dev pytest`.

No secret is stored in this repository; workflows reference them by name only (`CLOUDFLARE_API_TOKEN` in the
environment `analyst-pages`, `INBOX_MIRROR_KEY` in the environment `analyst-inbox`, both main only).

The analyst judge is not here (prereg R1(a), register R-AN-1): it lives in gold-forward-watch (`judge_analyst/`),
reads this public repository and the inbox as data only, and publishes its verdict at
`<PINS.judge_site_url>/judge_analyst/analyst_verdict.json`, which `analyst-settle` reads (R-AN-70). A verdict counts
only when it is at most 48 hours old and not older than the newest verdict an earlier settle run counted (§16.18 A7:
an old cached copy changes nothing). From the first canary day on, a violation pauses the calls while the verdict
carries it; only in the live period can a new integrity violation, after one the owner cleared, retire the analyst
(R-AN-55). After 48 hours without a fresh verdict the calls pause (`judge_silent`) until the next one.
