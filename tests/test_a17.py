"""§16.18 A17 (the time decision): time windows <d>_X<hhmm> and G_X(d) (A17-R1), the subject and R(T)'s time step
(A17-R2, A17-R3), `start`'s time window (A17-R4), `analyst-context/6` (A17-R5), `analyst-plan/4` and `review_extend`
(A17-R6), the open set with extensions (A17-R7), the merge (A17-R8), settlement's x_exit, at_4h, to_day_end and
`analyst-settled/3` (A17-R9), and one canary day replayed on the local inbox world. Days before a17_from keep A16's
rules."""
import datetime as dt
import json

from ga import clock, merge, paths, plan, rec, records, review, settle, settle_run, store, wake
from harness import Harness
from synth import store_files

UTC = rec.UTC
P = "analyst/canary/"
D = dt.date(2026, 11, 10)          # Tuesday, GMT/EST: W1 08:00, W1b 10:00, W2 14:30, W2b 16:30 (entries, UTC)
DS = D.isoformat()
T1100 = f"{DS}_T1100"              # a trigger trade 11:30 - 15:30 (its time window: X1500, entering 15:30)


def u(*a):
    return dt.datetime(*a, tzinfo=UTC)


def entry(cid, status, action=None, side=None, names=(), S=20.0, T=40.0):
    w = clock.window_of(cid)
    return {"call_id": cid, "entry_utc": w.entry_utc, "exit_utc": w.exit_utc, "status": status, "action": action,
            "side": side, "S": S, "T": T, "stop_mult": 1.0, "target_mult": 2.0, "names": list(names), "kind": w.kind}


# ---------------------------------------------------------------- time windows (A17-R1)
def test_time_windows_and_their_grid():
    x = clock.window_of(f"{DS}_X1530")
    assert (x.kind, x.name, x.exchange, x.tag) == ("time", "X", None, "X1530")
    assert (x.check_utc, x.entry_utc, x.deadline_utc, x.span_start_utc, x.exit_utc) == (
        u(2026, 11, 10, 15, 30), u(2026, 11, 10, 16), u(2026, 11, 10, 15, 45), u(2026, 11, 10, 15, 30),
        u(2026, 11, 10, 20))
    assert clock.window_type(f"{DS}_X1530") == "X" and clock.window_type(T1100) == "T"
    assert clock.window_key(f"{DS}_W2") < clock.window_key(f"{DS}_X1500") < clock.window_key(f"{DS}_W2b")
    assert clock.day_end(D) == u(2026, 11, 10, 22) and clock.day_end(dt.date(2026, 10, 28)) == u(2026, 10, 28, 21)
    g, gx = clock.check_grid(D), clock.time_grid(D)
    assert g[-1] == u(2026, 11, 10, 17, 30) and gx[:len(g)] == g
    assert gx[len(g):] == [u(2026, 11, 10, 18) + i * clock.HALF for i in range(6)]        # 18:00 .. 20:30
    assert clock.valid_time(D, "X2030") and clock.valid_time(D, "X1500") and not clock.valid_time(D, "X2100")
    assert not clock.valid_time(D, "X0930")                                               # W1b's check
    assert clock.time_grid(dt.date(2026, 11, 14)) == [] and clock.time_tag(u(2026, 11, 10, 15)) == "X1500"
    assert clock.CALL_ID_RE.fullmatch(f"{DS}_X1500") and not clock.CALL_ID_RE.fullmatch(f"{DS}_X1515")
    m = {"mode": "canary", "a14_from": DS, "a15_from": DS, "a16_from": DS, "a17_from": "2026-11-11"}
    assert paths.a17_from(m) == dt.date(2026, 11, 11) and not paths.a17_applies(m, D)
    assert paths.a17_applies(m, dt.date(2026, 11, 11)) and paths.a17_from({"a16_from": DS}) is None


