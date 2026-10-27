// R-AN-89 in worker.js: the clock (DST table of R-AN-8), G(d), the conditions, R(T), Twelve Data classification,
// and the build checks on the files (holiday table, inbox pin, wrangler.toml).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { MIN, REPO, W, WATCHER, at, iso } from "./helpers.mjs";

const hm = (ms) => iso(ms).slice(11, 16);

test("windows and L_d follow the DST table of R-AN-8 (A14 rows), US change 2026-11-01", () => {
  const rows = [ // day, W1 entry, W1b entry, W2 entry, W2b entry, L_d, |G(d)|
    ["2026-10-23", "07:00", "09:00", "13:30", "15:30", "16:30", 37],
    ["2026-10-26", "08:00", "10:00", "13:30", "15:30", "16:30", 37],
    ["2026-10-30", "08:00", "10:00", "13:30", "15:30", "16:30", 37],
    ["2026-11-02", "08:00", "10:00", "14:30", "16:30", "17:30", 39],
    ["2027-03-12", "08:00", "10:00", "14:30", "16:30", "17:30", 39],
    ["2027-03-15", "08:00", "10:00", "13:30", "15:30", "16:30", 37],
    ["2027-03-29", "07:00", "09:00", "13:30", "15:30", "16:30", 37],
    ["2027-11-01", "08:00", "10:00", "13:30", "15:30", "16:30", 37],
    ["2027-11-08", "08:00", "10:00", "14:30", "16:30", "17:30", 39],
  ];
  for (const [d, w1, w1b, w2, w2b, L, n] of rows) {
    const ws = Object.fromEntries(W.scheduledWindows(d).map((w) => [w.name, w]));
    assert.deepEqual([hm(ws.W1.entry), hm(ws.W1b.entry), hm(ws.W2.entry), hm(ws.W2b.entry)], [w1, w1b, w2, w2b], d);
    for (const w of Object.values(ws)) {
      assert.equal(w.exit - w.entry, 240 * MIN);
      assert.equal(w.entry - w.check, 30 * MIN);
      assert.ok(W.inGrid(d, w.check), `${d} ${w.name} check in G(d)`);
      assert.ok(w.exit <= W.modelDayEnd(d), "window inside the model day");
    }
    assert.equal(hm(W.lastCheck(d)), L, d);
    const g = W.grid(d);
    assert.equal(g.length, n, d);
    assert.equal(g[0], at(`${d}T00:00:00Z`) - 90 * MIN, "first check 22:30Z of the day before");
    for (const T of g) assert.ok(T + 270 * MIN <= W.modelDayEnd(d), "every trigger window fits the model day");
  }
});

test("the model day of a time, Sunday 22:00 open and the edges of G(d)", () => {
  assert.equal(W.tday(at("2026-11-02T22:30:00Z")), "2026-11-03");
  assert.equal(W.triggerWindow("2026-11-03", at("2026-11-02T22:30:00Z")).call_id, "2026-11-03_T2230"); // R-AN-4
  // Sunday 2026-11-01 (the US change day): Monday's first check is Sunday 22:30Z; 22:00 is not a check
  assert.equal(W.tday(at("2026-11-01T22:30:00Z")), "2026-11-02");
  assert.ok(W.inGrid("2026-11-02", at("2026-11-01T22:30:00Z")));
  assert.ok(!W.inGrid("2026-11-02", at("2026-11-01T22:00:00Z")));
  assert.ok(!W.inGrid("2026-11-01", at("2026-11-01T12:00:00Z")), "Sunday is not a model weekday");
  assert.ok(!W.inGrid("2026-10-31", at("2026-10-30T22:30:00Z")), "Friday 22:30Z belongs to Saturday");
  assert.ok(W.inGrid("2026-10-30", at("2026-10-30T16:30:00Z")) && !W.inGrid("2026-10-30", at("2026-10-30T17:00:00Z")));
  assert.ok(W.inGrid("2026-11-06", at("2026-11-06T17:30:00Z")) && !W.inGrid("2026-11-06", at("2026-11-06T18:00:00Z")));
});

