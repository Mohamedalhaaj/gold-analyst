"""§16.18 A15 (trade management and learning in the canary): the open set and the `review` block (R-AN-92), the plan
and the lock's options (R-AN-26..R-AN-29), managed exits and the settled record's new fields (R-AN-93), overlap
weights and clusters (R-AN-94), canary settlement, memory, lessons and revision (R-AN-95), `start` and the closer
without `trade_open` (R-AN-39, R-AN-90), and a replay of the 2026-10-29 canary day, where W1 and W1b become reviews
of the T0600 sell."""
import csv
import datetime as dt
import io
import json
from pathlib import Path

import pytest

from ga import clock, memory, paths, plan, rec, records, review, settle, settle_run, state, store, td, weekly
from ga.repo import DictView, GitView
from harness import PLAN, Harness
from synth import store_files

UTC = rec.UTC
P = "analyst/canary/"


def u(*a):
    return dt.datetime(*a, tzinfo=UTC)


# ---------------------------------------------------------------- the open set (R-AN-92)
def entry(cid, status, action=None, side=None, names=(), S=20.0, T=40.0):
    w = clock.window_of(cid)
    return {"call_id": cid, "entry_utc": w.entry_utc, "exit_utc": w.exit_utc, "status": status, "action": action,
            "side": side, "S": S, "T": T, "stop_mult": 1.0, "target_mult": 2.0, "names": list(names)}


D = "2026-11-10"          # Tuesday, GMT/EST: W1 08:00, W1b 10:00, T1100 11:30, W2 14:30, W2b 16:30 (entries, UTC)


def test_open_sets_close_add_reverse_and_no_decision():
    es = [entry(f"{D}_W1", "called", "buy", 1),
          entry(f"{D}_W1b", "called", "add", 1, [f"{D}_W1"]),
          entry(f"{D}_T1100", "review_close", "close", 0, [f"{D}_W1", f"{D}_W1b"]),
          entry(f"{D}_W2", "called", "sell", -1),
          entry(f"{D}_W2b", "called", "reverse", 1, [f"{D}_W2"])]
    o, eff, closed = review.open_sets(es)
    assert [t["call_id"] for t in o[f"{D}_W1b"]] == [f"{D}_W1"]
    assert [t["call_id"] for t in o[f"{D}_T1100"]] == [f"{D}_W1", f"{D}_W1b"]          # two open: hold or close
    assert o[f"{D}_W2"] == [] and [t["call_id"] for t in o[f"{D}_W2b"]] == [f"{D}_W2"]
    assert closed == {f"{D}_W1": f"{D}_T1100", f"{D}_W1b": f"{D}_T1100", f"{D}_W2": f"{D}_W2b"}
    assert eff[f"{D}_W1"] == u(2026, 11, 10, 11, 30) and eff[f"{D}_W2b"] == u(2026, 11, 10, 20, 30)
    assert review.reviews_of(es, o)[f"{D}_W1"] == [es[1], es[2]]
    # a review ending no_call, missed or skipped changes nothing; an unnamed trade is never closed
    es2 = [entry(f"{D}_W1", "called", "buy", 1), entry(f"{D}_W1b", "no_call:invalid"),
           entry(f"{D}_T1100", "review_close", "close", 0, []), entry(f"{D}_W2", None)]
    o2, _eff, closed2 = review.open_sets(es2)
    assert closed2 == {} and [t["call_id"] for t in o2[f"{D}_T1100"]] == [f"{D}_W1"]
    assert o2[f"{D}_W2"] == []                         # W1 exits 12:00 < 14:30
    # an entry equal to eff_exit is not a review of the trade (A14-D4)
    t0600 = entry("2026-10-29_T0600", "called", "sell", -1)
    w1b = entry("2026-10-29_W1b", None)
    t1000 = entry("2026-10-29_T1000", None)
    assert review.open_sets([t0600, w1b, t1000])[0]["2026-10-29_T1000"] == []           # 10:30 = 10:30
    assert review.options(0) == ("buy", "sell", "stay_out") and review.options(2) == ("hold", "close")


