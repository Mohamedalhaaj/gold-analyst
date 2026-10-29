// Test helpers: a fake inbox (raw.githubusercontent.com), a fake Twelve Data, a fake fire endpoint, a fake KV and a
// fake clock, so worker.js runs unchanged under node:test.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import worker from "../worker.js";

export const W = worker.internals;
export const worker_ = worker;
export const MIN = 60_000;
export const HERE = dirname(fileURLToPath(import.meta.url));
export const WATCHER = join(HERE, "..");
export const REPO = join(HERE, "..", "..");
export const RAW = `https://raw.githubusercontent.com/${W.INBOX_REPO}/refs/heads/`;
export const SECRETS = {
  TD_API_KEY: "td-key-SECRET-0123456789",
  FIRE_URL: "https://api.anthropic.com/v1/claude_code/routines/trig_SECRET_0123/fire",
  FIRE_TOKEN: "fire-token-SECRET-0123456789",
};

export const at = (s) => Date.parse(s);
export const iso = (ms) => new Date(ms).toISOString().slice(0, 19) + "Z";

export function loadBars(name) {
  const doc = JSON.parse(readFileSync(join(HERE, "fixtures", name), "utf8"));
  return doc.bars.map(([t, o, h, l, c]) => ({ t: Date.parse(t), o, h, l, c }));
}

export function trades(callId) {
  let h = 0;
  for (const ch of callId) h = (h * 31 + ch.charCodeAt(0)) % 1000003;
  return h % 5 < 2;
}

export class FakeKV {
  constructor() { this.map = new Map(); }
  async get(key, opts) {
    const v = this.map.get(key);
    if (v === undefined) return null;
    return opts && opts.type === "json" ? JSON.parse(v) : v;
  }
  async put(key, value) { this.map.set(key, String(value)); }
}

const json = (obj, status = 200) => new Response(JSON.stringify(obj), { status });

export class World {
  constructor({ bars = [], mode = "canary", a14_from = "2026-10-26", a15_from = undefined, calls_enabled = true } = {}) {
    this.bars = bars;
    this.mainFiles = new Map();
    this.branchFiles = new Map();
    this.mainFiles.set("analyst/MODE.json", { mode, a14_from, a15_from, canary_from: "2026-10-26" });
    this.mainFiles.set("analyst/STATE.json", { calls_enabled });
    this.kv = new FakeKV();
    this.env = { ...SECRETS, WATCHER_LOG: this.kv };
    this.clock = 0;
    this.requests = [];
    this.fires = [];
    this.fireReplies = []; // statuses (or "error") for the next POSTs; default 200
    this.tdReplies = []; // functions (url) => Response | "error", for the next TD tries; default: the bars
    this.io = {
      fetch: (url, init) => this.fetch(url, init),
      now: () => this.clock,
      sleep: async (ms) => { this.clock += ms; },
    };
  }

  setMain(path, obj) { this.mainFiles.set(path, obj); }
  setBranch(branch, path, obj) { this.branchFiles.set(`${branch}\n${path}`, obj); }

  tdBody(url) {
    const q = new URL(url).searchParams;
    const A = Date.parse(q.get("start_date") + "Z");
    const B = Date.parse(q.get("end_date") + "Z");
    const values = this.bars.filter((b) => b.t >= A && b.t <= B).map((b) => ({
      datetime: iso(b.t).replace("T", " ").slice(0, 19),
      open: String(b.o), high: String(b.h), low: String(b.l), close: String(b.c), volume: "0",
    }));
    return json({ meta: { symbol: "XAU/USD", interval: "30min" }, values, status: "ok" });
  }

  async fetch(url, init = {}) {
    const method = init.method || "GET";
    this.requests.push({ url, method, headers: { ...(init.headers || {}) }, body: init.body ?? null, at: this.clock });
    if (url.startsWith(RAW)) {
      const rest = url.slice(RAW.length);
      const i = rest.indexOf("/analyst/");
      const ref = rest.slice(0, i);
      const path = rest.slice(i + 1);
      const obj = ref === "main" ? this.mainFiles.get(path) : this.branchFiles.get(`${ref}\n${path}`);
      return obj === undefined ? new Response("404: Not Found", { status: 404 }) : json(obj);
    }
    if (url.startsWith("https://api.twelvedata.com/")) {
      const r = this.tdReplies.shift();
      if (r === "error") throw new TypeError("network");
      return r ? r(url) : this.tdBody(url);
    }
    if (url === SECRETS.FIRE_URL && method === "POST") {
      this.fires.push({ at: this.clock, headers: init.headers, body: init.body });
      const r = this.fireReplies.shift() ?? 200;
      if (r === "error") throw new TypeError("network");
      return json({ type: "routine_fire" }, r);
    }
    throw new Error(`unexpected fetch ${method} ${url}`);
  }

  tdRequests() { return this.requests.filter((r) => r.url.startsWith("https://api.twelvedata.com/")); }

  async log(day) { return (await this.kv.get(`log/${day}`, { type: "json" })) || []; }

  /** Writes what a session writes for window w (n = 1 attempt and call file) to its branches. */
  wake(w, reason, called) {
    const P = "analyst/canary/";
    const attempt = { schema: "analyst-attempt/2", call_id: w.call_id, n: 1,
      wake: { kind: w.kind, reason, check_utc: iso(w.check), td: null } };
    const call = { schema: "analyst-call/1", call_id: w.call_id, status: called ? "called" : "stay_out" };
    this.setBranch(`claude/analyst-canary-attempt-${w.call_id}-1`, `${P}attempts/${w.call_id}_1.json`, attempt);
    this.setBranch(`claude/analyst-canary-call-${w.call_id}`, `${P}calls/${w.call_id}.json`, call);
    return { attempt, call };
  }
}