test("the holiday table in worker.js is analyst/tables/holidays.csv byte for byte", () => {
  assert.equal(W.HOLIDAYS_CSV, readFileSync(join(REPO, "analyst", "tables", "holidays.csv"), "utf8"));
  assert.ok(W.isHolidayDay("2026-11-26") && W.isHolidayFor("2026-11-26", "NYSE") && !W.isHolidayFor("2026-11-26", "LSE"));
  assert.ok(W.isHolidayDay("2026-12-28") && W.isHolidayFor("2026-12-28", "LSE") && !W.isHolidayDay("2026-11-25"));
});

test("the inbox read by the watcher is analyst/PINS.json inbox_repo", () => {
  const pins = JSON.parse(readFileSync(join(REPO, "analyst", "PINS.json"), "utf8"));
  assert.equal(W.INBOX_REPO, pins.inbox_repo);
});

test("wrangler.toml: name, cron, entry deployed unbundled, KV binding, no secret", () => {
  const t = readFileSync(join(WATCHER, "wrangler.toml"), "utf8");
  assert.match(t, /^name = "gold-analyst-watcher"$/m);
  assert.match(t, /^main = "worker\.js"$/m);
  assert.match(t, /^compatibility_date = "2026-10-01"$/m);
  assert.match(t, /^no_bundle = true$/m);
  assert.match(t, /^crons = \["1,31 \* \* \* \*"\]$/m);
  assert.match(t, /^binding = "WATCHER_LOG"$/m);
  assert.doesNotMatch(t, /^\s*(TD_API_KEY|FIRE_URL|FIRE_TOKEN)\s*=|^\[vars\]|apikey |Bearer /m);
});

// ---- conditions and R(T)

const DAY = "2026-11-04"; // a Wednesday on GMT/EST
const bar = (t, o, h, l, c) => ({ t: typeof t === "string" ? at(t) : t, o, h, l, c });

/** 20 prior weekdays with range 10 (h 105, l 95), then the bars of DAY given. */
function history(today, priorDays = 20, last = { h: 105, l: 95 }) {
  const bars = [];
  let e = DAY;
  let n = 0;
  while (n < priorDays) {
    e = new Date(at(`${e}T00:00:00Z`) - 86_400_000).toISOString().slice(0, 10);
    if (!W.isWeekday(e)) continue;
    const t0 = at(`${e}T12:00:00Z`);
    const hl = n === 0 ? last : { h: 105, l: 95 };
    bars.push(bar(t0, 100, hl.h, hl.l, 100));
    n += 1;
  }
  return [...bars.reverse(), ...today];
}

