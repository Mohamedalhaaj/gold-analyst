"""plan.json schema (R-AN-26), S and T (R-AN-27), derivability (R-AN-33), the display sanitizer (R-AN-63)."""
import json

import pytest

from ga import plan

GOOD = {"action": "buy", "stop_mult": 1.5, "target_mult": 3.0, "confidence": "low", "reason_ar": "نطاق ضيق",
        "key_risks_ar": ""}


def v(obj):
    return plan.validate(json.dumps(obj, ensure_ascii=False).encode())


def test_valid_plans():
    p, e = v(GOOD)
    assert e is None and p["stop_mult"] == 1.5 and isinstance(p["target_mult"], float)
    p, e = v({**GOOD, "stop_mult": 1, "target_mult": 2})           # 1 and 1.0 are both accepted
    assert e is None and p["stop_mult"] == 1.0 and type(p["stop_mult"]) is float
    p, e = v({"action": "stay_out", "confidence": "high", "reason_ar": "خارج", "key_risks_ar": "لا"})
    assert e is None and "stop_mult" not in p
    assert v({**GOOD, "reason_ar": "ا" * 400})[1] is None
    assert v({**GOOD, "key_risks_ar": "ا" * 300})[1] is None
    assert v({**GOOD, "reason_ar": "سطر\nسطر"})[1] is None


@pytest.mark.parametrize("bad,code", [
    ({**GOOD, "action": "hold"}, "action"),
    ({**GOOD, "stop_mult": 2.5}, "stop_mult"),
    ({**GOOD, "stop_mult": True}, "stop_mult"),
    ({**GOOD, "target_mult": 4.0}, "target_mult"),
    ({**GOOD, "target_mult": "3.0"}, "target_mult"),
    ({**GOOD, "confidence": "certain"}, "confidence"),
    ({**GOOD, "reason_ar": ""}, "reason_ar"),
    ({**GOOD, "reason_ar": "ا" * 401}, "reason_ar"),
    ({**GOOD, "key_risks_ar": "ا" * 301}, "key_risks_ar"),
    ({**GOOD, "reason_ar": "a\tb"}, "control_char"),
    ({**GOOD, "reason_ar": "a\u0085b"}, "control_char"),
    ({**GOOD, "extra": 1}, "extra_keys"),
    ({k: x for k, x in GOOD.items() if k != "key_risks_ar"}, "missing_keys"),
    ({"action": "stay_out", "stop_mult": 1.0, "confidence": "low", "reason_ar": "x", "key_risks_ar": ""},
     "extra_keys"),
    ({k: x for k, x in GOOD.items() if k != "stop_mult"}, "missing_keys"),
])
def test_invalid_plans(bad, code):
    p, e = v(bad)
    assert p is None and code in e


def test_not_json_duplicates_and_nan():
    assert plan.validate(b"{")[1] == ["json"]
    assert plan.validate(b"[]")[1] == ["not_object"]
    assert plan.validate(b'{"action":"buy","action":"sell"}')[1] == ["json"]
    assert plan.validate(b'{"action":"buy","stop_mult":NaN}')[1] == ["json"]
    assert plan.validate(b"\xff\xfe")[1] == ["json"]


def test_s_and_t_integer_half_up():
    # D_ref 23.45 -> cents 2345; x1.5 = 35.175 -> 35.2 (half up on dimes); x3 = 70.35 -> 70.4
    assert plan.s_and_t(2345, 1.5, 3.0) == (352, 704, 35.2, 70.4)
    assert plan.s_and_t(2345, 1.0, 1.0) == (235, 235, 23.5, 23.5)       # 234.5 dimes -> 235
    assert plan.s_and_t(2344, 1.0, 2.0) == (234, 469, 23.4, 46.9)
    assert plan.s_and_t(1, 2.0, 1.5) == (0, 0, 0.0, 0.0)
    for cents in range(1, 6000, 7):
        for ms in (1.0, 1.5, 2.0):
            s_d, _, s, _ = plan.s_and_t(cents, ms, 1.0)
            exact = cents * ms / 10                                      # in dimes
            assert s_d == int(exact + 0.5 + 1e-9) and s == s_d / 10
    with pytest.raises(ValueError):
        plan.s_and_t(1000, 1.2, 1.0)


def call_doc(**kw):
    ctx = {"d_ref": {"D_ref": 23.45, "D_ref_cents": 2345}}
    base = {"plan": dict(GOOD), "context": ctx, "d_ref": ctx["d_ref"], "side": 1, "stop_mult": 1.5,
            "target_mult": 3.0, "S": 35.2, "T": 70.4, "S_dimes": 352, "T_dimes": 704}
    base.update(kw)
    return base