# ---------------------------------------------------------------- R(T)'s time step (A17-R3)
def test_the_time_step_of_r():
    T = u(2026, 11, 10, 15)
    r = wake.rule(D, T, [], [], [], False, 10, subject=T1100, a17=True)
    assert (r["result"], r["tag"], r["kind"], r["reason"]) == ("window", "X1500", "time", None)
    assert wake.rule(D, T, [], [], [], False, 10, subject=T1100)["result"] == "none"       # before a17_from
    ten = [{"check_utc": u(2026, 11, 10, h), "kind": "trigger", "reason": "move"} for h in range(1, 11)]
    assert wake.rule(D, T, [], ten, [], False, 10, subject=T1100, a17=True)["result"] == "day_cap"
    r = wake.rule(D, u(2026, 11, 10, 16), [], [], [], False, 10, subject=T1100, a17=True)
    assert (r["result"], r["tag"]) == ("window", "W2b")                                  # a scheduled check wins
    assert wake.rule(D, T, [], [], [], True, 10, subject=T1100, a17=True)["result"] == "none"   # holiday row
    near = [{"check_utc": u(2026, 11, 10, 14, 30), "kind": "trigger", "reason": "move"}]
    assert wake.rule(D, T, [], near, [], False, 10, subject=T1100, a17=True)["tag"] == "X1500"  # no gap rule
    assert wake.rule(D, u(2026, 11, 10, 19), [], [], [], False, 10, a17=True)["result"] == "none"
    assert wake.rule(D, u(2026, 11, 10, 19), [], [], [], False, 10, subject="x", a17=True)["tag"] == "X1900"


# ---------------------------------------------------------------- the open set and the subject (A17-R2, A17-R7)
def test_the_open_set_with_an_extension():
    w1 = entry(f"{DS}_W1", "called", "buy", 1)                                # 08:00 - 12:00
    x = entry(f"{DS}_X1130", "review_extend", "extend", 0, [f"{DS}_W1"])      # enters at 12:00, W1's exit
    w2 = entry(f"{DS}_W2", "review_hold", "hold", 0, [f"{DS}_W1"])            # 14:30, a review of the extended W1
    w2b = entry(f"{DS}_W2b", "review_close", "close", 0, [f"{DS}_W1"])        # 16:30 closes it
    o, eff, closed, ext = review.open_state([w1, x, w2, w2b])
    assert o[x["call_id"]] == [] and [t["call_id"] for t in o[w2["call_id"]]] == [w1["call_id"]]
    assert ext == {w1["call_id"]: x["call_id"]} and closed == {w1["call_id"]: w2b["call_id"]}
    assert eff[w1["call_id"]] == u(2026, 11, 10, 16, 30)
    _o, eff2, _c, _x = review.open_state([w1, x, w2])
    assert eff2[w1["call_id"]] == clock.day_end(D) == u(2026, 11, 10, 22)
    assert review.open_sets([w1, x, w2])[1] == eff2                          # the A15 view agrees
    for st, names in (("review_close", [w1["call_id"]]), ("no_call:invalid", []), ("review_extend", []),
                      ("missed:trigger", [])):
        o3, eff3, _c3, ext3 = review.open_state([w1, dict(x, status=st, names=names), w2])
        assert ext3 == {} and eff3[w1["call_id"]] == w1["exit_utc"] and o3[w2["call_id"]] == [], st
    _o4, eff4, _c4, _x4 = review.open_state([dict(x, status="called", side=1)])
    assert eff4 == {}                                                       # a time window opens nothing
    # a second trade open at the time window (an A15 add, 10:00 - 14:00): X is a review of neither trade
    add = entry(f"{DS}_W1b", "called", "add", 1, [f"{DS}_W1"])
    es = [w1, add, x]
    o5, _e5, _c5, _x5 = review.open_state(es)
    assert o5[x["call_id"]] == [] and review.reviews_of(es, o5) == {w1["call_id"]: [add]}


