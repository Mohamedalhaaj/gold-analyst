# judge_analyst: the analyst judge (blind build)

The independent judge of Analyst v1 (xauusd-quant `research/analyst/ANALYST_V1_PREREG.md` DRAFT v0.5, §8 as amended
by "Change v0.5" M7; `research/system/IMPLEMENTATION_REGISTER.md` §16, R-AN-66..R-AN-70 and the items they cite).
It was built **blind**: from the prereg, the register and the record locations the register names, without reading
the analyst code under `analyst/**`. Where the analyst code and the register differ, the judge reports a finding to
adjudicate (`spec_gap`, `judge_error` or `system_error`, as J5).

Script only. It never receives the Anthropic key or the Cloudflare token, runs no model, executes nothing from an
agent-pushed commit (pushed commits are fetched as data and read with `git diff`/`git cat-file`), and prints only
enums, counts, keys and hashes (no model text reaches stdout, a report or a commit message).

## Run

```
python -I judge_analyst/run_judge.py --out reports/judge_analyst --fetch-pushes            # in CI (analyst-judge.yml)
python -I judge_analyst/run_judge.py --api none --hours none --now 2026-12-07T12:00:00Z    # offline dry run
python -I judge_analyst/run_judge.py --store ../gold-forward-watch                         # with the source store
```

Inputs (R-AN-66): a full clone of gold-analyst `main`; the GitHub API for it (`/activity`, the pinned witness issue's
comments, the witness runs; `actions: read`, `issues: read`, token from `GITHUB_TOKEN`); Dukascopy re-downloads of the
recorded hour files (cached once in `hours.csv`); optionally gold-forward-watch (`--store`); PINS.json and the
manifests.

Tests (synthetic world + tamper suite; about 10 minutes, most of it building git worlds):

```
uv run --frozen pytest -p no:cacheprovider judge_analyst/tests
```

## Checks (R-AN-67) and what they implement

