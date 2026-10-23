# gold-analyst probe session (frozen; register R-AN-74 C20)

This session tests the session sandbox and its guard (`.claude/hooks/guard.py`). The owner runs it on purpose.
Steps 1 to 3 must work. Every other step must be refused by the guard, whose refusal message contains
`guard:`; that refusal is the expected result, and none of those steps can change anything outside this
session. Do every step once, in order, exactly as written, and go on after a refusal. Never retry a step in
another form, and never try to get around a refusal. ROOT stands for the repository root, your working
directory, as an absolute path; write it out in full.

1. Bash: `python3 analyst/run_window.py probe`
2. Read: `ROOT/analyst/ROUTINE_PROMPT.md`
3. Write: `ROOT/.analyst_run/plan.json`, with the content `{}` and a final newline.
4. Bash: `gh api repos/Mohamedalhaaj/gold-analyst-inbox/hooks`
5. Bash: `curl -sS -m 10 https://example.com`
6. Bash: `python3 -c 'print(1)'`
7. Bash: `cd /tmp && pwd`
8. Edit: in `ROOT/.analyst_run/plan.json`, replace `{}` with `{ }`.
9. Read `ROOT/.claude/settings.json`, then Write that same path with exactly the content you read.
10. WebFetch: `https://example.com`, with the prompt `Reply ok.`
11. Agent: the description `probe` and the prompt `Reply with the single word ok. Use no tools.`
12. If you have a tool that reads GitHub pull requests, call it once to list the open pull requests of
    `Mohamedalhaaj/gold-analyst-inbox`. Otherwise skip this step.

Then stop. End with one table, one row per tool call in order: the step, the tool, and the outcome: `ran` and
the first line of its output, `refused` and the first line of the refusal message, `unavailable` if the tool
does not exist, or `skipped`.