def test_the_subject_of_a_check():
    t = entry(T1100, "called", "buy", 1)
    T = u(2026, 11, 10, 15)
    assert review.time_subject([t], T, D)["call_id"] == T1100
    assert review.time_subject([t], u(2026, 11, 10, 14, 30), D) is None
    for c in (entry(f"{DS}_W2", "review_close", "close", 0, [T1100]),
              entry(f"{DS}_W2", "called", "reverse", -1, [T1100])):
        assert review.time_subject([t, c], T, D) is None                    # closed before its exit
    assert review.time_subject([t, entry(f"{DS}_W2", "review_hold", "hold", 0, [T1100])], T, D)["call_id"] == T1100
    assert review.time_subject([dict(t, status="stay_out")], T, D) is None
    late = entry(f"{DS}_T1700", "called", "sell", -1)                       # exits 21:30 > 22:00 − 60 min
    assert review.time_subject([late], u(2026, 11, 10, 21), D) is None
    last = entry(f"{DS}_T1630", "called", "sell", -1)                       # exits 21:00 = 22:00 − 60 min
    assert review.time_subject([last], u(2026, 11, 10, 20, 30), D)["call_id"] == f"{DS}_T1630"


# ---------------------------------------------------------------- the /6 blocks (A17-R5)
def test_the_review_blocks_of_context_6():
    t = entry(T1100, "called", "buy", 1)
    bars = [(u(2026, 11, 10, 11, 30), 4000.0, 4006.0, 3995.0, 4005.0),
            (u(2026, 11, 10, 12), 4005.0, 4008.0, 4001.0, 4003.0)]
    end = clock.day_end(D)
    tb = review.block([t], u(2026, 11, 10, 15, 30), bars, kind="time", cur={T1100: (t["exit_utc"], False)},
                      extend_until=end)
    assert list(tb) == ["kind", "open", "options", "ref_px", "ref_utc", "extend_until_utc"]
    assert (tb["kind"], tb["options"], tb["extend_until_utc"]) == ("time", ["extend", "close"],
                                                                    "2026-11-10T22:00:00Z")
    o = tb["open"][0]
    assert list(o)[:6] == ["call_id", "action", "side", "entry_utc", "exit_utc", "extended"]
    assert (o["exit_utc"], o["extended"], o["minutes_left"], o["move_usd"]) == ("2026-11-10T15:30:00Z", False, 0,
                                                                              3.0)
    rb = review.block([t], u(2026, 11, 10, 16, 30), bars, kind="review", cur={T1100: (end, True)})
    assert (rb["kind"], rb["options"], rb["extend_until_utc"]) == ("review", ["hold", "close", "reverse", "add"],
                                                                    None)
    assert (rb["open"][0]["exit_utc"], rb["open"][0]["extended"], rb["open"][0]["minutes_left"]) == (
        "2026-11-10T22:00:00Z", True, 330)
    b5 = review.block([t], u(2026, 11, 10, 16, 30), bars)                   # analyst-context/5 is unchanged
    assert "kind" not in b5 and "extended" not in b5["open"][0] and list(b5)[0] == "open"
    assert review.allowed_actions({"review": tb}) == ("extend", "close")


# ---------------------------------------------------------------- plans, calls and wakes (A17-R4, A17-R6, A17-R8)
EXT = {"action": "extend", "confidence": "low", "reason_ar": "الاتجاه مستمر", "key_risks_ar": ""}
CLOSE = dict(EXT, action="close")
HOLD = dict(EXT, action="hold")
BUY = {"action": "buy", "stop_mult": 1.0, "target_mult": 2.0, "confidence": "low", "reason_ar": "شراء",
       "key_risks_ar": ""}


def v(obj, p4=True):
    return plan.validate(json.dumps(obj).encode(), stay_reason=True, plan3=True, plan4=p4)


def test_plan_4_and_its_status():
    assert v(EXT)[1] is None and v(EXT)[0] == EXT and v(CLOSE)[1] is None
    assert "action" in v(EXT, p4=False)[1]                                  # analyst-plan/3 has no extend
    assert v(dict(EXT, stop_mult=1.0))[1] == ["extra_keys"] and v(dict(EXT, stay_out_reason="no_setup"))[1] == [
        "extra_keys"]
    assert plan.STATUS_OF["extend"] == "review_extend" and plan.side_of("extend") == 0
    assert plan.plan4_applies("analyst-context/6") and not plan.plan4_applies("analyst-context/5")
    assert plan.plan3_applies("analyst-context/6") and plan.stay_reason_applies("analyst-context/6")


