"""plan.json validation, S and T, and the display sanitizer (prereg §3 Schema, §7 Text safety; register R-AN-26,
R-AN-27, R-AN-63). FROZEN.

plan.json is a JSON object with exactly: action ∈ {buy, sell, stay_out}; for buy/sell only stop_mult ∈ {1.0, 1.5,
2.0} and target_mult ∈ {1.0, 1.5, 2.0, 3.0} (absent for stay_out); confidence ∈ {low, medium, high}; reason_ar (1-400
code points); key_risks_ar (0-300 code points). Any other key, a missing key, or a C0/C1 control character other than
LF in a string is invalid. Text is stored exactly as received.

S and T: exact integer arithmetic, half up: m = round(10·mult); S_dimes = (D_ref_cents·m_S + 50) // 100; S = S_dimes/10.
"""
import json
import math

ACTIONS = ("buy", "sell", "stay_out")
STOP_MULTS = (1.0, 1.5, 2.0)
TARGET_MULTS = (1.0, 1.5, 2.0, 3.0)
CONFIDENCE = ("low", "medium", "high")
REASON_MAX = 400
RISKS_MAX = 300
TRADE_KEYS = {"action", "stop_mult", "target_mult", "confidence", "reason_ar", "key_risks_ar"}
STAY_KEYS = {"action", "confidence", "reason_ar", "key_risks_ar"}


def _bad_control(s):
    for ch in s:
        o = ord(ch)
        if (o < 0x20 and ch != "\n") or 0x7F <= o <= 0x9F:
            return True
    return False


def _num_in(x, allowed):
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return False
    return math.isfinite(x) and float(x) in allowed


def _no_constant(c):
    raise ValueError(f"non-finite constant {c}")


def _unique_pairs(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def validate(raw_bytes):
    """(plan dict with multiples as floats, None) or (None, [error codes])."""
    errs = []
    try:
        text = raw_bytes.decode("utf-8")
        obj = json.loads(text, parse_constant=_no_constant, object_pairs_hook=_unique_pairs)
    except Exception:  # noqa: BLE001
        return None, ["json"]
    if not isinstance(obj, dict):
        return None, ["not_object"]
    action = obj.get("action")
    if action not in ACTIONS:
        errs.append("action")
    want = STAY_KEYS if action == "stay_out" else TRADE_KEYS
    extra, missing = set(obj) - want, want - set(obj)
    if extra:
        errs.append("extra_keys")
    if missing:
        errs.append("missing_keys")
    if action in ("buy", "sell"):
        if "stop_mult" in obj and not _num_in(obj["stop_mult"], STOP_MULTS):
            errs.append("stop_mult")
        if "target_mult" in obj and not _num_in(obj["target_mult"], TARGET_MULTS):
            errs.append("target_mult")
    if "confidence" in obj and obj["confidence"] not in CONFIDENCE:
        errs.append("confidence")
    r = obj.get("reason_ar")
    if "reason_ar" in obj:
        if not isinstance(r, str) or not (1 <= len(r) <= REASON_MAX):
            errs.append("reason_ar")
        elif _bad_control(r):
            errs.append("control_char")
    k = obj.get("key_risks_ar")
    if "key_risks_ar" in obj:
        if not isinstance(k, str) or not (0 <= len(k) <= RISKS_MAX):
            errs.append("key_risks_ar")
        elif _bad_control(k):
            errs.append("control_char")
    if errs:
        return None, sorted(set(errs))
    plan = dict(obj)
    if action in ("buy", "sell"):
        plan["stop_mult"] = float(obj["stop_mult"])
        plan["target_mult"] = float(obj["target_mult"])
    return plan, None


def side_of(action):
    return {"buy": 1, "sell": -1, "stay_out": 0}[action]


def s_and_t(d_ref_cents, stop_mult, target_mult):
    """(S_dimes, T_dimes, S, T) by exact integer half-up rounding."""
    m_s, m_t = int(round(10 * stop_mult)), int(round(10 * target_mult))
    if m_s not in (10, 15, 20) or m_t not in (10, 15, 20, 30):
        raise ValueError("multiples outside the schema")
    s_d = (d_ref_cents * m_s + 50) // 100
    t_d = (d_ref_cents * m_t + 50) // 100
    return s_d, t_d, s_d / 10, t_d / 10


def derivable(call):
    """R-AN-33 / R-AN-36 step 4: S, T, D_ref, side and the multiples follow from plan and context.d_ref."""
    try:
        plan, ctx = call.get("plan"), call.get("context") or {}
        dref = ctx.get("d_ref") or {}
        if call.get("d_ref") != (ctx.get("d_ref")):
            return False
        if plan is None:
            return all(call.get(k) is None for k in ("stop_mult", "target_mult", "S", "T", "S_dimes", "T_dimes")) \
                and call.get("side") in (None, 0)
        side = side_of(plan["action"])
        if call.get("side") != side:
            return False
        if plan["action"] == "stay_out":
            return all(call.get(k) is None for k in ("stop_mult", "target_mult", "S", "T", "S_dimes", "T_dimes"))
        cents = dref.get("D_ref_cents")
        if not isinstance(cents, int) or cents <= 0 or int(round(dref["D_ref"] * 100)) != cents:
            return False
        if call.get("stop_mult") != plan["stop_mult"] or call.get("target_mult") != plan["target_mult"]:
            return False
        s_d, t_d, s, t = s_and_t(cents, plan["stop_mult"], plan["target_mult"])
        return (call.get("S_dimes"), call.get("T_dimes"), call.get("S"), call.get("T")) == (s_d, t_d, s, t)
    except Exception:  # noqa: BLE001 - any malformed field is "not derivable"
        return False


# ---------------------------------------------------------------- display sanitizer (R-AN-63)
def _allowed(ch):
    o = ord(ch)
    return (0x0621 <= o <= 0x063A or 0x0641 <= o <= 0x064A or 0x0660 <= o <= 0x0669 or "0" <= ch <= "9"
            or ch in " .,\u060c:\u061b%$-\n")


def sanitize(text):
    """Keep only Arabic letters, digits, space, . , ، : ؛ % $ - and newline. Everything else (bidi controls, tatweel,
    diacritics, ؟, brackets, Latin letters, markup) is removed."""
    if not isinstance(text, str):
        return ""
    return "".join(ch for ch in text if _allowed(ch))
