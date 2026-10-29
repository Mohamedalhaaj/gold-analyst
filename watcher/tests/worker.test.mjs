// R-AN-91 in worker.js: when the watcher acts, its reads, gates, Twelve Data request, fire and retry, caps and log.
import { test } from "node:test";
import assert from "node:assert/strict";
import { MIN, SECRETS, W, World, at, loadBars, worker_ } from "./helpers.mjs";

const D = "2026-11-04"; // Wednesday, GMT/EST: W1 check 07:30, W1b 09:30, W2 14:00, W2b 16:00, L_d 17:30
const P = "analyst/canary/";
const BARS = loadBars("bars_synthetic.json");

async function check(world, Tstr, lateMs = MIN) {
  const T = at(Tstr);
  world.clock = T + lateMs;
  return W.runCheck(T + MIN, world.env, world.io);
}

function win(name, d = D) {
  return W.scheduledWindows(d).find((w) => w.name === name);
}

function onMain(world, w, status, { terminal = false, reason = null } = {}) {
  world.setMain(`${P}attempts/${w.call_id}_1.json`, { call_id: w.call_id, wake: { kind: w.kind, reason } });
  world.setMain(`${P}calls/${w.call_id}.json`, { call_id: w.call_id, status });
  if (terminal) {
    world.setMain(`${P}terminal/${w.call_id}.json`,
      { call_id: w.call_id, status, attempts: [{ n: 1, path: `${P}attempts/${w.call_id}_1.json` }] });
  }
}

test("acts only on checks of G(d) for model weekdays >= a14_from", async () => {
  const off = new World({ bars: BARS, a14_from: null });
  assert.equal(await check(off, `${D}T09:30:00Z`), null);
  assert.equal(off.requests.length, 2, "only MODE.json and STATE.json");
  assert.equal(off.kv.map.size, 0);
  const later = new World({ bars: BARS, a14_from: "2026-11-05" });
  assert.equal(await check(later, `${D}T09:30:00Z`), null);
  const w = new World({ bars: BARS });
  for (const t of ["2026-11-04T18:00:00Z", "2026-11-07T12:00:00Z", "2026-11-08T22:00:00Z", "2026-11-06T22:30:00Z"]) {
    assert.equal(await check(w, t), null, t);
  }
  assert.equal(w.requests.length, 0, "no read outside G(d)");
  const sunday = await check(w, "2026-11-08T22:30:00Z"); // Monday 2026-11-09's first check, at the Sunday open
  assert.equal(sunday.model_day, "2026-11-09");
  assert.equal(sunday.check_utc, "2026-11-08T22:30:00Z");
  assert.ok(!sunday.decision.startsWith("td_"));
  assert.equal(w.fires.length, sunday.fired ? 1 : 0);
});

test("mode off, MODE.json unreadable, calls disabled, STATE.json unreadable: logged, no request, no fire", async () => {
  const cases = [
    [new World({ bars: BARS, mode: "off" }), "mode_off"],
    [new World({ bars: BARS, calls_enabled: false }), "calls_disabled"],
  ];
  const noMode = new World({ bars: BARS });
  noMode.mainFiles.delete("analyst/MODE.json");
  cases.push([noMode, "mode_off"]);
  const noState = new World({ bars: BARS });
  noState.mainFiles.delete("analyst/STATE.json");
  cases.push([noState, "calls_disabled"]);
  for (const [world, decision] of cases) {
    const e = await check(world, `${D}T09:30:00Z`);
    assert.deepEqual([e.decision, e.fired, e.http, e.tries], [decision, false, null, 0]);
    assert.equal(world.tdRequests().length + world.fires.length, 0);
    assert.deepEqual((await world.log(D)).map((x) => x.decision), [decision]);
  }
});

test("W1 and W2 checks: `fixed`, no fire (their own routines fire them)", async () => {
  const world = new World({ bars: BARS });
  for (const t of [`${D}T07:30:00Z`, `${D}T14:00:00Z`]) {
    const e = await check(world, t);
    assert.deepEqual([e.decision, e.fired, e.tries], ["fixed", false, 0]);
  }
  assert.equal(world.fires.length + world.tdRequests().length, 0);
});

test("W1b: one POST to FIRE_URL with the token, the routine headers and no text; no Twelve Data request", async () => {
  const world = new World({ bars: BARS });
  onMain(world, win("W1"), "stay_out");
  const e = await check(world, `${D}T09:30:00Z`);
  assert.deepEqual([e.decision, e.fired, e.http, e.tries], ["W1b", true, 200, 1]);
  assert.equal(world.fires.length, 1);
  const f = world.fires[0];
  assert.equal(f.headers.Authorization, `Bearer ${SECRETS.FIRE_TOKEN}`);
  assert.equal(f.headers["anthropic-beta"], "experimental-cc-routine-2026-04-01");
  assert.equal(f.headers["anthropic-version"], "2023-06-01");
  assert.equal(f.body, "{}");
  assert.equal(world.tdRequests().length, 0);
  // a second run of the same check (a repeated cron delivery) does nothing
  assert.equal(await check(world, `${D}T09:30:00Z`, 2 * MIN), null);
  assert.equal(world.fires.length, 1);
  assert.equal((await world.log(D)).length, 1);
});

