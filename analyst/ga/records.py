"""Session-written records: attempt, call, lesson (register R-AN-25, R-AN-29, R-AN-48). FROZEN.

Every time in `self_reported` is informational only (prereg M1): lock evidence is server side.

§16.18 A14 (R-AN-25, R-AN-90): an attempt written by this build is `analyst-attempt/2`, `/1` plus `wake` right after
`call_id`: {kind, reason, check_utc, td} for a call attempt (kind from the call_id; reason null unless a trigger,
then one of the five conditions; check_utc = entry − 30 min; td null unless a trigger, then the XAUUSD_M30
provenance object), null in a lesson or revision attempt. analyst-merge accepts `/1` (W1 and W2 only) and `/2`.

§16.18 A15 (R-AN-29, R-AN-33): with an `analyst-context/5` context the call stays `analyst-call/1`; its status is
`called` for buy, sell, reverse and add, `stay_out` for stay_out, `review_hold` for hold, `review_close` for close,
or a `no_call:*`; side, the multiples, S, T, S_dimes and T_dimes are set for every `called` call (reverse and add by
plan.side_of from the context's `review`) and are as for stay_out otherwise. A call is not valid when its status does
not follow from its plan by that mapping, or when it has a review status or action without an `analyst-context/5`
context; the merge does not recompute the open set (the judge does).
"""
import os
import shutil
import subprocess

from . import clock, paths, plan, rec
from . import wake as wake_rule

ATTEMPT_SCHEMA = "analyst-attempt/2"
ATTEMPT_SCHEMA_V1 = "analyst-attempt/1"
WAKE_REQ = "XAUUSD_M30"
PROV_KEYS = {"src", "req", "url", "http", "td_code", "status", "reason", "bytes", "sha256", "values_n", "bars",
             "fetched_utc", "tries"}
CALL_SCHEMA = "analyst-call/1"
LESSON_SCHEMA = "analyst-lesson/1"
NOTE = "self-reported, unverified"
CALL_STATUSES = ("called", "stay_out", "no_call:data", "no_call:d_ref", "no_call:invalid", "no_call:refusal",
                 "review_hold", "review_close")
REVIEW_CONTEXT = "analyst-context/5"
PLAN_ACTIONS = {"called": ("buy", "sell", "reverse", "add"), "stay_out": ("stay_out",), "review_hold": ("hold",),
                "review_close": ("close",)}
SESSION_ENV = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "SESSION_ID")
MODEL_ENV = ("CLAUDE_CODE_MODEL", "ANTHROPIC_MODEL", "CLAUDE_MODEL")


def frozen_block(view):
    m = view.read(paths.MANIFEST_PATH) or b""
    sha = rec.sha256_bytes(m)

    def s(p):
        b = view.read(p)
        return rec.sha256_bytes(b) if b is not None else None

    return {"manifest_sha256": sha, "code_sha8": sha[:8], "routine_prompt_sha256": s(paths.ROUTINE_PROMPT_PATH),
            "prompts_sha256": {k: s(v) for k, v in paths.PROMPTS.items()}, "schema_sha256": s(paths.SCHEMA_PATH)}


def _env_first(names, env):
    for n in names:
        v = env.get(n)
        if v:
            return v[:200]
    return None


def cc_version(env=None, runner=subprocess.run):
    exe = shutil.which("claude")
    if not exe:
        return None
    try:
        p = runner([exe, "--version"], capture_output=True, timeout=10)
        out = p.stdout.decode("utf-8", "replace").strip().splitlines()
        return out[0][:100] if p.returncode == 0 and out else None
    except Exception:  # noqa: BLE001
        return None


def self_report(now, key, env=None, version=None):
    env = os.environ if env is None else env
    return {key: rec.iso(now), "session_id": _env_first(SESSION_ENV, env), "model": _env_first(MODEL_ENV, env),
            "cc_version": version, "note": NOTE}


def wake_of(w, reason=None, td_prov=None):
    """The attempt record's `wake` of window w (R-AN-90)."""
    return {"kind": w.kind, "reason": reason, "check_utc": rec.iso(w.check_utc), "td": td_prov}


def attempt(kind, mode, draw_id, n, call_id, head_sha, frozen, selfrep, wake=None):
    """An `analyst-attempt/2` record; a call attempt without an explicit `wake` gets its window's (a trigger's needs
    the reason and the request, so it must be given)."""
    if kind == "call" and wake is None:
        try:
            wake = wake_of(clock.window_of(call_id))
        except ValueError:                       # a call_id that names no window: no wake
            wake = None
    return {"schema": ATTEMPT_SCHEMA, "kind": kind, "mode": mode, "draw_id": draw_id, "n": n, "call_id": call_id,
            "wake": wake, "repo_head_sha": head_sha, "manifest_sha256": frozen["manifest_sha256"],
            "code_sha8": frozen["code_sha8"],
            "routine_prompt_sha256": frozen["routine_prompt_sha256"], "self_reported": selfrep}