def test_the_review_block_and_the_m30r_bars():
    w = entry(f"{D}_W1", "called", "buy", 1, S=10.0, T=20.0)
    bars = [(u(2026, 11, 10, 8, 0), 4000.0, 4004.0, 3995.0, 4003.0),
            (u(2026, 11, 10, 8, 30), 4003.0, 4021.0, 4001.0, 4012.0)]
    b = review.block([w], u(2026, 11, 10, 10, 0), bars)
    o = b["open"][0]
    assert list(o) == list(review.OPEN_KEYS) and b["options"] == ["hold", "close", "reverse", "add"]
    assert (b["ref_px"], b["ref_utc"]) == (4012.0, "2026-11-10T09:00:00Z")
    assert (o["side"], o["entry_px"], o["move_usd"], o["move_r"]) == ("buy", 4000.0, 12.0, 1.2)
    assert (o["high_since_entry"], o["low_since_entry"]) == (4021.0, 3995.0)
    assert (o["stop_touched"], o["target_touched"], o["minutes_left"]) == (False, True, 120)
    s = dict(w, side=-1)
    o = review.block([s], u(2026, 11, 10, 10, 0), bars)["open"][0]
    assert (o["side"], o["move_usd"], o["stop_touched"], o["target_touched"]) == ("sell", -12.0, True, False)
    n = review.block([w], u(2026, 11, 10, 10, 0), [])
    assert n["ref_px"] is None and n["open"][0]["entry_px"] is None and n["open"][0]["stop_touched"] is None
    late = review.block([w], u(2026, 11, 10, 10, 0), bars[1:])["open"][0]           # no bar at entry_utc
    assert late["entry_px"] is None and late["move_usd"] is None and late["high_since_entry"] == 4021.0
    asof = u(2026, 11, 10, 9, 22, 5)
    assert review.needs_request([w], asof) and not review.needs_request([w], u(2026, 11, 10, 8, 29))
    assert review.request_range([w], asof) == (u(2026, 11, 10, 8), u(2026, 11, 10, 9))
    vals = bars + [(u(2026, 11, 10, 9, 0), 1, 1, 1, 1)]
    assert review.used_bars(vals, u(2026, 11, 10, 8), asof) == bars                     # 09:00 + 30 > asof


def test_weights_and_clusters():
    t = lambda h1, m1, h2, m2: (u(2026, 11, 10, h1, m1), u(2026, 11, 10, h2, m2))   # noqa: E731
    tr = [("a",) + t(8, 0, 12, 0), ("b",) + t(10, 0, 14, 0), ("c",) + t(13, 0, 17, 0), ("d",) + t(17, 0, 21, 0)]
    assert review.weights(tr) == {"a": (0.5, "a"), "b": (0.5, "a"), "c": (0.5, "a"), "d": (1.0, "d")}   # a chain
    assert review.weights([("x",) + t(8, 0, 10, 0), ("y",) + t(10, 0, 14, 0)]) == {"x": (1.0, "x"), "y": (1.0, "y")}


# ---------------------------------------------------------------- the plan and the call (R-AN-26..R-AN-29)
HOLD = {"action": "hold", "confidence": "low", "reason_ar": "انتظار", "key_risks_ar": ""}
CLOSE = dict(HOLD, action="close")
ADD = dict(PLAN, action="add")
REV = dict(PLAN, action="reverse")
SELL = dict(PLAN, action="sell")


def v3(obj):
    return plan.validate(json.dumps(obj, ensure_ascii=False).encode(), stay_reason=True, plan3=True)


def test_plan_3_keys_and_the_older_schema():
    assert v3(HOLD)[1] is None and v3(CLOSE)[1] is None and v3(ADD)[1] is None and v3(REV)[1] is None
    assert v3(dict(HOLD, stop_mult=1.0))[1] == ["extra_keys"]
    assert v3({k: x for k, x in ADD.items() if k != "stop_mult"})[1] == ["missing_keys"]
    assert v3(dict(ADD, target_mult=2.5))[1] == ["target_mult"]
    assert v3(dict(HOLD, stay_out_reason="no_setup"))[1] == ["extra_keys"]
    assert "action" in plan.validate(json.dumps(HOLD).encode(), stay_reason=True)[1]     # analyst-plan/2
    assert plan.plan3_applies("analyst-context/5") and not plan.plan3_applies("analyst-context/4")
    assert plan.stay_reason_applies("analyst-context/5")
    doc = json.loads((Path(__file__).resolve().parents[1] / paths.SCHEMA_PATH).read_text("utf-8"))
    assert doc["$id"] == "analyst-plan/3"
    trade, stay, rv = doc["oneOf"]
    assert trade["properties"]["action"]["enum"] == ["buy", "sell", "reverse", "add"]
    assert rv["properties"]["action"]["enum"] == ["hold", "close"] and set(rv["required"]) == plan.STAY_KEYS


def ctx5(review_block, cents=2000):
    return {"schema": "analyst-context/5", "call_id": f"{D}_W1b", "mode": "canary", "window": "W1b",
            "model_day": D, "entry_utc": "2026-11-10T10:00:00Z", "exit_utc": "2026-11-10T14:00:00Z",
            "deadline_utc": "2026-11-10T09:45:00Z", "asof_utc": "2026-11-10T09:33:00Z", "repo_head_sha": "a" * 40,
            "detail": None, "status": "ok", "review": review_block,
            "d_ref": {"D_ref": cents / 100, "D_ref_cents": cents}}


def call_of(ctx, status, obj):
    return records.call(ctx, rec.compact_sha256(ctx), 1, status, obj, "p" * 64, 0, {"version": 0, "sha256": "s"},
                        {}, {"manifest_sha256": "m", "code_sha8": "c", "routine_prompt_sha256": "r"}, {})