test("open trade: read from the call branch before the merge; exit = entry of the next window is not open", async () => {
  const world = new World({ bars: BARS });
  world.wake(win("W1"), null, true); // a buy or a sell, on its branches only
  const a = await check(world, `${D}T09:30:00Z`);
  assert.deepEqual([a.decision, a.fired], ["trade_open", false]);
  assert.equal((await check(world, `${D}T11:00:00Z`)).decision, "trade_open"); // W1 exits 12:00 > 11:30
  const b = await check(world, `${D}T11:30:00Z`); // exit 12:00 = entry: not open (A14-D4)
  assert.notEqual(b.decision, "trade_open");
  assert.equal(world.fires.length, b.fired ? 1 : 0);
  // terminal on main decides over the call file
  const t = new World({ bars: BARS });
  t.wake(win("W1"), null, true);
  t.setMain(`${P}terminal/${win("W1").call_id}.json`, { status: "stay_out", attempts: [{ n: 1 }] });
  assert.equal((await check(t, `${D}T09:30:00Z`)).decision, "W1b");
  const c = new World({ bars: BARS });
  onMain(c, win("W1"), "called", { terminal: true });
  assert.equal((await check(c, `${D}T09:30:00Z`)).decision, "trade_open");
});

test("A15: from a15_from no check is `trade_open` and no call file is read; before it the A14 rule holds", async () => {
  const world = new World({ bars: BARS, a15_from: D });
  world.wake(win("W1"), null, true); // a buy or a sell, open until 12:00
  const a = await check(world, `${D}T09:30:00Z`); // W1b during W1's trade: fired, a review in its session
  assert.deepEqual([a.decision, a.fired], ["W1b", true]);
  const callReads = world.requests.filter((r) => r.url.includes("/calls/"));
  assert.equal(callReads.length, 0);
  const c = new World({ bars: BARS, a15_from: D });
  onMain(c, win("W1"), "called", { terminal: true });
  assert.equal((await check(c, `${D}T09:30:00Z`)).decision, "W1b");
  const before = new World({ bars: BARS, a15_from: "2026-11-05" }); // a15_from later than the day: A14
  before.wake(win("W1"), null, true);
  assert.equal((await check(before, `${D}T09:30:00Z`)).decision, "trade_open");
});

async function seedTriggers(world, checks, withAttempts) {
  const entries = checks.map((t) => ({ check_utc: t, model_day: D, decision: "trigger:y_low", fired: true,
    http: 200, tries: 1, utc: t }));
  await world.kv.put(`log/${D}`, JSON.stringify(entries));
  if (withAttempts) {
    for (const t of checks) onMain(world, W.triggerWindow(D, at(t)), "stay_out", { terminal: true, reason: "y_low" });
  }
}

const EARLY = ["2026-11-03T22:30:00Z", "2026-11-04T00:00:00Z", "2026-11-04T01:30:00Z", "2026-11-04T03:00:00Z",
  "2026-11-04T04:30:00Z", "2026-11-04T06:00:00Z"];

test("day cap: six wakes → `day_cap`; fire cap: six fires logged → `fire_cap`, nothing sent", async () => {
  const cap = new World({ bars: BARS });
  await seedTriggers(cap, EARLY.slice(0, 5), true);
  onMain(cap, win("W1"), "stay_out", { terminal: true });
  const e = await check(cap, `${D}T09:30:00Z`);
  assert.deepEqual([e.decision, e.fired], ["day_cap", false]);
  const five = new World({ bars: BARS });
  await seedTriggers(five, EARLY.slice(0, 5), true);
  assert.equal((await check(five, `${D}T09:30:00Z`)).decision, "W1b", "five wakes: not capped");
  const fc = new World({ bars: BARS });
  await seedTriggers(fc, EARLY, false); // six fires that never got an attempt
  const f = await check(fc, `${D}T09:30:00Z`);
  assert.deepEqual([f.decision, f.fired, f.tries], ["fire_cap", false, 0]);
  assert.equal(fc.fires.length, 0);
});

test("consumed conditions come from the attempts' wake.reason", async () => {
  // y_low consumed by a trigger at 06:00 (g = 360 at 12:00), every other condition false or consumed too
  const world = new World({ bars: BARS });
  await seedTriggers(world, ["2026-11-04T06:00:00Z"], true);
  const e = await check(world, `${D}T12:00:00Z`);
  assert.notEqual(e.decision, "trigger:y_low");
  assert.equal(world.tdRequests().length, 1);
});