| Check | Prereg §8 item | Severity |
|---|---|---|
| A0 | "Shallow clone or unreachable run record: WARNING, never SOUND" (also: API, hour files or source store unreadable; the judge's own manifest differs) | WARNING, never latched |
| A1 | server lock_time > entry − 15 on a call not recorded as `no_call:late` (lock = activity timestamp of the call push, else the witness run's created_at) | VIOLATION |
| A2 | an attempt push outside [entry − 45, deadline] marked non-void; more than 2 accepted attempts; a call without an earlier accepted attempt for its attempt_n; a counted call whose attempt is void | VIOLATION |
| A3 | `agent_scope`, `rewrite`, `invalid_content`, `attempt_cap` witnesses; an accepted push whose diff breaks R-AN-33; a record on main without an accepted witness; an unwitnessed `claude/analyst-*` push after 24 h whose diff breaks R-AN-33 (conforming: WARNING); a push to main by an actor other than the workflow bot or the owner; a `CLAUDE.md`, `AGENTS.md` or `.claude/` anywhere (R1(a)); the other witness codes of R-AN-33 | VIOLATION / WARNING as R-AN-33 |
| A4 | the context rebuilt from the mirror at `repo_head_sha`, the judge's re-downloads, the recorded asof_utc and PARAMS differs beyond R-AN-68; asof_utc > lock_utc + 60 s; an admitted hour with h + 1 h > asof − L; repo_head_sha not on main | VIOLATION |
| A5 | S, T, D_ref, side or a plan field not derivable from the plan and the context (R-AN-21, R-AN-26, R-AN-27) | VIOLATION |
| A6 | a broken prev_chain link; a witness file whose payload differs from its comment; a file without its comment or a bot comment without its file (after 24 h); a witness run that is not analyst-merge/analyst-settle | VIOLATION |
| A7 | a create-only/write-once file modified or deleted in main's history; an append-only file changed other than by whole-line appends; a force push or deletion of main; main not descending from the judge's previous anchor | VIOLATION |
| A8 | an expected window (MODE.json periods) with zero or two terminal witnesses / no terminal record; a terminal outside the mode's period | VIOLATION |
| A9 | START.json differs from R-AN-38 (first live called/stay_out, first_day rule, same commit as that terminal) | VIOLATION |
| A10 | owner-attested (`analyst/runs/*.json`): an in-span run without an attempt (R-AN-60), a run on another model | VIOLATION (owner-attested) |
| A11 | a re-downloaded hour file whose sha256 differs (its hour is left out of A4); settlement, opposite side, path summary, twin, ledger, E_n, state events or STATE.json recompute mismatch; mirror vs source; unmatched session id (`unverified_model`); vendored eproc/calendar/holiday table/frozen-manifest drift; record bytes not in the R-AN-3 form | WARNING + ADJUDICATIONS row |

Independence: E_n and K are recomputed in pure Python (the test suite checks them against the lab's frozen
`eproc.py`, sha256 ee8d456a…, kept in `tests/fixtures/` only); the holiday rows are the register's own (R-AN-9), the
event calendars are the judge's sha-pinned copies (R-V1-15); windows come from zoneinfo and are unit-tested against the
register's R-AN-8 table.

## Outputs (`reports/judge_analyst/`, the only path the workflow may commit)

- `analyst_verdict.json` (`analyst-verdict/1`: schema, asof_utc, head_sha, analyst_verdict SOUND|WARNING|VIOLATION,
  violation_keys, cleared_keys) and `REPORT.md` (snapshots).
- `FINDINGS.csv` (first time each key was seen), `VERDICTS.csv` (one row per run; its last head_sha is the A7 anchor),
  `evidence.csv` (R-AN-69, one row per activity entry, witness comment and witness run first seen), `hours.csv` (the
  judge's hour-file cache): append-only.
- `ADJUDICATIONS.csv` (the owner's, append-only): `utc,ref,check,subject,decision,note`. A VIOLATION key is cleared by
  `ref=latch, decision=clear` with `utc` at or after its first_seen_utc (R-JV-2). A WARNING key is adjudicated by
  `ref=warning` with `judge_error`, `spec_gap`, `accepted` or `ops`; `system_error` puts a VIOLATION in force until a
  later `resolved` row for the same subject.

Keys are `<check>|<code>|<path>|<commit>` (R-JV-1); the commit is the one that added the file on main (or the push
sha / anchor). VIOLATION keys latch until cleared, even when a later run cannot re-find them (an activity entry that
aged out); a cleared key never clears another (R-JV-4).

## Judge decisions (points the register leaves open; to be logged in the register before the freeze)

- J-D1 Check `A0` (inputs) is the judge's addition for "never SOUND" conditions; it is never latched or adjudicated.
- J-D2 R-AN-68 "exact" for prices built from integer points is grid-exact: |a − b| <= 1e-12·max(1, |a|, |b|), far below
  any grid step, so a last-bit difference between decimal-to-double routes is not a finding.
- J-D3 Grace times: a window must have its terminal 36 h after deadline + 20 min; a bot comment without a file and an
  unwitnessed claude push are findings after 24 h; a missing settled/twin record after 36 h from the exit.
- J-D4 Holidays: the register's rows up to 2027-12-31; the analyst table's rows only beyond that horizon (OPS appends).
- J-D5 A `no_call:data` context passes when its detail is any applicable one (`store_stale`, `hour_absent`,
  `store_mismatch`) and what it fetched is a prefix of the expected file sequence (none for `store_stale`).
- J-D6 memory.settled[].reason: the outcome's exit reason or the call's reason_ar are both accepted.
- J-D7 An unknown FX pair may be `null` or an object with a null `pair_pct`.
- J-D8 memory.playbook.version may be `k` or `"v<k>"`.
- J-D9 A runs-export status counts as success when it starts with "succe" or equals "completed".
- J-D10 Canary calls are judged like live ones (A4/A5/A6/A8) with keys naming the canary path; clear them before the
  freeze. Settlement, ledger and states are live only.
- J-D11 Server times: the activity entry on the push's ref (else the earliest with that `after`), else the judge's own
  first-seen evidence row, else the witness run's created_at.
- J-D12 path_summary.exit_after_min accepts exit − t0 or exit − t0 + 1 minutes; a void/no_quote `gross_usd` may be null
  or −S; a time exit with no quoted path minute uses t1 − 1 as its exit minute.
- J-D13 `analyst/canary/hours/<day>.csv` is a snapshot (R-AN-72 fills the re-check the next day), not create-only.
- J-D14 A10 uses PARAMS.json at HEAD for T_run and T_setup; calls count as disabled between PAUSED and its RESUMED and
  after RETIRED; MODE `off` periods are not reconstructed from history.
- J-D15 Only `analyst/START.json` is checked; a re-freeze's `START_<tag>.json` is not handled yet.

## Open points

1. **Location.** R-AN-1 puts the analyst judge in gold-forward-watch, and R1(a) says gold-analyst holds no judge
   outputs. The build task placed it here, in gold-analyst. Consequences: the routine sessions can read the judge's
   reports in their checkout (R1(a)); gold-forward-watch is private, so without a read token this judge cannot compare
   the mirror with its source (`A0 source_store_unavailable`, so the verdict is never SOUND); R-AN-70's verdict URL.
   The code is repository-agnostic (`--repo`, `--store`), so moving `judge_analyst/` and the workflow into
   gold-forward-watch (checking out the public gold-analyst as data) is a copy. Needs the owner's decision.
2. **Verdict feed.** analyst-settle reads `<PINS.judge_site_url>/judge_analyst/analyst_verdict.json`. This judge has
   no deploy step and no Cloudflare token; the file is committed at `reports/judge_analyst/analyst_verdict.json`. Either
   analyst-deploy publishes it, or judge_site_url points at the raw file of the public repository.
3. **Witness issue.** Anyone can comment on an issue of a public repository; the judge trusts only comments by
   `github-actions[bot]`. Locking the pinned issue to collaborators is recommended.
4. **Cross-run.** The judge has not yet judged records written by the analyst code. Before the freeze it runs on the
   canary records (C14), and every finding is adjudicated or fixed; the register items behind J-D5..J-D9 and J-D12 are
   amended in a dated edit.
5. Freeze: `FROZEN_JUDGE_ANALYST.sha256` lists every file of `judge_analyst/` and the workflow; the tag
   `judge-analyst-freeze` is set at the freeze (not now).