def call(ctx, ctx_sha, attempt_n, status, plan_obj, plan_sha, failures, playbook, pins, frozen, selfrep,
         canary_probe=None):
    if status not in CALL_STATUSES:
        raise ValueError(status)
    out = {"schema": CALL_SCHEMA, "call_id": ctx["call_id"], "mode": ctx["mode"], "window": ctx["window"],
           "model_day": ctx["model_day"], "entry_utc": ctx["entry_utc"], "exit_utc": ctx["exit_utc"],
           "deadline_utc": ctx["deadline_utc"], "status": status,
           "detail": ctx["detail"] if status in ("no_call:data", "no_call:d_ref") else None,
           "attempt_n": attempt_n, "decision_utc": ctx["asof_utc"], "context_sha256": ctx_sha, "context": ctx,
           "playbook": playbook, "plan": plan_obj, "plan_sha256": plan_sha,
           "d_ref": ctx.get("d_ref"),
           "side": None, "stop_mult": None, "target_mult": None, "S": None, "T": None, "S_dimes": None,
           "T_dimes": None, "lock_validation_failures": failures,
           "request": {"runner": "claude-code-routine", "model_set": pins.get("model"),
                       "effort_set": pins.get("effort")},
           "frozen": frozen, "repo_head_sha": ctx["repo_head_sha"], "self_reported": selfrep}
    if status == "called":
        cents = ctx["d_ref"]["D_ref_cents"]
        s_d, t_d, s, t = plan.s_and_t(cents, plan_obj["stop_mult"], plan_obj["target_mult"])
        out.update(side=plan.side_of(plan_obj["action"], ctx.get("review")), stop_mult=plan_obj["stop_mult"],
                   target_mult=plan_obj["target_mult"], S=s, T=t, S_dimes=s_d, T_dimes=t_d)
    elif status in ("stay_out", "review_hold", "review_close"):
        out["side"] = 0
    if canary_probe is not None:
        out["canary_probe"] = canary_probe
    return out


def lesson(mode, call_id, status, text_ar, settled_sha, playbook_version, attempt_n, selfrep):
    return {"schema": LESSON_SCHEMA, "call_id": call_id, "mode": mode, "status": status,
            "text_ar": text_ar if status == "ok" else None, "settled_sha256": settled_sha,
            "playbook_version": playbook_version, "attempt_n": attempt_n, "self_reported": selfrep}


def valid_wake(wake, call_id):
    """The shape of a call attempt's `wake` (R-AN-25): kind and check_utc from the call_id, reason and td by kind."""
    if not isinstance(wake, dict) or set(wake) != {"kind", "reason", "check_utc", "td"}:
        return False
    try:
        w = clock.window_of(call_id)
    except ValueError:
        return False
    if wake["kind"] != w.kind or wake["check_utc"] != rec.iso(w.check_utc):
        return False
    if w.kind != "trigger":
        return wake["reason"] is None and wake["td"] is None
    td_ = wake["td"]
    return wake["reason"] in wake_rule.CONDITIONS and isinstance(td_, dict) and set(td_) == PROV_KEYS \
        and td_["req"] == WAKE_REQ and isinstance(td_["bars"], list)


def valid_attempt(doc, path_kind_ok=True):
    keys = {"schema", "kind", "mode", "draw_id", "n", "call_id", "repo_head_sha", "manifest_sha256", "code_sha8",
            "routine_prompt_sha256", "self_reported"}
    if not isinstance(doc, dict) or doc.get("schema") not in (ATTEMPT_SCHEMA, ATTEMPT_SCHEMA_V1):
        return False
    want = keys | ({"wake"} if doc["schema"] == ATTEMPT_SCHEMA else set())
    if set(doc) != want or doc["kind"] not in ("call", "lesson", "revision") or doc["n"] not in (1, 2):
        return False
    if doc["schema"] == ATTEMPT_SCHEMA_V1:
        try:
            return doc["kind"] != "call" or clock.window_type(str(doc["call_id"])) in clock.WINDOWS
        except ValueError:
            return False
    if doc["kind"] != "call":
        return doc["wake"] is None
    return valid_wake(doc["wake"], doc["call_id"])


def valid_call(doc):
    need = {"schema", "call_id", "mode", "window", "model_day", "entry_utc", "exit_utc", "deadline_utc", "status",
            "detail", "attempt_n", "decision_utc", "context_sha256", "context", "playbook", "plan", "plan_sha256",
            "d_ref", "side", "stop_mult", "target_mult", "S", "T", "S_dimes", "T_dimes", "lock_validation_failures",
            "request", "frozen", "repo_head_sha", "self_reported"}
    if not isinstance(doc, dict) or not need <= set(doc) or set(doc) - need - {"canary_probe"}:
        return False
    if doc["schema"] != CALL_SCHEMA or doc["status"] not in CALL_STATUSES or doc["attempt_n"] not in (1, 2):
        return False
    ctx = doc["context"]
    if not isinstance(ctx, dict) or rec.compact_sha256(ctx) != doc["context_sha256"]:
        return False
    if ctx.get("call_id") != doc["call_id"] or ctx.get("asof_utc") != doc["decision_utc"]:
        return False
    if doc["status"] in PLAN_ACTIONS:
        if ctx.get("status") != "ok" or not isinstance(doc["plan"], dict):
            return False
        if doc["plan"].get("action") not in PLAN_ACTIONS[doc["status"]]:
            return False
    action = doc["plan"].get("action") if isinstance(doc["plan"], dict) else None
    if (doc["status"] in ("review_hold", "review_close") or action in ("hold", "close", "reverse", "add")) \
            and ctx.get("schema") != REVIEW_CONTEXT:
        return False
    if doc["status"] in ("no_call:data", "no_call:d_ref") and ctx.get("status") != doc["status"]:
        return False
    return True


def valid_lesson(doc):
    keys = {"schema", "call_id", "mode", "status", "text_ar", "settled_sha256", "playbook_version", "attempt_n",
            "self_reported"}
    if not isinstance(doc, dict) or set(doc) != keys or doc["schema"] != LESSON_SCHEMA:
        return False
    if doc["status"] == "ok":
        t = doc["text_ar"]
        return isinstance(t, str) and 1 <= len(t.split()) <= 120 and not plan._bad_control(t)
    return doc["status"] == "failed" and doc["text_ar"] is None
