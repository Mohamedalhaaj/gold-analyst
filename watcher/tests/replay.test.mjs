// The watcher, unchanged, replayed check by check over two fixtures against the Python reference (ref_wakes.py):
// recorded bars (Dukascopy mid M30, 2026-09-29..10-26, across the UK clock change) and synthetic bars
// (2026-10-26..11-27, across the US clock change of 2026-11-01, Sunday opens, missing bars, Thanksgiving).
// The fake inbox moves each session's files from its branches to main one check later and writes the terminal two
// checks later, so branch, main and terminal reads are all exercised.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { join } from "node:path";
import { HERE, MIN, W, World, iso, loadBars, trades } from "./helpers.mjs";

async function replay(barsName, expectedName) {
  const exp = JSON.parse(readFileSync(join(HERE, "fixtures", expectedName), "utf8"));
  const world = new World({ bars: loadBars(barsName), a14_from: exp.a14_from });
  const P = "analyst/canary/";
  const got = {};
  for (const d of Object.keys(exp.days).sort()) {
    const woken = []; // {w, reason, called, k}
    const checks = [];
    const grid = W.grid(d);
    for (let k = 0; k < grid.length; k++) {
      const T = grid[k];
      // the merge: files one check old reach main; the terminal follows one check later
      for (const x of woken) {
        const cid = x.w.call_id;
        if (k - x.k === 1 && x.attempted) {
          world.setMain(`${P}attempts/${cid}_1.json`, x.files.attempt);
          world.setMain(`${P}calls/${cid}.json`, x.files.call);
        }
        if (k - x.k === 2) {
          world.setMain(`${P}terminal/${cid}.json`, {
            schema: "analyst-terminal/1", call_id: cid,
            status: x.attempted ? x.files.call.status : "skipped:holiday",
            attempts: x.attempted ? [{ n: 1, path: `${P}attempts/${cid}_1.json` }] : [],
          });
        }
      }
      world.clock = T + MIN;
      const entry = await W.runCheck(T + MIN, world.env, world.io);
      assert.ok(entry, `no entry at ${iso(T)}`);
      checks.push([entry.check_utc, entry.decision]);
      // the session: the window R gave, unless `start` refuses it as a holiday of its own exchange
      let w = null;
      let reason = null;
      if (entry.decision === "fixed" || ((entry.decision === "W1b" || entry.decision === "W2b") && entry.fired)) {
        w = W.scheduledWindows(d).find((x) => x.check === T);
      } else if (entry.decision.startsWith("trigger:") && entry.fired) {
        w = W.triggerWindow(d, T);
        reason = entry.decision.slice(8);
      }
      if (w) {
        const refused = w.exchange !== null && W.isHolidayFor(d, w.exchange);
        const called = !refused && trades(w.call_id);
        const files = refused ? null : world.wake(w, reason, called);
        woken.push({ w, reason, called, attempted: !refused, files, k });
      }
    }
    got[d] = {
      checks,
      wakes: woken.filter((x) => x.attempted).map((x) => [x.w.call_id, x.w.kind, x.reason, x.called]),
    };
  }
  return { exp, got, world };
}

for (const [bars, expected] of [
  ["bars_recorded.json", "expected_recorded.json"],
  ["bars_synthetic.json", "expected_synthetic.json"],
]) {
  test(`replay ${bars}: every check's decision and every wake equal the Python reference`, async () => {
    const { exp, got, world } = await replay(bars, expected);
    for (const d of Object.keys(exp.days)) {
      assert.deepEqual(got[d].checks, exp.days[d].checks, `decisions of ${d}`);
      assert.deepEqual(got[d].wakes, exp.days[d].wakes, `wakes of ${d}`);
    }
    // the KV log holds exactly the checks, and no secret anywhere
    for (const d of Object.keys(exp.days)) {
      const log = await world.log(d);
      assert.deepEqual(log.map((e) => [e.check_utc, e.decision]), exp.days[d].checks);
    }
    const all = [...world.kv.map.values()].join("\n");
    for (const s of [world.env.TD_API_KEY, world.env.FIRE_TOKEN, world.env.FIRE_URL]) assert.ok(!all.includes(s));
    // Twelve Data: one request per check where a trigger could be due, never more than 39 a day
    const perDay = {};
    for (const r of world.tdRequests()) {
      const T = Date.parse(new URL(r.url).searchParams.get("end_date") + "Z");
      perDay[W.tday(T)] = (perDay[W.tday(T)] || 0) + 1;
    }
    for (const n of Object.values(perDay)) assert.ok(n < 39);
  });
}

test("the committed expected files are what ref_wakes.py computes now", (t) => {
  const py = spawnSync("python3", ["--version"]);
  if (py.status !== 0) {
    t.skip("python3 not available");
    return;
  }
  for (const [bars, a, b, expected] of [
    ["bars_recorded.json", "2026-09-29", "2026-10-26", "expected_recorded.json"],
    ["bars_synthetic.json", "2026-10-26", "2026-11-27", "expected_synthetic.json"],
  ]) {
    const r = spawnSync("python3", ["-I", join(HERE, "ref_wakes.py"), "expected", join(HERE, "fixtures", bars), a, b],
      { encoding: "utf8", maxBuffer: 1 << 26 });
    assert.equal(r.status, 0, r.stderr);
    assert.equal(r.stdout, readFileSync(join(HERE, "fixtures", expected), "utf8"));
  }
  const s = spawnSync("python3", ["-I", join(HERE, "ref_wakes.py"), "synthetic", "20261101"],
    { encoding: "utf8", maxBuffer: 1 << 26 });
  assert.equal(s.stdout, readFileSync(join(HERE, "fixtures", "bars_synthetic.json"), "utf8"));
});