def test_derivable():
    assert plan.derivable(call_doc())
    assert not plan.derivable(call_doc(S=35.1))
    assert not plan.derivable(call_doc(side=-1))
    assert not plan.derivable(call_doc(d_ref={"D_ref": 23.46, "D_ref_cents": 2346}))
    assert not plan.derivable(call_doc(stop_mult=2.0))
    so = {"action": "stay_out", "confidence": "low", "reason_ar": "x", "key_risks_ar": ""}
    assert plan.derivable(call_doc(plan=so, side=0, stop_mult=None, target_mult=None, S=None, T=None, S_dimes=None,
                                   T_dimes=None))
    assert not plan.derivable(call_doc(plan=so, side=0))
    assert plan.derivable(call_doc(plan=None, side=None, stop_mult=None, target_mult=None, S=None, T=None,
                                   S_dimes=None, T_dimes=None))


def test_sanitizer_allowlist():
    s = "شراء‏ عند 4000.5 $، ثم: هدف؛ 3% - <script>alert(1)</script>‮⁦ـَ؟ [x] http://e.co\n٣"
    out = plan.sanitize(s)
    assert "<" not in out and ">" not in out and "/" not in out and "(" not in out
    for ch in ("‏", "‮", "⁦", "ـ", "َ", "؟", "[", "]", "h", "t", "p"):
        assert ch not in out
    assert out.startswith("شراء عند 4000.5 $، ثم: هدف؛ 3% - ")
    assert out.endswith("\n٣")
    assert plan.sanitize(None) == ""
    for ch in "‎‏‪‫‬‭‮⁦⁧⁨⁩":
        assert plan.sanitize(ch) == ""


# ---------------------------------------------------------------- §16.18 A12: stay_out_reason (R-AN-26, R-AN-28)
SO = {"action": "stay_out", "stay_out_reason": "no_setup", "confidence": "low", "reason_ar": "لا إعداد",
      "key_risks_ar": ""}


def v3(obj):
    return plan.validate(json.dumps(obj, ensure_ascii=False).encode(), stay_reason=True)


def test_stay_out_reason_values():
    assert plan.STAY_OUT_REASONS == ("event_risk", "no_setup", "conflicting_signals", "mid_range")
    for r in plan.STAY_OUT_REASONS:
        p, e = v3({**SO, "stay_out_reason": r})
        assert e is None and p["stay_out_reason"] == r
    assert v3(GOOD)[1] is None                                        # a trade carries none


@pytest.mark.parametrize("bad,codes", [
    ({k: x for k, x in SO.items() if k != "stay_out_reason"}, ["missing_keys"]),
    ({**GOOD, "stay_out_reason": "no_setup"}, ["extra_keys"]),
    ({**SO, "stay_out_reason": "boredom"}, ["stay_out_reason"]),
    ({**SO, "stay_out_reason": "No_Setup"}, ["stay_out_reason"]),
    ({**SO, "stay_out_reason": ["no_setup"]}, ["stay_out_reason"]),
    ({**SO, "stay_out_reason": None}, ["stay_out_reason"]),
    ({**SO, "stay_out_reason": 1}, ["stay_out_reason"]),
    ({**SO, "stop_mult": 1.0}, ["extra_keys"]),
    ({**SO, "stay_out_reason": "x", "confidence": "max"}, ["confidence", "stay_out_reason"]),
])
def test_stay_out_reason_failures(bad, codes):
    p, e = v3(bad)
    assert p is None and e == codes


def test_a_context_2_call_keeps_the_old_key_set():
    old = {k: x for k, x in SO.items() if k != "stay_out_reason"}
    assert v(old)[1] is None and v(SO)[1] == ["extra_keys"]
    assert plan.validate(json.dumps(SO).encode())[1] == ["extra_keys"]
    assert plan.stay_reason_applies("analyst-context/3") and not plan.stay_reason_applies("analyst-context/2")
    assert not plan.stay_reason_applies(None)


def test_plan_schema_file_is_analyst_plan_2():
    from pathlib import Path
    doc = json.loads((Path(__file__).resolve().parents[1] / "analyst/schema/plan.schema.json").read_text("utf-8"))
    assert doc["$id"] == "analyst-plan/2"
    trade, stay = doc["oneOf"]
    assert "stay_out_reason" not in trade["properties"] and trade["additionalProperties"] is False
    assert stay["properties"]["stay_out_reason"] == {"enum": list(plan.STAY_OUT_REASONS)}
    assert set(stay["required"]) == set(plan.STAY_KEYS_A12) == {"action", "stay_out_reason", "confidence",
                                                                "reason_ar", "key_risks_ar"}
    assert set(stay["properties"]) == set(stay["required"]) and stay["additionalProperties"] is False
