"""analyst-track: provisional hourly tracking of one paper trade (prereg R8; register R-AN-65). Display only:
never counted, never judged; final settlement is §4's on the system store.

For each hour h from floor(entry) to the hour holding exit − 1 min, wait until h + 1 h + L, fetch the XAU hour file
(polling every 2 min for up to 30 min more), read the ticks in [entry, exit) in time order. The provisional entry is
the first tick at or after entry (ask for a long, bid for a short); the stop is hit when the exit-side price passes X,
the target when it passes Tg; otherwise the trade exits on time at the last tick. Provisional R = s·(price − P_e)/S,
without costs. Each update writes site/track/<call_id>.json with updated_utc.
"""
import datetime as dt
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

import ga  # noqa: E402,F401
from ga import clock, hours, rec  # noqa: E402

SCHEMA = "analyst-track/1"


def new_state(call):
    return {"schema": SCHEMA, "call_id": call["call_id"], "side": call["side"], "S": call["S"], "T": call["T"],
            "P_e": None, "X": None, "Tg": None, "status": "open", "price": None, "provisional_R": None,
            "exit_utc_ms": None, "hours_done": [], "updated_utc": None}


def update(st, hour, ticks, entry_ms, exit_ms):
    """Apply one hour file's ticks (XAU points, file order) to the tracking state."""
    if st["status"] != "open":
        return st
    side, S, T = st["side"], st["S"], st["T"]
    base = rec.epoch_ms(hour)
    for ms, ask, bid, _av, _bv in ticks:
        t = base + ms
        if t < entry_ms or t >= exit_ms:
            continue
        a, b = ask / 1000, bid / 1000
        if st["P_e"] is None:
            st["P_e"] = a if side > 0 else b
            st["X"] = st["P_e"] - side * S
            st["Tg"] = st["P_e"] + side * T
        px = b if side > 0 else a
        st["price"] = px
        if (side > 0 and px <= st["X"]) or (side < 0 and px >= st["X"]):
            st["status"], st["exit_utc_ms"] = "stop", t
            px = st["X"]
        elif (side > 0 and px >= st["Tg"]) or (side < 0 and px <= st["Tg"]):
            st["status"], st["exit_utc_ms"] = "target", t
            px = st["Tg"]
        st["provisional_R"] = side * (px - st["P_e"]) / S
        if st["status"] != "open":
            break
    st["hours_done"].append(rec.iso(hour))
    return st


def hours_to_track(entry_utc, exit_utc):
    return clock.hours_between(clock.floor_hour(entry_utc), clock.floor_hour(exit_utc - clock.MIN))


def run(call, L_min, write, publish, get=hours.urllib_get, now=None, sleep=None):
    import time
    now = now or (lambda: dt.datetime.now(rec.UTC).replace(microsecond=0))
    sleep = sleep or time.sleep
    entry, exit_ = rec.parse_iso(call["entry_utc"]), rec.parse_iso(call["exit_utc"])
    st = new_state(call)
    hs = hours_to_track(entry, exit_)
    for i, h in enumerate(hs):
        due = h + clock.HOUR + dt.timedelta(minutes=L_min)
        while now() < due:
            sleep(min(60.0, (due - now()).total_seconds()))
        ticks = None
        for _ in range(16):
            code, body = get(hours.url("XAUUSD", h), hours.TIMEOUT_S)
            status, tk = hours.classify(code, body)
            if status == "present":
                ticks = tk
                break
            sleep(120)
        update(st, h, ticks or [], rec.epoch_ms(entry), rec.epoch_ms(exit_))
        if i == len(hs) - 1 and st["status"] == "open" and st["P_e"] is not None:
            st["status"] = "time"
        st["updated_utc"] = rec.iso(now())
        write(st)
        publish()
        if st["status"] != "open":
            break
    return st