test("fire retry: once, 60 s later, after 429, 5xx or a connection error, only while now <= T + 10 min", async () => {
  const run = async (replies, lateMs = MIN) => {
    const world = new World({ bars: BARS });
    world.fireReplies = [...replies];
    const e = await check(world, `${D}T09:30:00Z`, lateMs);
    return { e, world };
  };
  let { e, world } = await run([429, 200]);
  assert.deepEqual([e.fired, e.http, e.tries], [true, 200, 2]);
  assert.equal(world.fires[1].at - world.fires[0].at, 60_000);
  ({ e } = await run([503, 502]));
  assert.deepEqual([e.fired, e.http, e.tries], [false, 502, 2]);
  ({ e } = await run(["error", 201]));
  assert.deepEqual([e.fired, e.http, e.tries], [true, 201, 2]);
  ({ e } = await run([400, 200]));
  assert.deepEqual([e.fired, e.http, e.tries], [false, 400, 1], "no retry after another 4xx");
  ({ e } = await run([429, 200], 9.5 * MIN));
  assert.deepEqual([e.fired, e.http, e.tries], [false, 429, 1], "the retry would be after T + 10 min");
  ({ e, world } = await run([200], 10.5 * MIN));
  assert.deepEqual([e.decision, e.fired, e.http, e.tries], ["W1b", false, null, 0], "a late run never fires");
  assert.equal(world.fires.length, 0);
});

test("Twelve Data: the URL and headers, 429 not retried, 5xx and connection errors retried once 8 s later", async () => {
  const T = `${D}T12:00:00Z`;
  const json = (o, s = 200) => () => new Response(JSON.stringify(o), { status: s });
  const run = async (replies) => {
    const world = new World({ bars: BARS });
    world.tdReplies = [...replies];
    const e = await check(world, T);
    return { e, world, td: world.tdRequests() };
  };
  let { e, world, td } = await run([]);
  assert.equal(td.length, 1);
  assert.equal(td[0].url, W.tdUrl(D, at(T)));
  assert.deepEqual(td[0].headers, { Authorization: `apikey ${SECRETS.TD_API_KEY}`,
    "User-Agent": "Mozilla/5.0 (gold-analyst context)" });
  assert.ok(!e.decision.startsWith("td_"));
  assert.equal(world.fires.length, e.fired ? 1 : 0);
  ({ e, td, world } = await run([json({ status: "error", code: 429 }, 429), json({})]));
  assert.deepEqual([e.decision, e.fired, td.length, world.fires.length], ["td_credits", false, 1, 0]);
  ({ e, td } = await run([json({}, 500)]));
  assert.equal(td.length, 2);
  assert.ok(td[1].at - td[0].at >= 8_000);
  assert.ok(!e.decision.startsWith("td_"));
  ({ e, td } = await run(["error", json({ status: "error", code: 401 }, 401)]));
  assert.deepEqual([e.decision, td.length], ["td_key", 2]);
  ({ e, td } = await run([json({}, 503), json({}, 503)]));
  assert.deepEqual([e.decision, td.length], ["td_server", 2]);
  ({ e } = await run([json({ status: "ok", values: [{ datetime: `${D} 11:15:00`, open: "1", high: "1",
    low: "1", close: "1" }] })]));
  assert.equal(e.decision, "td_invalid");
  ({ e } = await run([json({ status: "error", code: 403 })]));
  assert.equal(e.decision, "td_plan");
});

test("missing bars: no bar closing at T gives no trigger, and a scheduled check still fires", async () => {
  const world = new World({ bars: BARS.filter((b) => b.t !== at(`${D}T11:30:00Z`) && b.t !== at(`${D}T09:00:00Z`)) });
  const e = await check(world, `${D}T12:00:00Z`);
  assert.equal(e.decision, "none");
  assert.equal(world.tdRequests().length, 1);
  assert.equal((await check(world, `${D}T09:30:00Z`)).decision, "W1b");
});

test("GET /log?day= returns the day's entries and nothing secret; every other route is 404", async () => {
  const world = new World({ bars: BARS });
  await check(world, `${D}T09:30:00Z`);
  await check(world, `${D}T12:00:00Z`);
  const get = (u, method = "GET") => worker_.fetch(new Request(`https://gold-analyst-watcher.example${u}`, { method }),
    world.env);
  const r = await get(`/log?day=${D}`);
  assert.equal(r.status, 200);
  assert.match(r.headers.get("content-type"), /application\/json/);
  const body = await r.text();
  const entries = JSON.parse(body);
  assert.deepEqual(entries.map((x) => x.check_utc), [`${D}T09:30:00Z`, `${D}T12:00:00Z`]);
  for (const x of entries) {
    assert.deepEqual(Object.keys(x), ["check_utc", "model_day", "decision", "fired", "http", "tries", "utc"]);
  }
  assert.deepEqual(await (await get("/log?day=2026-11-05")).json(), []);
  assert.equal((await get("/log?day=2026-13-01")).status, 400);
  assert.equal((await get("/log")).status, 400);
  assert.equal((await get("/")).status, 404);
  assert.equal((await get("/status")).status, 404);
  assert.equal((await get(`/log?day=${D}`, "POST")).status, 404);
  for (const s of Object.values(SECRETS)) {
    assert.ok(!body.includes(s));
    assert.ok(![...world.kv.map.values()].join("").includes(s));
  }
});