def ctx6(cid, review_block, cents=2000):
    w = clock.window_of(cid)
    return {"schema": "analyst-context/6", "call_id": cid, "mode": "canary", "window": w.name, "model_day": DS,
            "entry_utc": rec.iso(w.entry_utc), "exit_utc": rec.iso(w.exit_utc),
            "deadline_utc": rec.iso(w.deadline_utc), "asof_utc": rec.iso(w.deadline_utc - dt.timedelta(minutes=5)),
            "repo_head_sha": "a" * 40, "detail": None, "status": "ok", "review": review_block,
            "d_ref": {"D_ref": cents / 100, "D_ref_cents": cents}}


def call_of(ctx, status, obj):
    return records.call(ctx, rec.compact_sha256(ctx), 1, status, obj, "p" * 64, 0, {"version": 0, "sha256": "s"},
                        {}, {"manifest_sha256": "m", "code_sha8": "c", "routine_prompt_sha256": "r"}, {})


def test_calls_and_wakes_of_time_windows():
    t = entry(T1100, "called", "buy", 1)
    tb = review.block([t], u(2026, 11, 10, 15, 30), [], kind="time", cur={T1100: (t["exit_utc"], False)},
                      extend_until=clock.day_end(D))
    x = f"{DS}_X1500"
    c = call_of(ctx6(x, tb), "review_extend", EXT)
    assert (c["status"], c["side"], c["S"]) == ("review_extend", 0, None) and records.valid_call(c)
    assert plan.derivable(c)
    assert records.valid_call(call_of(ctx6(x, tb), "review_close", CLOSE))
    assert not records.valid_call(call_of(ctx6(x, tb), "review_hold", HOLD))           # not a time decision
    assert not records.valid_call(dict(c, status="review_close"))                        # status from the plan
    as5 = dict(ctx6(x, tb), schema="analyst-context/5")
    assert not records.valid_call(call_of(as5, "review_extend", EXT))                    # extend needs /6
    rb = review.block([t], u(2026, 11, 10, 14, 30), [], kind="review", cur={T1100: (t["exit_utc"], False)})
    assert not records.valid_call(call_of(ctx6(f"{DS}_W2", rb), "review_extend", EXT))  # a review, not time
    assert records.valid_call(call_of(ctx6(f"{DS}_W2", rb), "review_hold", HOLD))
    assert not records.valid_call(call_of(ctx6(x, None), "called", BUY))                 # a time window opens nothing
    xw = clock.window_of(x)
    assert records.valid_wake(records.wake_of(xw, T1100), x)
    assert not records.valid_wake(records.wake_of(xw), x)                                # the subject is named
    assert not records.valid_wake(records.wake_of(xw, f"{DS}_X1100"), x)
    assert not records.valid_wake(records.wake_of(xw, "2026-11-09_T1100"), x)
    assert records.valid_wake(records.wake_of(clock.window_of(T1100), "asia_up", {}), T1100) is False   # unchanged


def test_merge_ids_and_expected_time_windows():
    m17 = {"mode": "canary", "canary_from": DS, "a14_from": DS, "a15_from": DS, "a17_from": DS}
    m16 = {k: x for k, x in m17.items() if k != "a17_from"}
    for tag, ok in (("X1500", True), ("X2030", True), ("X2100", False), ("X0930", False)):
        assert merge.a14_window_ok(m17, clock.Window(D, tag)) is ok, tag
    assert not merge.a14_window_ok(m16, clock.Window(D, "X1500"))
    assert merge.a14_window_ok(m16, clock.Window(D, "T1100"))
    assert "review_extend" in merge.TERMINAL_STATUSES and merge.WAKE_KINDS == ("trigger", "time")


# ---------------------------------------------------------------- settlement (A17-R9)
TX = clock.Window(D, "T1100")
T0 = int(TX.entry_utc.timestamp())


