# watcher: the A14 watcher (register R-AN-91)

A Cloudflare Worker with no Claude and no model. It is frozen with the analyst (FROZEN_ANALYST.sha256) and
deployed byte for byte (`no_bundle`), so `analyst/WATCHER_CONFIG.json`'s `script_sha256` is
`shasum -a 256 watcher/worker.js` at the deployed commit.

| File | What it is |
|---|---|
| `worker.js` | The Worker: the trigger rule of R-AN-89, the gates, the Twelve Data request, the fire, the KV log, `GET /log`. |
| `wrangler.toml` | Name `gold-analyst-watcher`, cron `1,31 * * * *`, compatibility date 2026-10-01, the KV binding `WATCHER_LOG`. No secret. |
| `tests/*.test.mjs` | `node --test watcher/tests/*.test.mjs` (Node 24, no install). |
| `tests/ref_wakes.py` | The Python reference of the rule, written apart from `worker.js`, after wake_freq.py. |
| `tests/wake_freq_replay.py` | wake_freq.py itself on the recorded bars, against the reference (needs pandas and wake_freq.py). |
| `tests/fixtures/` | Recorded bars (Dukascopy mid M30, 2026-09-01..10-26), synthetic bars (2026-09-27..11-27), expected decisions. |

## What it does

At minute 1 and 31 of every hour it takes the check T (the scheduled time floored to the half hour) and acts only
when d = tday(T) = the UTC date of T + 2 h is a model weekday >= MODE.json `a14_from` and T is in G(d) (22:30 UTC of
the day before to L_d = 16:30 UTC on EDT, 17:30 on EST). Then:

1. It reads, without any credential, `analyst/MODE.json` and `analyst/STATE.json` on inbox main
   (`raw.githubusercontent.com/Mohamedalhaaj/gold-analyst-inbox/refs/heads/main/...`), and for W1, W2, W1b, W2b and the
   trigger windows it fired on d (from its own log), each with a check earlier than T: the terminal on main, the n = 1
   attempt record and the call file on main, else on their `claude/analyst-<mode>-attempt-…-1` and
   `claude/analyst-<mode>-call-…` branches. A failed read counts as absent. From them it forms E(T) and O(T); for
   model days >= MODE.json `a15_from` (§16.18 A15) it reads no call file and forms no O(T), so `trade_open` never
   occurs: a window during an open trade is fired like any other and becomes a review in its session.
2. `mode_off` or `calls_disabled` → no fire. Otherwise R(T): `trade_open`, `day_cap`, W1/W2 (`fixed`, their own
   routines fire them), W1b/W2b (fire, no Twelve Data request), a holiday day or g(T) < 60 (`none`), else one Twelve
   Data request (R-AN-90's URL, `Authorization: apikey …`, R-AN-87's User-Agent, at most 2 tries, the second only after
   td_code 0 or 5xx, 8 s apart); not `ok` → `td_<reason>`; then step 6 → `trigger:<reason>` (fire) or `none`.
3. The fire is one POST to FIRE_URL with `Authorization: Bearer <FIRE_TOKEN>`, `anthropic-beta:
   experimental-cc-routine-2026-04-01`, `anthropic-version: 2023-06-01` and the body `{}` (no text). 2xx is a fire;
   after 429, 5xx or a connection error it retries once 60 s later; no try starts after T + 10 min. A seventh fire on a
   model day is logged `fire_cap` and not sent.
4. One log entry per check in KV (`log/<model_day>`): `{check_utc, model_day, decision, fired, http, tries, utc}`.
   `GET /log?day=YYYY-MM-DD` returns a model day's entries; it is the only route (no `/status`) and holds no secret.

## Choices where the register is silent

- MODE.json unreadable → logged `mode_off` (a14_from unknown, no fire); STATE.json unreadable → `calls_disabled`.
- W1b and W2b are fired on a holiday of their own exchange too, as R-AN-91's decision list says; `start` refuses them
  (`skipped:holiday`). Trigger windows are never fired on a holiday-table day (step 4).
