"""`lock --exam` for the historical exam session (prereg R6, §12; register R-AN-79, R-AN-80). FROZEN.

The exam repository's main is one parentless commit holding context.json (one anonymized window), the routine prompt,
the plan schema, the system prompt, playbook v0 and the scripts this module needs (rec, plan, gitops). It imports
nothing that knows dates, prices or settlement. The plan record goes to `plans/<exam_id>.json` on the branch
`claude/exam-<exam_id>`; one draw per session.
"""
import datetime as dt
import json
import os
from pathlib import Path

from . import gitops, plan, rec

SCHEMA = "analyst-exam-plan/1"
CONTEXT_SCHEMA = "analyst-exam-context/1"
MAX_FAILURES = 3


def run(repo_dir, plan_file=None, decline=False, git=None, now=None, out=print, env=None):
    repo_dir = Path(repo_dir)
    git = git or gitops.Git(repo_dir)
    now = now or (lambda: dt.datetime.now(rec.UTC).replace(microsecond=0))
    env = os.environ if env is None else env
    work = repo_dir / ".analyst_run"
    work.mkdir(parents=True, exist_ok=True)
    stp = work / "exam_state.json"
    st = json.loads(stp.read_text("utf-8")) if stp.exists() else {"failures": 0}
    if st.get("locked"):
        out("refused already_locked")
        return 0
    ctx = json.loads((repo_dir / "context.json").read_text("utf-8"))
    if ctx.get("schema") != CONTEXT_SCHEMA:
        out("error context_schema")
        return 1
    exam_id = ctx["exam_id"]
    status, obj, psha = None, None, None
    if decline:
        status = "no_call:refusal"
    else:
        try:
            raw = Path(plan_file).read_bytes()
            obj, errs = plan.validate(raw)
        except OSError:
            raw, obj, errs = None, None, ["missing_plan"]
        if errs:
            st["failures"] = int(st.get("failures", 0)) + 1
            stp.write_text(json.dumps(st), "utf-8")
            out("invalid " + ",".join(errs) + f" failures={st['failures']}")
            if st["failures"] < MAX_FAILURES:
                return 2
            status, obj = "no_call:invalid", None
        else:
            status = "stay_out" if obj["action"] == "stay_out" else "called"
            psha = rec.sha256_bytes(raw)
    doc = {"schema": SCHEMA, "exam_id": exam_id, "status": status, "context_sha256": rec.compact_sha256(ctx),
           "plan": obj, "plan_sha256": psha, "lock_validation_failures": int(st.get("failures", 0)),
           "self_reported": {"locked_utc": rec.iso(now()), "session_id": env.get("CLAUDE_CODE_SESSION_ID"),
                             "note": "self-reported, unverified"}}
    head = git.fetch_main()
    path = f"plans/{exam_id}.json"
    commit = git.commit_one_file(head, path, rec.record_bytes(doc), f"exam plan {exam_id}")
    st["locked"] = True
    stp.write_text(json.dumps(st), "utf-8")
    for _ in range(5):
        ok, _ = git.push(commit, f"claude/exam-{exam_id}")
        if ok:
            out(f"exam_locked {exam_id} status={status}")
            return 0
    out("error exam_push")
    return 1