test("conditionsAt: R20, yesterday, Asia, c_prev; all false without bar_T or with 19 prior days", () => {
  const T = at(`${DAY}T09:00:00Z`);
  const today = [
    bar("2026-11-03T23:00:00Z", 100, 101, 99, 100), // Asia: 99..101
    bar(`${DAY}T07:00:00Z`, 100, 100.5, 99.5, 100),
    bar(`${DAY}T07:30:00Z`, 100, 101, 100, 101), // closes at or before T − 60 min: c_prev = 101
    bar(`${DAY}T08:30:00Z`, 101, 104.2, 101, 104), // bar_T (closes at T)
  ];
  const c = W.conditionsAt(T, DAY, history(today));
  assert.equal(c.R20, 10);
  assert.deepEqual([c.asia_up, c.asia_dn, c.y_high, c.y_low, c.move], [true, false, true, false, true]);
  // y_high threshold: h >= y_h − 0.1·R20 = 104
  const c2 = W.conditionsAt(T, DAY, history([...today.slice(0, 3), bar(`${DAY}T08:30:00Z`, 101, 103.99, 101, 102)]));
  assert.deepEqual([c2.y_high, c2.move], [false, false]);
  // bar_T missing (the 08:30 bar never came): every condition false
  const c3 = W.conditionsAt(T, DAY, history(today.slice(0, 3)));
  assert.deepEqual([c3.asia_up, c3.asia_dn, c3.y_high, c3.y_low, c3.move], [false, false, false, false, false]);
  // 19 prior days: R20 undefined
  const c4 = W.conditionsAt(T, DAY, history(today, 19));
  assert.equal(c4.R20, null);
  assert.ok(!c4.asia_up && !c4.y_high && !c4.move);
  // a bar that has not closed by T is not used
  const c5 = W.conditionsAt(at(`${DAY}T08:30:00Z`), DAY, history(today));
  assert.equal(c5.y_high, false);
  // before 07:00 UTC no Asia break
  const c6 = W.conditionsAt(at(`${DAY}T06:30:00Z`), DAY,
    history([bar("2026-11-03T23:00:00Z", 100, 101, 99, 100), bar(`${DAY}T06:00:00Z`, 100, 102, 100, 102)]));
  assert.equal(c6.asia_up, false);
});

test("median is statistics.median", () => {
  assert.equal(W.median([3, 1, 2]), 2);
  assert.equal(W.median([4, 1, 3, 2]), 2.5);
});

const allTrue = { asia_up: true, asia_dn: true, y_high: true, y_low: true, move: true };
const T0 = at(`${DAY}T12:00:00Z`); // not a scheduled check on 2026-11-04 (09:30 W1b, 14:00 W2, 16:00 W2b, 07:30 W1)

test("R(T): order of the steps, scheduled checks, holidays, gap, consumed conditions, move gap", () => {
  const trig = (check, reason) => ({ check, kind: "trigger", reason });
  assert.equal(W.ruleR(T0, DAY, [], [{}], allTrue).result, "trade_open");
  const six = Array.from({ length: 6 }, (_, i) => trig(T0 - (i + 3) * 30 * MIN, "x"));
  assert.equal(W.ruleR(T0, DAY, six, [], allTrue).result, "day_cap");
  assert.equal(W.ruleR(T0, DAY, six.slice(1), [], allTrue).window.reason, "asia_up");
  // scheduled check: the window even with a wake 30 min earlier, and even on a holiday
  const s = W.ruleR(at(`${DAY}T09:30:00Z`), DAY, [trig(at(`${DAY}T09:00:00Z`), "y_low")], [], undefined);
  assert.deepEqual([s.result, s.window.name, s.window.kind, s.window.reason], ["window", "W1b", "second_look", null]);
  const h = W.ruleR(at("2026-11-26T16:00:00Z"), "2026-11-26", [], [], undefined);
  assert.deepEqual([h.result, h.window.name], ["window", "W2b"]);
  assert.deepEqual(W.ruleR(at("2026-11-26T12:00:00Z"), "2026-11-26", [], [], allTrue).why, "holiday");
  assert.deepEqual(W.ruleR(at("2026-11-27T12:00:00Z"), "2026-11-27", [], [], allTrue).why, "holiday", "early close");
  // gap
  assert.equal(W.ruleR(T0, DAY, [trig(T0 - 30 * MIN, "y_low")], [], allTrue).why, "gap");
  assert.equal(W.ruleR(T0, DAY, [trig(T0 - 60 * MIN, "y_low")], [], undefined).result, "trigger_due");
  // consumed, in order; move needs g >= 120
  const E = [trig(T0 - 60 * MIN, "asia_up"), { check: T0 - 300 * MIN, kind: "fixed", reason: null }];
  assert.equal(W.ruleR(T0, DAY, E, [], allTrue).window.reason, "asia_dn");
  const onlyMove = { ...allTrue, asia_up: false, asia_dn: false, y_high: false, y_low: false };
  assert.equal(W.ruleR(T0, DAY, [trig(T0 - 90 * MIN, "y_low")], [], onlyMove).why, "no_condition");
  const m = W.ruleR(T0, DAY, [trig(T0 - 120 * MIN, "y_low")], [], onlyMove);
  assert.deepEqual([m.window.call_id, m.window.reason, m.window.entry, m.window.exit],
    [`${DAY}_T1200`, "move", T0 + 30 * MIN, T0 + 270 * MIN]);
  assert.equal(W.ruleR(T0, DAY, [], [], onlyMove).window.reason, "move", "g infinite with no wake");
  assert.equal(W.ruleR(T0, DAY, [trig(T0 - 180 * MIN, "move")], [], onlyMove).why, "no_condition", "move consumed");
});

