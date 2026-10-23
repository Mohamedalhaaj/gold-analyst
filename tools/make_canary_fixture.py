"""Write analyst/tests/fixtures/canary_settled.json: the frozen SYNTHETIC settled record that the canary lesson
routine is shown (register R-AN-71). Its prices come from a synthetic path, not from the market; it is marked
"synthetic": true. Build-time tool."""
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analyst"))
sys.path.insert(0, str(ROOT / "tests"))
import ga  # noqa: E402,F401
from ga import clock, rec, settle, store  # noqa: E402
from synth import day_records  # noqa: E402

CALL_ID = "2026-10-16_W1"


def build():
    w = clock.window_of(CALL_ID)
    m = store.Minutes()
    for d in (w.day - dt.timedelta(days=1), w.day):
        for side in store.SIDES:
            m.add_day(d, side, [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in day_records(d, side)])
    S_d, T_d = 300, 400
    out, aux = settle.trade(m, w, 1, S_d, T_d)
    opp, _ = settle.trade(m, w, -1, S_d, T_d)
    plan = {"action": "buy", "stop_mult": 1.5, "target_mult": 2.0, "confidence": "low",
            "reason_ar": "سجل اصطناعي للتجربة التمهيدية فقط", "key_risks_ar": "ليس سعرا حقيقيا"}
    return {"schema": "analyst-settled/1", "synthetic": True, "call_id": CALL_ID, "mode": "canary",
            "window": w.name, "model_day": w.day.isoformat(), "class": "shakedown", "action": "buy", "side": 1,
            "stop_mult": 1.5, "target_mult": 2.0, "D_ref": 20.0, "S": S_d / 10, "T": T_d / 10, "S_dimes": S_d,
            "T_dimes": T_d, "entry_utc": rec.iso(w.entry_utc), "exit_utc": rec.iso(w.exit_utc), "outcome": out,
            "opposite": opp, "path_summary": settle.path_summary(m, w, 1, out, aux), "call": plan,
            "context": None, "inputs": None, "call_sha256": None, "terminal_sha256": None, "context_sha256": None,
            "settled_utc": "2026-10-17T00:00:00Z", "run_id": None, "head_sha": None}


if __name__ == "__main__":
    p = ROOT / "analyst" / "tests" / "fixtures" / "canary_settled.json"
    p.write_bytes(rec.record_bytes(build()))
    print(p)
