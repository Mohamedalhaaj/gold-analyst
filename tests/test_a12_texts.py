"""§16.18 A12: the system prompt and playbook v0 (R-AN-50's format rules, the A12 role and stay_out_reason lines)."""
from pathlib import Path

from ga import plan

ROOT = Path(__file__).resolve().parents[1]


def test_playbook_v0_keeps_the_format_rules():
    t = (ROOT / "analyst/playbooks/v0.md").read_text("utf-8")
    assert t.split("\n", 1)[0] == "# playbook v0" and t.endswith("\n")
    assert len(t.split()) == 1196 <= 1200
    assert "http" not in t and "```" not in t
    assert not any((ord(c) < 0x20 and c != "\n") or 0x7F <= ord(c) <= 0x9F for c in t)
    a = t.index("## حقائق مثبتة غير اتجاهية / Validated non-directional facts")
    b = t.index("## إطار عمل: فرضيات غير مثبتة / Working framework: hypotheses, not proven")
    assert a < b and len(t[a:b].split()) == 912


def test_system_prompt_keeps_its_opening_and_names_every_stay_out_reason():
    t = " ".join((ROOT / "analyst/prompts/system.md").read_text("utf-8").split())   # §16.18 A15 rewrapped it
    assert "You are an analyst under test." in t and "Nobody has shown that you have an edge" in t
    assert "Judge buy and sell by the same standard." in t
    for r in plan.STAY_OUT_REASONS:
        assert f'"{r}"' in t