// ---- Twelve Data

const enc = (o) => new TextEncoder().encode(typeof o === "string" ? o : JSON.stringify(o));
const val = (dt, o = "4000.1", h = "4001", l = "3999", c = "4000.5") => ({ datetime: dt, open: o, high: h, low: l, close: c });

test("the request URL is R-AN-90's, A = 22:00 of d − 33 days, B = T", () => {
  assert.equal(W.tdUrl("2026-11-03", at("2026-11-03T09:00:00Z")),
    "https://api.twelvedata.com/time_series?symbol=XAU/USD&interval=30min&start_date=2026-10-01T22:00:00" +
    "&end_date=2026-11-03T09:00:00&timezone=UTC&order=asc&outputsize=5000&dp=5");
});

test("classification of a Twelve Data response (R-AN-87, 30-minute datetimes)", () => {
  const ok = W.classifyTd(200, enc({ status: "ok", values: [val("2026-11-03 08:30:00"), val("2026-11-03 08:00:00")] }));
  assert.equal(ok.status, "ok");
  assert.deepEqual(ok.bars.map((b) => iso(b.t)), ["2026-11-03T08:00:00Z", "2026-11-03T08:30:00Z"]);
  assert.equal(W.classifyTd(200, enc({ status: "ok", values: [] })).status, "ok");
  const bad = (values) => W.classifyTd(200, enc({ status: "ok", values })).reason;
  assert.equal(bad([val("2026-11-03 08:15:00")]), "invalid");
  assert.equal(bad([val("2026-11-03 08:30:01")]), "invalid");
  assert.equal(bad([val("2026-02-30 08:30:00")]), "invalid");
  assert.equal(bad([val("2026-11-03 08:30:00"), val("2026-11-03 08:30:00")]), "invalid");
  assert.equal(bad([val("2026-11-03 08:30:00", "4000", "3999")]), "invalid", "h < o");
  assert.equal(bad([val("2026-11-03 08:30:00", "4000", "4001", "4000.6")]), "invalid", "l > c");
  assert.equal(bad([val("2026-11-03 08:30:00", "-1")]), "invalid");
  assert.equal(bad([{ ...val("2026-11-03 08:30:00"), close: 4000.5 }]), "invalid", "a number, not a string");
  assert.equal(W.classifyTd(200, enc("not json")).reason, "invalid");
  assert.equal(W.classifyTd(200, new Uint8Array([0xff, 0xfe])).reason, "invalid");
  const big = { status: "ok", values: [val("2026-11-03 08:30:00")], pad: "x".repeat(262_144) };
  assert.equal(W.classifyTd(200, enc(big)).reason, "invalid", "body over 262,144 bytes");
  assert.deepEqual([401, 403, 429, 500, 503, 0, 404].map((h) => W.classifyTd(h, h ? enc("{}") : null).reason),
    ["key", "plan", "credits", "server", "server", "server", "other"]);
  const e = W.classifyTd(200, enc({ status: "error", code: 429, message: "x" }));
  assert.deepEqual([e.td_code, e.reason], [429, "credits"]);
  assert.equal(W.classifyTd(200, enc({ status: "error", code: 401 })).reason, "key");
});