def walk(n_min, mid=4000000, spread=200, up_after=None):
    """Flat minutes from T0, rising 10 $ from minute `up_after` on (a long's target of 40 $ then hits later)."""
    m = store.Minutes()
    for i in range(-10, n_min):
        t = T0 + 60 * i
        x = mid + (50000 if up_after is not None and i >= up_after else 0)
        m.side["BID"][t] = (x - spread // 2,) * 4 + (1.0,)
        m.side["ASK"][t] = (x + spread // 2,) * 4 + (1.0,)
    return m


def test_an_extended_trade_and_the_time_block():
    call = {"side": 1, "S": 20.0, "S_dimes": 200, "T_dimes": 400}
    end = clock.day_end(D)
    m = walk(11 * 60, up_after=300)                    # +50 $ five hours in: after the 4-hour exit
    a4 = settle.trade(m, TX, 1, 200, 400)[0]
    assert (a4["reason"], a4["exit_minute_utc"]) == ("time", "2026-11-10T15:29:00Z")
    res = settle.outcomes(m, settle.with_exit(TX, end), call, {})
    assert (res["outcome"]["reason"], res["outcome"]["exit_minute_utc"]) == ("target", "2026-11-10T16:30:00Z")
    assert res["x_end"] == u(2026, 11, 10, 16, 31)
    tb = settle.time_block(m, TX, call, {}, f"{DS}_X1500", "review_extend", True, end)
    assert list(tb) == ["call_id", "status", "extended", "x_exit_utc", "at_4h", "to_day_end"]
    assert (tb["extended"], tb["x_exit_utc"]) == (True, "2026-11-10T22:00:00Z")
    assert tb["at_4h"] == a4 and tb["to_day_end"]["reason"] == "target"
    flat = walk(11 * 60)
    hold = settle.outcomes(flat, settle.with_exit(TX, end), call, {})["outcome"]
    assert (hold["reason"], hold["exit_minute_utc"]) == ("time", "2026-11-10T21:59:00Z")
    assert settle.with_exit(TX, TX.exit_utc) is TX
    late = clock.Window(D, "T1700")                    # exits 21:30: no extension possible, so no shadows
    lt = settle.time_block(flat, late, call, {}, None, None, False, late.exit_utc)
    assert (lt["call_id"], lt["at_4h"], lt["to_day_end"], lt["x_exit_utc"]) == (None, None, None,
                                                                               "2026-11-10T21:30:00Z")


# ---------------------------------------------------------------- one canary day on the local inbox world
def up_m30(t):
    """Flat 30-minute bars; each model day's first bar is wide (R20 = 20); asia_up at the 11:00 check."""
    if t == u(2026, 11, 10, 10, 30):
        return (4000.0, 4012.0, 3999.0, 4011.0)
    mm = clock.to_server(t)
    return (4000.0, 4010.0, 3990.0, 4000.0) if (mm.hour, mm.minute) == (1, 0) else (4000.0, 4001.0, 3999.0, 4000.0)


def ctx_of(h):
    return json.loads((h.sess_dir / ".analyst_run" / "context.json").read_text())


def state_of(h):
    return json.loads((h.sess_dir / ".analyst_run" / "state.json").read_text())


def step(h, at_start, at_ctx, at_lock, plan_obj, cid):
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


def test_a_canary_day_with_a_time_decision(tmp_path):
    h = Harness(tmp_path, canary_from=DS, a14_from=DS, a15_from=DS, a16_from=DS, a17_from=DS, m30=up_m30)
    step(h, (2026, 11, 10, 11, 1, 10), (2026, 11, 10, 11, 3), (2026, 11, 10, 11, 10), BUY, T1100)
    assert h.main_json(f"{P}terminal/{T1100}.json")["status"] == "called"
    # 15:00: T1100 exits at 15:30, so the check's candidate is its time window X1500, with no XAUUSD_M30 request
    n_req = len(h.feed.requests)
    h.at(2026, 11, 10, 15, 1, 10)
    assert h.run("start") == 0 and h.log[-1] == f"attempt_pushed {DS}_X1500 n=1 mode=canary", h.log[-3:]
    assert len(h.feed.requests) == n_req
    wk = state_of(h)["wake"]
    assert (wk["kind"], wk["reason"], wk["check_utc"], wk["td"]) == ("time", T1100, "2026-11-10T15:00:00Z", None)
    h.push_and_merge()
    h.at(2026, 11, 10, 15, 3)
    assert h.run("context") == 0, h.log[-3:]
    c = ctx_of(h)
    rv = c["review"]
    assert (c["schema"], c["window"], rv["kind"], rv["options"], rv["extend_until_utc"]) == (
        "analyst-context/6", "X", "time", ["extend", "close"], "2026-11-10T22:00:00Z")
    assert [(o["call_id"], o["exit_utc"], o["extended"], o["minutes_left"]) for o in rv["open"]] == [
        (T1100, "2026-11-10T15:30:00Z", False, 0)]
    assert c["files"][-1]["req"] == "XAUUSD_M30R"
    h.at(2026, 11, 10, 15, 8)
    assert h.run("lock", str(h.write_plan(HOLD))) == 2 and h.log[-1] == "invalid action_not_allowed failures=1"
    assert h.run("lock", str(h.write_plan(EXT))) == 0 and h.log[-1] == f"locked {DS}_X1500 status=review_extend"
    h.push_and_merge()
    assert h.main_json(f"{P}terminal/{DS}_X1500.json")["status"] == "review_extend"
    # W2b (entry 16:30) is a review of the extended trade: its exit is now the end of the day
    c2 = step(h, (2026, 11, 10, 16, 1), (2026, 11, 10, 16, 3), (2026, 11, 10, 16, 10), HOLD, f"{DS}_W2b")
    o = c2["review"]["open"]
    assert (c2["review"]["kind"], [(x["call_id"], x["exit_utc"], x["extended"], x["minutes_left"]) for x in o]) == (
        "review", [(T1100, "2026-11-10T22:00:00Z", True, 330)])
    # 18:00: beyond L_d with no subject, the check is not due
    h.at(2026, 11, 10, 18, 1, 10)
    assert h.run("start") == 0 and h.log[-1] == "refused not_due"
    # the closer, then analyst-settle the next day: the extended trade runs to 22:00, the time window settles nothing
    h.at(2026, 11, 10, 22, 30)
    h.merge()
    h.at(2026, 11, 11, 9, 0)
    h.world.owner_commit(store_files(clock.days_range(dt.date(2026, 9, 1), D)), "mirror")
    settle_run.run_settle(h.workspace("settle"), h.api, h.now, {"workflow": "analyst-settle", "id": 9, "attempt": 1,
                                                                 "event": "push"})
    s = h.main_json(f"{P}settled/{T1100}.json")
    assert (s["schema"], s["exit_utc"], s["closed_by"]) == ("analyst-settled/3", "2026-11-10T15:30:00Z", None)
    tb = s["time"]
    assert (tb["call_id"], tb["status"], tb["extended"], tb["x_exit_utc"]) == (
        f"{DS}_X1500", "review_extend", True, "2026-11-10T22:00:00Z")
    assert tb["at_4h"]["status"] == "final" and tb["to_day_end"]["status"] == "final"
    out = s["outcome"]
    assert out["exit_minute_utc"] <= "2026-11-10T21:59:00Z"
    assert out == tb["to_day_end"]                    # no later close: the outcome is the hold to the day's end
    if out["reason"] == "time":
        assert (out["exit_minute_utc"], s["x_end_utc"]) == ("2026-11-10T21:59:00Z", "2026-11-10T22:00:00Z")
    assert [(r["call_id"], r["status"]) for r in s["reviews"]] == [(f"{DS}_W2", "missed:trigger"),
                                                                    (f"{DS}_W2b", "review_hold")]
    names = h.world.main_list(f"{P}settled/") + h.world.main_list(f"{P}twins/")
    assert not any("_X1500" in p for p in names)       # a time window: no settled record, no twin
    assert f"{P}twins/{T1100}.json" in names


def test_without_a17_from_the_same_check_stays_a_trigger_candidate(tmp_path):
    h = Harness(tmp_path, canary_from=DS, a14_from=DS, a15_from=DS, a16_from=DS, m30=up_m30)
    step(h, (2026, 11, 10, 11, 1, 10), (2026, 11, 10, 11, 3), (2026, 11, 10, 11, 10), BUY, T1100)
    h.at(2026, 11, 10, 15, 1, 10)
    assert h.run("start") == 0 and h.log[-1] == "refused not_triggered"
    h.at(2026, 11, 10, 18, 1, 10)
    assert h.run("start") == 0 and h.log[-1] == "refused outside_span"