def test_call_sides_statuses_and_validity():
    one = review.block([entry(f"{D}_W1", "called", "sell", -1)], u(2026, 11, 10, 10), [])
    c = call_of(ctx5(one), "called", REV)
    assert (c["side"], c["S"], c["T"], c["S_dimes"]) == (1, 30.0, 40.0, 300) and records.valid_call(c)
    assert plan.derivable(c)
    a = call_of(ctx5(one), "called", ADD)
    assert a["side"] == -1 and plan.derivable(a) and records.valid_call(a)
    h = call_of(ctx5(one), "review_hold", HOLD)
    assert (h["side"], h["S"]) == (0, None) and records.valid_call(h) and plan.derivable(h)
    assert records.valid_call(call_of(ctx5(one), "review_close", CLOSE))
    assert not records.valid_call(dict(h, status="stay_out"))                 # status does not follow from the plan
    old = dict(ctx5(one), schema="analyst-context/4")
    assert not records.valid_call(call_of(old, "review_hold", HOLD))          # a review status needs /5
    two = review.block([entry(f"{D}_W1", "called", "sell", -1), entry(f"{D}_W1b", "called", "add", -1)],
                       u(2026, 11, 10, 11, 30), [])
    with pytest.raises(ValueError):
        plan.side_of("reverse", two)
    bad = dict(a, side=1)
    assert not plan.derivable(bad)
    assert plan.STATUS_OF == {"buy": "called", "sell": "called", "reverse": "called", "add": "called",
                              "stay_out": "stay_out", "hold": "review_hold", "close": "review_close"}


# ---------------------------------------------------------------- managed exits (R-AN-93)
W = clock.Window(dt.date(2026, 11, 10), "W1")
T0 = int(W.entry_utc.timestamp())


