"""The wake_freq replay: wake_freq.py (the A14 measurement, unchanged) on the recorded bars, against this reference.

  1. legacy_wakes() (wake_freq.simulate in plain Python) must equal wake_freq.simulate exactly;
  2. replay() (the A14 rule, the watcher's expected output) is set beside wake_freq.simulate with the A14 check
     times and no trades, and every difference must have a recorded cause: `move` consumed once a day (R-AN-89
     step 6; wake_freq.py lets `big_move` fire again) and the later effects of that one extra wake on the same day.

usage: <python with pandas> wake_freq_replay.py <dir of wake_freq.py> <gold-forward-watch root>
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, sys.argv[1])
sys.argv = [sys.argv[0], sys.argv[2]]  # wake_freq.py reads its root from argv[1] at import
import pandas as pd  # noqa: E402
import ref_wakes as ref  # noqa: E402
import wake_freq as wf  # noqa: E402

bars = ref.load_bars(HERE / "fixtures" / "bars_recorded.json")
df = pd.DataFrame([{"o": o, "h": h, "l": l, "c": c, "spread": 0.0} for _, o, h, l, c in bars],
                  index=pd.DatetimeIndex([b[0] for b in bars]))
REGIMES = [  # (first day, last day, SCHED as check times, as in the DST table)
    ("2026-09-29", "2026-10-23", {"W1": "06:30", "W1b": "08:30", "W2": "13:00", "W2b": "15:00"}),
    ("2026-10-26", "2026-10-26", {"W1": "07:30", "W1b": "09:30", "W2": "13:00", "W2b": "15:00"}),
]
NAMES = {"big_move": "move"}
ok = True
for check_to in ("16:30", "20:30"):
    for a, b, sched in REGIMES:
        wf.SCHED, wf.CHECK_TO = sched, check_to
        r, _ = wf.simulate(df, p_enter=0.0, cap=6, seed=0)
        theirs = [(t.date().isoformat(), x.strftime("%Y-%m-%dT%H:%M:%SZ"), why)
                  for t, x, why in zip(r["tday"], r["t"], r["why"]) if a <= t.date().isoformat() <= b]
        mine = [w for w in ref.legacy_wakes(bars, sched, check_to) if a <= w[0] <= b]
        same = theirs == mine
        ok &= same
        print(f"[1] CHECK_TO {check_to} {a}..{b}: wake_freq.simulate {len(theirs)} wakes, legacy_wakes {len(mine)}:",
              "EQUAL" if same else "DIFFERENT")
        if check_to != "16:30":
            continue
        days = [d for d in pd.date_range(a, b).date if d.weekday() < 5]
        exp = ref.replay(bars, days, outcome=lambda cid: False)
        spec = {(d, c, dec.split(":", 1)[1] if dec.startswith("trigger:") else dec)
                for d, v in exp.items() for c, dec in v["checks"] if dec not in ("none", "trade_open", "day_cap")}
        spec = {(d, c, {"fixed": None}.get(w, w)) for d, c, w in spec}
        legacy = {(d, c, NAMES.get(w, w)) for d, c, w in theirs}
        legacy = {(d, c, None if w in ("W1", "W2") else w) for d, c, w in legacy}
        only_wf, only_spec = sorted(legacy - spec), sorted(spec - legacy)
        diff_days = sorted({x[0] for x in only_wf + only_spec})
        explained = True
        for d in diff_days:
            first = min([x for x in only_wf + only_spec if x[0] == d], key=lambda x: x[1])
            moves_before = [x for x in theirs if x[0] == d and x[2] == "big_move" and x[1] < first[1]]
            cause = "move fired again in wake_freq.py" if (first in only_wf and first[2] == "move" and moves_before) \
                else None
            if cause is None and first in only_wf:
                # an extra wake earlier in wake_freq.py moves g(T) and the cap for the rest of the day
                cause = "after an earlier move-again wake" if any(
                    x[2] == "big_move" for x in theirs if x[0] == d and x[1] < first[1]
                    and sum(y[2] == "big_move" for y in theirs if y[0] == d and y[1] <= x[1]) > 1) else None
            if cause is None and first in only_spec:
                cause = "after an earlier move-again wake" if sum(x[2] == "big_move" for x in theirs
                                                                  if x[0] == d and x[1] < first[1]) > 1 else None
            explained &= cause is not None
            print(f"    {d}: first difference {first} ({'wake_freq only' if first in only_wf else 'A14 only'}) ->",
                  cause or "UNEXPLAINED")
        print(f"[2] {a}..{b}: wake_freq.simulate {len(legacy)} wakes, A14 rule {len(spec)} wakes, same on "
              f"{len(days) - len(diff_days)} of {len(days)} days;", "all differences explained" if explained
              else "UNEXPLAINED differences")
        ok &= explained
print("RESULT", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