- A second run of a check already in the log does nothing (a repeated cron delivery cannot fire twice).
- The first fire is also bounded by T + 10 min (a cron run delayed past it does not fire; logged with `tries` 0).
- The holiday table and the inbox repository are constants in `worker.js`; tests compare them with
  `analyst/tables/holidays.csv` (byte for byte) and `analyst/PINS.json`. After the table's horizon (2027-12-31) the
  watcher still fires and `start` refuses (`no_table`).
- Workers subrequests: a check reads 2 files plus 1 to 5 per earlier window (about 25 late in a busy day; 50 at worst
  when nothing has been merged for hours). The Workers free plan allows 50 subrequests per run; above that a read
  fails, counts as absent, and `start` stays the gate.
- raw.githubusercontent.com can serve a file up to about 5 minutes old; every window it reads was pushed at least
  15 minutes before the next check.

## Tests

```
node --test watcher/tests/*.test.mjs
/Users/mohamedalhaj/Documents/xauusd-quant/.venv/bin/python -I watcher/tests/wake_freq_replay.py <dir of wake_freq.py> /Users/mohamedalhaj/Documents/gold-forward-watch
```

- `replay.test.mjs` runs `worker.js` unchanged, check by check, over both fixtures with a fake inbox that moves each
  session's files from its branches to main one check later and writes the terminal a check after that, and compares
  every check's decision and every wake with `expected_*.json` from `ref_wakes.py`. It also re-runs `ref_wakes.py`
  (when python3 is present) to show the expected files are current. Recorded: 20 days, 740 checks, 99 wakes.
  Synthetic: 25 days (the US change of 2026-11-01, Sunday opens at 22:00/23:00 UTC, missing bars, Thanksgiving),
  965 checks, 111 wakes.
- `rule.test.mjs`: the DST table of R-AN-8 (every row, |G(d)| 37 or 39, L_d), tday and Sunday opens, conditions
  (R20 with 19 days, missing bar_T, thresholds), R(T)'s order, Twelve Data classification, the file checks.
- `worker.test.mjs`: when it acts, mode/state gates, W1/W2 not fired, the W1b fire's request, open trades from the
  call branch and the terminal, exit = entry not open (A14-D4), day cap, fire cap, retries on 429/5xx/connection
  errors and the T + 10 min bound, Twelve Data 429 (not retried) and 5xx (retried 8 s later), missing bars, `/log`.
- `wake_freq_replay.py`: wake_freq.simulate (unchanged, p_enter 0) equals `legacy_wakes()` and, with the A14 check
  times and no trades, the A14 rule gives the same wakes on all 20 recorded weekdays (116 wakes).

## Deploy (owner) and secrets (client)

Deploy from the A14 build commit on gold-analyst main, in a clean checkout, then set the secrets; until MODE.json has
`a14_from` the deployed Worker reads MODE.json each half hour and does nothing else.

```
cd <clean checkout of gold-analyst at the A14 build commit>/watcher
npx --yes wrangler@4.151.1 whoami          # login check; if not logged in: npx --yes wrangler@4.151.1 login
npx --yes wrangler@4.151.1 deploy          # creates the Worker, the cron and the KV namespace (automatic provisioning)
git checkout -- wrangler.toml              # if wrangler wrote the namespace id back; the binding stays linked
security find-generic-password -s twelvedata-api-key -w | tr -d '\n' | npx --yes wrangler@4.151.1 secret put TD_API_KEY --name gold-analyst-watcher
npx --yes wrangler@4.151.1 secret put FIRE_URL --name gold-analyst-watcher     # paste analyst-wake's Fire URL
npx --yes wrangler@4.151.1 secret put FIRE_TOKEN --name gold-analyst-watcher   # paste analyst-wake's API token
npx --yes wrangler@4.151.1 secret list --name gold-analyst-watcher             # three names, no values
```

Then the owner commits `analyst/WATCHER_CONFIG.json` (R-AN-91) with `script_sha256` = `shasum -a 256 worker.js`,
`kv_namespace` from `npx --yes wrangler@4.151.1 kv namespace list`, and sets MODE.json `a14_from` before 22:00Z of
the day before.