def flat(n=240, mid=4000000, spread=200):
    m = store.Minutes()
    for i in range(-10, n):
        t = T0 + 60 * i
        m.side["BID"][t] = (mid - spread // 2,) * 4 + (1.0,)
        m.side["ASK"][t] = (mid + spread // 2,) * 4 + (1.0,)
    return m


def test_managed_exit_cases_and_x_end():
    mt = T0 + 30 * 60
    out, aux = settle.trade(flat(), W, 1, 200, 400, mt)
    assert (out["reason"], out["fill"], out["P_x"], out["exit_minute_utc"]) == ("managed", "open", 3999.9,
                                                                                "2026-11-10T08:30:00Z")
    assert aux["x_end"] == mt
    m = flat()
    m.side["BID"][mt] = (3970000, 3990000, 3960000, 3985000, 1.0)            # minute m itself is not tested
    assert settle.trade(m, W, 1, 200, 400, mt)[0]["P_x"] == 3970.0
    m = flat()
    m.side["BID"][T0 + 600] = (3999900, 4000000, 3980000, 3990000, 1.0)      # stop before m: as without a review
    out, aux = settle.trade(m, W, 1, 200, 400, mt)
    assert (out["reason"], out["fill"], aux["x_end"]) == ("stop", "level", T0 + 660)
    m = flat()
    m.side["BID"][T0 + 300] = (3970000, 3990000, 3960000, 3985000, 1.0)      # stop at an open
    assert settle.trade(m, W, 1, 200, 400, mt)[1]["x_end"] == T0 + 300
    m = flat()
    del m.side["BID"][mt]
    out, aux = settle.trade(m, W, 1, 200, 400, mt)                           # case 2: the latest quoted close
    assert (out["reason"], out["fill"], out["exit_minute_utc"], aux["x_end"]) == ("managed", "close",
                                                                                   "2026-11-10T08:29:00Z", mt)
    m = flat()
    for i in range(0, 11):
        del m.side["BID"][T0 + 60 * i]
    out, aux = settle.trade(m, W, 1, 200, 400, T0 + 600)                     # case 3: no quote at all, at X
    assert (out["reason"], out["fill"], out["exit_minute_utc"], out["P_x"]) == ("gap", "level", None, 3980.1)
    assert aux["x_end"] == T0 + 600
    out, aux = settle.trade(flat(), W, 1, 200, 400)                          # a time exit ends at exit_utc
    assert aux["x_end"] == int(W.exit_utc.timestamp())
    call = {"side": 1, "S": 20.0, "S_dimes": 200, "T_dimes": 400}
    r = settle.outcomes(flat(), W, call, {}, u(2026, 11, 10, 8, 30))
    assert r["opposite"]["reason"] == "managed" and r["unmanaged"]["reason"] == "time"
    assert r["x_end"] == u(2026, 11, 10, 8, 30) and r["path_summary"]["exit_after_min"] == 30
    assert settle.outcomes(flat(), W, call, {})["unmanaged"] is None


# ---------------------------------------------------------------- one canary day settled as a unit (R-AN-94, 95)
class FakeWS:
    def __init__(self, files):
        self.view = DictView(files)

    def write(self, path, data):
        self.view.files[path] = data


def call_file(cid, action, side, names=()):
    pl = {"action": action, "confidence": "low", "reason_ar": f"سبب {action}", "key_risks_ar": ""}
    trade = action in review.TRADE_ACTIONS
    if trade:
        pl.update(stop_mult=1.0, target_mult=2.0)
    return {"call_id": cid, "plan": pl, "side": side, "S": 20.0 if trade else None, "T": 40.0 if trade else None,
            "S_dimes": 200 if trade else None, "T_dimes": 400 if trade else None,
            "stop_mult": 1.0 if trade else None, "target_mult": 2.0 if trade else None, "d_ref": {"D_ref": 20.0},
            "context": {"review": {"open": [{"call_id": n} for n in names]} if names else None},
            "context_sha256": "x"}


def day_files(a15="2026-11-10"):
    files = {paths.MODE_PATH: json.dumps({"mode": "canary", "canary_from": D, "a14_from": D, "a15_from": a15}),
             f"{P}witness/000001.json": json.dumps({"seq": 1, "kind": "attempt", "id": f"{D}_T1100",
                                                     "status": "accepted",
                                                     "path": f"{P}attempts/{D}_T1100_1.json"})}
    plan_ = [("W1", "called", "buy", 1, ()), ("W1b", "called", "add", 1, ("W1",)),
             ("T1100", "review_close", "close", 0, ("W1", "W1b")), ("W2", "called", "sell", -1, ()),
             ("W2b", "called", "reverse", 1, ("W2",))]
    for tag, status, action, side, names in plan_:
        cid = f"{D}_{tag}"
        files[f"{P}calls/{cid}.json"] = json.dumps(call_file(cid, action, side, [f"{D}_{n}" for n in names]))
        files[f"{P}terminal/{cid}.json"] = json.dumps({"call_id": cid, "status": status, "call": {"sha256": "c"}})
    files.update(store_files([dt.date(2026, 11, 9), dt.date(2026, 11, 10)]))
    return files


def test_a_canary_day_is_settled_as_one_unit():
    files = day_files()
    ws = FakeWS(dict(files))
    assert settle_run.settle_windows(ws, u(2026, 11, 10, 21, 0), 1, "h", mode="canary") == []   # before day end
    done = settle_run.settle_windows(ws, u(2026, 11, 11, 9, 0), 1, "h", mode="canary")
    assert [w.tag for w in done] == ["W1", "W1b", "T1100", "W2", "W2b"]
    s = {tag: ws.view.read_json(f"{P}settled/{D}_{tag}.json") for tag in ("W1", "W1b", "W2", "W2b")}
    assert not ws.view.exists(f"{P}settled/{D}_T1100.json") and ws.view.list("analyst/settled/") == []
    a = s["W1"]
    assert (a["schema"], a["mode"], a["class"]) == ("analyst-settled/2", "canary", "canary")
    assert (a["closed_by"], a["managed_utc"], a["x_end_utc"]) == (f"{D}_T1100", "2026-11-10T11:30:00Z",
                                                                  "2026-11-10T11:30:00Z")
    assert (a["outcome"]["reason"], a["outcome"]["fill"], a["weight"], a["cluster"]) == ("managed", "open", 0.5,
                                                                                         f"{D}_W1")
    assert a["unmanaged"]["reason"] == "time" and a["opposite"]["reason"] == "managed"
    assert [(r["call_id"], r["status"], r["action"]) for r in a["reviews"]] == [
        (f"{D}_W1b", "called", "add"), (f"{D}_T1100", "review_close", "close")]
    assert a["reviews"][1]["reason_ar"] == "سبب close"
    b = s["W1b"]
    assert (b["action"], b["side"], b["closed_by"], b["weight"], b["cluster"]) == ("add", 1, f"{D}_T1100", 0.5,
                                                                                   f"{D}_W1")
    c, d = s["W2"], s["W2b"]
    assert (c["closed_by"], c["x_end_utc"], c["weight"], c["cluster"]) == (f"{D}_W2b", "2026-11-10T16:30:00Z", 1.0,
                                                                           f"{D}_W2")
    assert (d["action"], d["side"], d["closed_by"], d["managed_utc"], d["unmanaged"]) == ("reverse", 1, None, None,
                                                                                         None)
    assert (d["x_end_utc"], d["weight"], d["cluster"], d["reviews"]) == ("2026-11-10T20:30:00Z", 1.0, f"{D}_W2b", [])
    assert sorted(ws.view.list(f"{P}twins/")) == sorted(f"{P}twins/{D}_{t}.json"
                                                         for t in ("W1", "W1b", "T1100", "W2", "W2b"))
    assert ws.view.read_json(f"{P}twins/{D}_T1100.json")["terminal_status"] == "review_close"
    before = dict(ws.view.files)
    settle_run.settle_windows(ws, u(2026, 11, 11, 10, 0), 2, "h", mode="canary")             # create-only
    assert ws.view.files == before


def test_the_day_unit_waits_for_every_terminal_and_every_call_file():
    files = day_files()
    del files[f"{P}terminal/{D}_W2b.json"]
    ws = FakeWS(files)
    assert settle_run.settle_windows(ws, u(2026, 11, 11, 9, 0), 1, "h", mode="canary") == []
    assert ws.view.list(f"{P}settled/") == [] and ws.view.list(f"{P}twins/") == []
    files = day_files()
    del files[f"{P}calls/{D}_W2.json"]
    log = []
    ws = FakeWS(files)
    assert settle_run.settle_windows(ws, u(2026, 11, 11, 9, 0), 1, "h", log.append, mode="canary") == []
    assert log == [f"call_file_missing {D}_W2"] and ws.view.list(f"{P}settled/") == []
    ws = FakeWS(day_files(a15=None))                          # no a15_from: the canary is never settled
    assert settle_run.settle_windows(ws, u(2026, 11, 11, 9, 0), 1, "h", mode="canary") == []
    ws = FakeWS(day_files(a15="2026-11-11"))                  # a canary day before a15_from is never settled
    settle_run.settle_windows(ws, u(2026, 11, 12, 9, 0), 1, "h", mode="canary")
    assert ws.view.list(f"{P}settled/") == []


# ---------------------------------------------------------------- weighted statistics (R-AN-94, R-AN-56, R-AN-58)
def weighted_view():
    files = {paths.START_PATH: json.dumps({"first_day": "2026-11-16"})}
    rows = [("W1", 1.0, 0.5, "W1"), ("W1b", -0.5, 0.5, "W1"), ("W2", 0.4, 1.0, "W2")]
    for tag, y, w, cl in rows:
        cid = f"2026-11-16_{tag}"
        files[f"analyst/terminal/{cid}.json"] = json.dumps({"call_id": cid, "status": "called", "entry_utc": "x",
                                                             "call": {"sha256": "c", "lock_utc": "t"},
                                                             "attempts": [{}]})
        files[f"analyst/calls/{cid}.json"] = json.dumps({"plan": {"action": "buy", "confidence": "low"}, "side": 1,
                                                         "S": 20.0, "T": 40.0, "d_ref": {"D_ref": 20.0},
                                                         "self_reported": {"session_id": f"s-{cid}"}})
        files[f"analyst/settled/{cid}.json"] = json.dumps({
            "side": 1, "weight": w, "cluster": f"2026-11-16_{cl}", "x_end_utc": "2026-11-16T12:00:00Z",
            "closed_by": None, "outcome": {"y": y, "R": y, "R_stress": y, "net_usd": 20 * y,
                                           "stress_net_usd": 20 * y - 0.25, "cost_usd": 0.3}})
        files[f"analyst/twins/{cid}.json"] = json.dumps({"status": "final", "d_W": 0.0})
    return DictView(files)


def test_weighted_count_cluster_e_process_and_ledger():
    view = weighted_view()
    ep = state._eproc()
    chk = state.promotion(state.collect(view, dt.date(2026, 11, 16), {})[0], dt.date(2026, 11, 16),
                          dt.date(2026, 11, 16), None)
    assert chk["n"] == 2.0 and chk["E"] == ep.e_value([0.25, 0.4])
    assert chk["P5"] is (0.5 * 1.0 + 0.5 * -0.5 + 0.4 - 0.4 > 0)          # the best cluster is W2 (0.4 > 0.25)
    st = state.run_state(view, [dt.date(2026, 11, 16)], u(2026, 11, 17, 9), 1, "h", None,
                         lambda p, b: view.files.__setitem__(p, b))
    assert (st["schema"], st["n"], st["n_trades"], st["n_clusters"]) == ("analyst-state/2", 2.0, 3, 2)
    assert st["E"] == ep.e_value([0.25, 0.4]) and isinstance(st["n"], float)
    led = list(csv.DictReader(io.StringIO(state.ledger_csv(view, u(2026, 11, 17, 9)).decode())))
    assert list(led[0]) == state.LEDGER_COLS
    assert [(r["weight"], r["cluster"], r["n_w"]) for r in led] == [
        ("0.5", "2026-11-16_W1", "0.5"), ("0.5", "2026-11-16_W1", "1.0"), ("1.0", "2026-11-16_W2", "2.0")]
    assert led[0]["E"] == "" and led[1]["E"] == repr(ep.e_value([0.25]))   # on the cluster's last row only
    assert led[2]["E"] == repr(ep.e_value([0.25, 0.4]))
    i = state.LEDGER_COLS.index
    assert i("weight") == i("y") + 1 and i("closed_by") == i("y") + 4 and i("n_w") == i("trade_no") + 1


# ---------------------------------------------------------------- canary memory and revision (R-AN-95, R-AN-50)
def test_canary_memory_playbook_and_week():
    fx = "2026-10-20_W1"
    files = {paths.MODE_PATH: json.dumps({"mode": "canary", "canary_from": "2026-11-09", "a14_from": "2026-11-09",
                                          "a15_from": "2026-11-10"}),
             f"{P}settled/{D}_W1.json": json.dumps({"call_id": f"{D}_W1", "action": "reverse", "side": -1,
                                                    "stop_mult": 1.0,
                                                    "target_mult": 2.0, "S": 20.0, "T": 40.0, "class": "canary",
                                                    "outcome": {"reason": "managed", "R": 0.5, "R_stress": 0.4}}),
             f"{P}lessons/{D}_W1.json": json.dumps({"call_id": f"{D}_W1", "status": "ok", "text_ar": "درس"}),
             f"{P}lessons/{fx}.json": json.dumps({"call_id": fx, "status": "ok", "text_ar": "تجربة"}),
             f"{P}terminal/{D}_W2.json": json.dumps({"status": "called"}),
             f"{P}calls/{D}_W2.json": json.dumps({"plan": {"action": "add"}, "side": 1}),
             f"{P}terminal/2026-11-09_W1.json": json.dumps({"status": "called"}),
             f"{P}revisions/2026-W45.json": json.dumps({"status": "written", "version": 1,
                                                        "effective_from": "2026-11-09"}),
             f"{P}playbooks/v1.md": "# playbook v1\n",
             "analyst/playbooks/v0.md": "# playbook v0\n"}
    view = DictView(files)
    mem = memory.build(view, dt.date(2026, 11, 11), "canary")
    assert mem["settled"] == [{"call_id": f"{D}_W1", "action": "reverse", "side": -1, "stop_mult": 1.0,
                               "target_mult": 2.0, "S": 20.0, "T": 40.0, "reason": "managed", "R": 0.5,
                               "R_stress": 0.4, "class": "canary"}]
    assert mem["lessons"] == [{"call_id": f"{D}_W1", "text_ar": "درس"}]           # never the fixture's lesson
    assert mem["pending"] == [{"call_id": f"{D}_W2", "action": "add", "side": 1}]   # 11-09 is before a15_from
    assert mem["playbook"]["version"] == 0                       # effective before a15_from: never in force
    view.files[f"{P}revisions/2026-W46.json"] = json.dumps({"status": "written", "version": 1,
                                                            "effective_from": "2026-11-16"}).encode()
    assert memory.playbook_in_force(view, dt.date(2026, 11, 16), "canary")["version"] == 1
    ok, why = weekly.week_complete(view, "canary", dt.date(2026, 11, 14))
    assert not ok                                                 # a canary called window before a15_from
    inp = weekly.revision_input(view, "canary", 1)
    assert [x["call_id"] for x in inp["lessons"]] == [f"{D}_W1"] and inp["settled"][0]["action"] == "reverse"


def test_canary_revision_effective_on_a15_from_is_never_in_force_nor_a_base():
    """A15-D22: a canary revision written on Saturday 2026-10-31, before a15_from was set (from the fixture's lesson),
    takes effect on Monday 2026-11-02 = a15_from. It is never in force, the next canary revision starts from v0, and
    its number is not reused; a revision effective after a15_from is in force and is the next base."""
    files = {paths.MODE_PATH: json.dumps({"mode": "canary", "canary_from": "2026-10-26", "a14_from": "2026-10-29",
                                          "a15_from": "2026-11-02"}),
             "analyst/playbooks/v0.md": "# playbook v0\n",
             f"{P}playbooks/v1.md": "# playbook v1\nfrom the fixture lesson\n",
             f"{P}revisions/2026-W44.json": json.dumps({"status": "written", "version": 1,
                                                        "effective_from": "2026-11-02"})}
    view = DictView(files)
    for d in (dt.date(2026, 11, 2), dt.date(2026, 11, 3), dt.date(2026, 11, 9)):
        pb = memory.playbook_in_force(view, d, "canary")
        assert (pb["version"], pb["path"], pb["text"]) == (0, "analyst/playbooks/v0.md", "# playbook v0\n")
    assert memory.build(view, dt.date(2026, 11, 3), "canary")["playbook"]["version"] == 0
    k = weekly.newest_playbook(view, "canary")
    assert k == 1                                                 # numbering: v1 exists, so the next file is v2
    inp = weekly.revision_input(view, "canary", k)
    assert inp["playbook"] == {"version": 0, "text": "# playbook v0\n"} and inp["next_version"] == 2
    view.files[f"{P}playbooks/v2.md"] = b"# playbook v2\nreal\n"
    view.files[f"{P}revisions/2026-W45.json"] = json.dumps({"status": "written", "version": 2,
                                                            "effective_from": "2026-11-09"}).encode()
    assert memory.playbook_in_force(view, dt.date(2026, 11, 6), "canary")["version"] == 0
    assert memory.playbook_in_force(view, dt.date(2026, 11, 9), "canary")["version"] == 2
    inp = weekly.revision_input(view, "canary", weekly.newest_playbook(view, "canary"))
    assert inp["playbook"] == {"version": 2, "text": "# playbook v2\nreal\n"} and inp["next_version"] == 3
    # without a15_from (the A14 canary) and in live the base is the newest version, as before
    view.files[paths.MODE_PATH] = json.dumps({"mode": "canary", "canary_from": "2026-10-26"}).encode()
    assert weekly.revision_input(view, "canary", 2)["playbook"]["version"] == 2


# ---------------------------------------------------------------- the sessions and the merge (local inbox world)
def m30_1029(t):
    """Flat 30-minute bars; each model day's first bar is wide (R20 = 20, y_l = 3990); the 05:30 bar of 2026-10-29
    reaches 3985: y_low at the 06:00 check."""
    if t == u(2026, 10, 29, 5, 30):
        return (4000.0, 4000.5, 3985.0, 3988.0)
    m = clock.to_server(t)
    return (4000.0, 4010.0, 3990.0, 4000.0) if (m.hour, m.minute) == (1, 0) else (4000.0, 4001.0, 3999.0, 4000.0)


def ctx_of(h):
    return json.loads((h.sess_dir / ".analyst_run" / "context.json").read_text())


def window(h, at_start, at_ctx, at_lock, plan_obj, cid):
    h.at(*at_start)
    assert h.run("start") == 0 and h.log[-1] == f"attempt_pushed {cid} n=1 mode=canary", h.log[-3:]
    h.push_and_merge()
    h.at(*at_ctx)
    assert h.run("context") == 0, h.log[-3:]
    ctx = ctx_of(h)
    if plan_obj is not None:
        h.at(*at_lock)
        assert h.run("lock", str(h.write_plan(plan_obj))) == 0, h.log[-3:]
        h.push_and_merge()
    return ctx


def test_replay_of_the_2026_10_29_canary_day(tmp_path):
    day = "2026-10-29"
    h = Harness(tmp_path, canary_from=day, a14_from=day, a15_from=day, store_last=dt.date(2026, 10, 28),
                slots_until=dt.date(2026, 11, 2), m30=m30_1029, inbox_from_utc="2026-10-20T00:00:00Z")
    c0 = window(h, (2026, 10, 29, 6, 1, 10), (2026, 10, 29, 6, 3), (2026, 10, 29, 6, 10), SELL, f"{day}_T0600")
    assert c0["schema"] == "analyst-context/5" and c0["review"] is None and c0["memory"]["settled"] == []
    assert h.main_json(f"{P}terminal/{day}_T0600.json")["status"] == "called"
    # W1 (entry 08:00): before A15 it was skipped:trade_open; now it is a review of the T0600 sell
    n_req = len(h.feed.requests)
    c1 = window(h, (2026, 10, 29, 7, 20), (2026, 10, 29, 7, 22), None, None, f"{day}_W1")
    rv = c1["review"]
    assert rv["options"] == ["hold", "close", "reverse", "add"] and [o["call_id"] for o in rv["open"]] == \
        [f"{day}_T0600"]
    o = rv["open"][0]
    assert (o["action"], o["side"], o["entry_utc"], o["exit_utc"], o["minutes_left"]) == (
        "sell", "sell", "2026-10-29T06:30:00Z", "2026-10-29T10:30:00Z", 150)
    assert (o["entry_px"], rv["ref_px"], rv["ref_utc"]) == (4000.0, 4000.0, "2026-10-29T07:00:00Z")
    assert o["stop_usd"] == h.main_json(f"{P}calls/{day}_T0600.json")["S"]
    m30r = c1["files"][-1]
    assert m30r["req"] == "XAUUSD_M30R" and m30r["url"] == td.m30_url(u(2026, 10, 29, 6, 30), u(2026, 10, 29, 7))
    assert [b["t_utc"] for b in m30r["bars"]] == ["2026-10-29T06:30:00Z"]
    assert h.feed.requests[-1] == m30r["url"] and len(h.feed.requests) == n_req + 9      # 9: the last request
    assert c1["sources"]["note"].endswith(" review prices come from Twelve Data 30-minute bars in UTC and are "
                                          "indicative; settlement uses Dukascopy minutes, where an open trade may "
                                          "already have reached its stop or target.")
    h.at(2026, 10, 29, 7, 25)
    assert h.run("lock", str(h.write_plan(PLAN))) == 2 and h.log[-1] == "invalid action_not_allowed failures=1"
    assert h.run("lock", str(h.write_plan(HOLD))) == 0 and h.log[-1] == f"locked {day}_W1 status=review_hold"
    h.push_and_merge()
    t1 = h.main_json(f"{P}terminal/{day}_W1.json")
    assert (t1["status"], t1["call"]["plan_status"]) == ("review_hold", "review_hold")
    # W1b (entry 10:00): still a review (the sell exits 10:30); the analyst closes it
    c2 = window(h, (2026, 10, 29, 9, 31), (2026, 10, 29, 9, 32), (2026, 10, 29, 9, 33), CLOSE, f"{day}_W1b")
    assert [x["call_id"] for x in c2["review"]["open"]] == [f"{day}_T0600"]
    assert h.main_json(f"{P}terminal/{day}_W1b.json")["status"] == "review_close"
    # the closer: W2 and W2b without an attempt are missed, never skipped:trade_open
    h.at(2026, 10, 29, 20, 0)
    h.merge()
    assert h.main_json(f"{P}terminal/{day}_W2.json")["status"] == "missed:trigger"
    assert h.main_json(f"{P}terminal/{day}_W2b.json")["status"] == "missed:trigger"
    # analyst-settle the next day: the canary day is settled as live would be, with the managed exit
    h.at(2026, 10, 30, 9, 0)
    h.world.owner_commit(store_files(clock.days_range(dt.date(2026, 9, 1), dt.date(2026, 10, 29))), "mirror")
    settle_run.run_settle(h.workspace("settle"), h.api, h.now, {"workflow": "analyst-settle", "id": 9,
                                                                 "attempt": 1, "event": "push"})
    s = h.main_json(f"{P}settled/{day}_T0600.json")
    assert (s["schema"], s["class"], s["action"], s["side"]) == ("analyst-settled/2", "canary", "sell", -1)
    assert (s["closed_by"], s["managed_utc"], s["weight"], s["cluster"]) == (f"{day}_W1b", "2026-10-29T10:00:00Z",
                                                                             1.0, f"{day}_T0600")
    assert s["outcome"]["reason"] == "managed" and s["x_end_utc"] == "2026-10-29T10:00:00Z"
    assert s["unmanaged"]["status"] == "final" and s["unmanaged"]["reason"] in ("time", "stop", "target")
    assert [(r["call_id"], r["status"], r["action"]) for r in s["reviews"]] == [
        (f"{day}_W1", "review_hold", "hold"), (f"{day}_W1b", "review_close", "close")]
    assert sorted(h.world.main_list(f"{P}twins/")) == sorted(f"{P}twins/{day}_{t}.json"
                                                              for t in ("T0600", "W1", "W1b", "W2", "W2b"))
    assert h.world.main_list("analyst/settled/") == []                       # never counted, never live
    # the next canary day's memory shows it, and the lesson run takes it instead of the fixture
    h.world.mirror()
    view = GitView(h.world.origin, h.world.main_sha())
    mem = memory.build(view, dt.date(2026, 10, 30), "canary")
    assert [(x["call_id"], x["side"], x["reason"]) for x in mem["settled"]] == [(f"{day}_T0600", -1, "managed")]
    h.at(2026, 10, 30, 16, 40)
    assert h.run("lesson_start") == 0 and h.log[-1].startswith(f"lesson_attempt_pushed {day}_T0600 n=1")
    inp = (h.sess_dir / ".analyst_run" / "lesson_input.json").read_bytes()
    assert inp == h.world.main_file(f"{P}settled/{day}_T0600.json")


def test_a_review_that_adds_and_the_closer_without_trade_open(tmp_path):
    from test_a14_session import T1100, UP, trigger_call
    h = Harness(tmp_path, a14_from="2026-11-10", a15_from="2026-11-10", m30=UP)
    trigger_call(h)                                         # a buy at 11:30, exit 15:30
    ctx = window(h, (2026, 11, 10, 14, 1), (2026, 11, 10, 14, 3), (2026, 11, 10, 14, 10), ADD,
                 "2026-11-10_W2")
    assert [o["call_id"] for o in ctx["review"]["open"]] == [T1100]
    call = h.main_json(f"{P}calls/2026-11-10_W2.json")
    s_d, t_d, S, T = plan.s_and_t(ctx["d_ref"]["D_ref_cents"], ADD["stop_mult"], ADD["target_mult"])
    assert (call["status"], call["side"], call["S"], call["T"]) == ("called", 1, S, T)
    assert h.main_json(f"{P}terminal/2026-11-10_W2.json")["status"] == "called"
    assert h.main_json(f"{P}terminal/2026-11-10_W1b.json")["status"] == "missed:trigger"
    # W2b (entry 16:30) sees the add only: the trigger's trade exits 15:30
    ctx = window(h, (2026, 11, 10, 16, 1), (2026, 11, 10, 16, 3), None, None, "2026-11-10_W2b")
    assert [o["call_id"] for o in ctx["review"]["open"]] == ["2026-11-10_W2"]
    assert ctx["review"]["open"][0]["action"] == "add"


def test_operational_keys_of_a14_and_a15():
    for k in ("A15|review_open|2026-11-10_W1b", "A15|open_cap|x", "A15|a15_from|x", "A15|lesson_source|x",
              "A14|skipped_wrong|x", "A14|a14_from|x"):
        assert state.operational(k), k
    assert not state.operational("A15|unsettled|x") and not state.operational("A5|x")


def test_plan_stays_standalone_and_agrees_with_review():
    src = (Path(__file__).resolve().parents[1] / "analyst/ga/plan.py").read_text("utf-8")
    assert "from . import" not in src and "import ga" not in src      # the exam repository holds rec, plan, gitops
    assert plan.ACTIONS_A15 == plan.ACTIONS + review.REVIEW_ACTIONS and plan.MULT_ACTIONS == review.TRADE_ACTIONS


def test_the_closer_never_writes_skipped_trade_open_from_a15_from(tmp_path):
    from test_a14_session import T1100, UP, trigger_call
    h = Harness(tmp_path, a14_from="2026-11-10", a15_from="2026-11-10", m30=UP)
    trigger_call(h)                                         # open until 15:30
    assert h.main_json(f"{P}terminal/{T1100}.json")["status"] == "called"
    h.at(2026, 11, 10, 14, 40)
    h.merge()
    t = h.main_json(f"{P}terminal/2026-11-10_W2.json")      # A14 wrote skipped:trade_open here
    assert (t["status"], t["writer"]) == ("missed:trigger", "closer")
