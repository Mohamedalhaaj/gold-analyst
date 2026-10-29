// gold-analyst watcher: register R-AN-89 (the trigger rule) and R-AN-91 (the watcher), §16.18 A14. FROZEN with the
// analyst (FROZEN_ANALYST.sha256); deployed byte for byte (wrangler.toml no_bundle), so WATCHER_CONFIG.json's
// script_sha256 is the sha256 of this file.
//
// It runs no Claude and no model. Every 30 minutes (cron "1,31 * * * *") it takes the check T = the scheduled time
// floored to the UTC half hour and acts only when d = tday(T) is a model weekday >= MODE.json a14_from and T is in
// G(d). It reads the inbox's public files (raw.githubusercontent.com, no credential), applies R(T), asks Twelve Data
// for XAU/USD 30-minute bars only when a trigger can be due, fires the routine analyst-wake (W1b, W2b, triggers;
// never W1 or W2) and writes one log entry per check to Workers KV. `start` is authoritative (R-AN-90); the watcher
// only avoids fires its gates would refuse.
//
// §16.18 A15 (R-AN-91, R-AN-89): for model days >= MODE.json a15_from the watcher does not form O(T) and never
// decides `trade_open`: its decision is R(T) without step 1, so a window during an open trade is fired like any other
// and becomes a review in its session. Its reads (for E(T)), request, fire, 6-a-day limit, log and secrets are
// unchanged; days before a15_from keep the A14 rule.
//
// Secrets (Worker secrets, never in a file, a log line or a response): TD_API_KEY, FIRE_URL, FIRE_TOKEN.
// Binding: WATCHER_LOG (Workers KV). The only route is GET /log?day=YYYY-MM-DD.

const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

const INBOX_REPO = "Mohamedalhaaj/gold-analyst-inbox"; // analyst/PINS.json inbox_repo (a build test checks it)
const RAW = `https://raw.githubusercontent.com/${INBOX_REPO}/refs/heads/`;
const TD_BASE = "https://api.twelvedata.com/time_series";
const TD_UA = "Mozilla/5.0 (gold-analyst context)"; // R-AN-87's only header besides the key
const TD_TRY_TIMEOUT_MS = 10_000;
const TD_TRY_SPACING_MS = 8_000;
const TD_MAX_BODY = 262_144;
const FIRE_BETA = "experimental-cc-routine-2026-04-01";
const FIRE_VERSION = "2023-06-01";
const FIRE_RETRY_AFTER_MS = 60_000;
const FIRE_LAST_MS = 10 * MIN; // a try only while now <= T + 10 min
const FIRE_CAP = 6; // fires a model day (a guard; R's cap of 6 wakes is the rule)
const DAY_CAP = 6; // R-AN-89 step 2
const READ_TIMEOUT_MS = 10_000;
const CONDITIONS = ["asia_up", "asia_dn", "y_high", "y_low", "move"];
const LONDON = "Europe/London";
const NEW_YORK = "America/New_York";

// analyst/tables/holidays.csv (R-AN-9), byte for byte; a build test compares it with the table.
const HOLIDAYS_CSV = `date,exchange,kind,close_utc
2026-11-26,NYSE,full,
2026-11-27,NYSE,early,2026-11-27T18:00:00Z
2026-12-24,LSE,early,2026-12-24T12:30:00Z
2026-12-24,NYSE,early,2026-12-24T18:00:00Z
2026-12-25,LSE,full,
2026-12-25,NYSE,full,
2026-12-28,LSE,full,
2026-12-31,LSE,early,2026-12-31T12:30:00Z
2027-01-01,LSE,full,
2027-01-01,NYSE,full,
2027-01-18,NYSE,full,
2027-02-15,NYSE,full,
2027-03-26,LSE,full,
2027-03-26,NYSE,full,
2027-03-29,LSE,full,
2027-05-03,LSE,full,
2027-05-31,LSE,full,
2027-05-31,NYSE,full,
2027-06-18,NYSE,full,
2027-07-05,NYSE,full,
2027-08-30,LSE,full,
2027-09-06,NYSE,full,
2027-11-25,NYSE,full,
2027-11-26,NYSE,early,2027-11-26T18:00:00Z
2027-12-24,LSE,early,2027-12-24T12:30:00Z
2027-12-24,NYSE,full,
2027-12-27,LSE,full,
2027-12-28,LSE,full,
2027-12-31,LSE,early,2027-12-31T12:30:00Z
`;

const HOLIDAYS = (() => {
  const m = new Map();
  for (const line of HOLIDAYS_CSV.trim().split("\n").slice(1)) {
    const [date, exchange] = line.split(",");
    if (!m.has(date)) m.set(date, new Set());
    m.get(date).add(exchange);
  }
  return m;
})();

// ---------------------------------------------------------------------------------------------------------------
// Time (UTC instants are milliseconds since the epoch; a day is "YYYY-MM-DD")

const ymd = (ms) => new Date(ms).toISOString().slice(0, 10);
const dayStart = (d) => Date.parse(`${d}T00:00:00Z`);
const addDays = (d, n) => ymd(dayStart(d) + n * DAY);
const isoZ = (ms) => new Date(ms).toISOString().slice(0, 19) + "Z"; // R-C4: YYYY-MM-DDTHH:MM:SSZ
const isoNoZone = (ms) => new Date(ms).toISOString().slice(0, 19); // R-AN-87 URL form
const hhmm = (ms) => new Date(ms).toISOString().slice(11, 16).replace(":", "");

/** tday(x) = the UTC date of x + 2 h (R-AN-89). */
function tday(ms) {
  return ymd(ms + 2 * HOUR);
}

function isWeekday(d) {
  const w = new Date(dayStart(d)).getUTCDay();
  return w >= 1 && w <= 5;
}

const FORMATTERS = new Map();
function tzOffsetMin(tz, ms) {
  let f = FORMATTERS.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", {
      timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
    FORMATTERS.set(tz, f);
  }
  const p = {};
  for (const x of f.formatToParts(new Date(ms))) p[x.type] = x.value;
  const wall = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second);
  return Math.round((wall - Math.floor(ms / 1000) * 1000) / MIN);
}

/** The UTC instant of wall time hh:mm on date d in zone tz; throws on a skipped or repeated wall time. */
function localToUtc(d, hh, mm, tz) {
  const naive = dayStart(d) + (hh * 60 + mm) * MIN;
  const candidates = new Set();
  for (const probe of [naive - DAY / 2, naive, naive + DAY / 2]) {
    const utc = naive - tzOffsetMin(tz, probe) * MIN;
    if (utc + tzOffsetMin(tz, utc) * MIN === naive) candidates.add(utc);
  }
  if (candidates.size !== 1) throw new Error(`wall time ${d} ${hh}:${mm} ${tz} is skipped or repeated`);
  return [...candidates][0];
}

/** model_to_utc((d+1) 00:00): the model clock is New York + 7 h, so this is 17:00 New York on d. */
function modelDayEnd(d) {
  return localToUtc(d, 17, 0, NEW_YORK);
}

/** L_d = model_to_utc((d+1) 00:00) − 270 min. */
function lastCheck(d) {
  return modelDayEnd(d) - 270 * MIN;
}

/** G(d): 22:30 UTC of d − 1 day + k·30 min, up to L_d (37 checks on EDT, 39 on EST). */
function grid(d) {
  const out = [];
  const L = lastCheck(d);
  for (let T = dayStart(d) - 90 * MIN; T <= L; T += 30 * MIN) out.push(T);
  return out;
}

function inGrid(d, T) {
  if (!isWeekday(d)) return false;
  const first = dayStart(d) - 90 * MIN;
  return T >= first && T <= lastCheck(d) && (T - first) % (30 * MIN) === 0;
}

function mkWindow(d, name, entry, kind, exchange) {
  return { call_id: `${d}_${name}`, name, kind, exchange, entry, exit: entry + 240 * MIN, check: entry - 30 * MIN };
}

/** W1, W1b, W2, W2b of model day d (R-AN-8): W1b/W2b enter 120 min after their parents; every exit is entry + 4 h. */
function scheduledWindows(d) {
  const w1 = localToUtc(d, 8, 0, LONDON);
  const w2 = localToUtc(d, 9, 30, NEW_YORK);
  return [
    mkWindow(d, "W1", w1, "fixed", "LSE"),
    mkWindow(d, "W1b", w1 + 120 * MIN, "second_look", "LSE"),
    mkWindow(d, "W2", w2, "fixed", "NYSE"),
    mkWindow(d, "W2b", w2 + 120 * MIN, "second_look", "NYSE"),
  ];
}

function triggerWindow(d, T) {
  return mkWindow(d, `T${hhmm(T)}`, T + 30 * MIN, "trigger", null);
}

/** Step 4: d has a row of either exchange in the holiday table. */
function isHolidayDay(d) {
  return HOLIDAYS.has(d);
}

function isHolidayFor(d, exchange) {
  const s = HOLIDAYS.get(d);
  return !!s && s.has(exchange);
}

// ---------------------------------------------------------------------------------------------------------------
// The rule (R-AN-89)

/** Python's statistics.median on finite doubles. */
function median(xs) {
  const s = [...xs].sort((a, b) => a - b);
  const n = s.length;
  const i = Math.floor(n / 2);
  return n % 2 === 1 ? s[i] : (s[i - 1] + s[i]) / 2;
}

/**
 * The five conditions at check T of model day d, from bars [{t, o, h, l, c}] (t = bar start, ms).
 * Only bars with t <= T − 30 min are used. All false when bar_T is absent or R20 is undefined.
 */
function conditionsAt(T, d, bars) {
  const byDay = new Map();
  for (const b of bars) {
    if (b.t > T - 30 * MIN) continue;
    const e = tday(b.t);
    if (!byDay.has(e)) byDay.set(e, []);
    byDay.get(e).push(b);
  }
  const none = { asia_up: false, asia_dn: false, y_high: false, y_low: false, move: false };
  const prior = [...byDay.keys()].filter((e) => e < d && isWeekday(e)).sort();
  if (prior.length < 20) return { ...none, R20: null };
  const range = (bs) => Math.max(...bs.map((b) => b.h)) - Math.min(...bs.map((b) => b.l));
  const R20 = median(prior.slice(-20).map((e) => range(byDay.get(e))));
  const yb = byDay.get(prior[prior.length - 1]);
  const y_h = Math.max(...yb.map((b) => b.h));
  const y_l = Math.min(...yb.map((b) => b.l));
  const today = (byDay.get(d) || []).slice().sort((a, b) => a.t - b.t);
  const barT = today.find((b) => b.t === T - 30 * MIN);
  if (!barT) return { ...none, R20 };
  const asiaEnd = dayStart(d) + 7 * HOUR;
  const asia = today.filter((b) => b.t < asiaEnd);
  const asiaDefined = asia.length > 0;
  const asia_h = asiaDefined ? Math.max(...asia.map((b) => b.h)) : null;
  const asia_l = asiaDefined ? Math.min(...asia.map((b) => b.l)) : null;
  const before = today.filter((b) => b.t <= T - 90 * MIN);
  const c_prev = before.length ? before[before.length - 1].c : null;
  const c_T = barT.c;
  const after = T >= asiaEnd;
  return {
    asia_up: after && asiaDefined && c_T > asia_h,
    asia_dn: after && asiaDefined && c_T < asia_l,
    y_high: barT.h >= y_h - 0.1 * R20,
    y_low: barT.l <= y_l + 0.1 * R20,
    move: c_prev !== null && Math.abs(c_T - c_prev) >= 0.3 * R20,
    R20,
  };
}

/**
 * R(T) for T in G(d). E: windows of d with a check earlier than T that have an attempt ({check, kind, reason});
 * O: windows of d with check < T whose call is a buy or a sell and whose exit_utc > T + 30 min.
 * conds: the five conditions at T, or undefined when not yet known (then a due trigger gives "trigger_due").
 * Returns {result: "trade_open" | "day_cap" | "window" | "none" | "trigger_due", window?, why?, g}.
 */
function ruleR(T, d, E, O, conds) {
  if (O.length > 0) return { result: "trade_open" };
  if (E.length >= DAY_CAP) return { result: "day_cap" };
  const sched = scheduledWindows(d).find((w) => w.check === T);
  if (sched) return { result: "window", window: { ...sched, reason: null } };
  if (isHolidayDay(d)) return { result: "none", why: "holiday" };
  const g = E.length ? (T - Math.max(...E.map((w) => w.check))) / MIN : Infinity;
  if (g < 60) return { result: "none", why: "gap", g };
  if (conds === undefined) return { result: "trigger_due", g };
  const consumed = new Set(E.filter((w) => w.kind === "trigger").map((w) => w.reason));
  for (const c of CONDITIONS) {
    if (!conds[c] || consumed.has(c)) continue;
    if (c === "move" && g < 120) continue;
    return { result: "window", window: { ...triggerWindow(d, T), reason: c }, g };
  }
  return { result: "none", why: "no_condition", g };
}

// ---------------------------------------------------------------------------------------------------------------
// Twelve Data (R-AN-90's request, R-AN-87's transport and classification, interval 30min)

function tdUrl(d, T) {
  const A = `${addDays(d, -33)}T22:00:00`;
  const B = isoNoZone(T);
  return `${TD_BASE}?symbol=XAU/USD&interval=30min&start_date=${A}&end_date=${B}&timezone=UTC&order=asc&outputsize=5000&dp=5`;
}

const PRICE_RE = /^[0-9]+(\.[0-9]+)?$/;
const DT_RE = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})$/;

function parseTdDatetime(s) {
  if (typeof s !== "string") return null;
  const m = DT_RE.exec(s);
  if (!m) return null;
  const [y, mo, da, h, mi, se] = m.slice(1).map(Number);
  if (se !== 0 || (mi !== 0 && mi !== 30) || h > 23) return null;
  const ms = Date.UTC(y, mo - 1, da, h, mi, se);
  const back = new Date(ms);
  if (back.getUTCFullYear() !== y || back.getUTCMonth() !== mo - 1 || back.getUTCDate() !== da) return null;
  return ms;
}

/** Classifies one response: {status: "ok", bars} or {status: "error", td_code, reason}. */
function classifyTd(http, bodyBytes) {
  let obj = null;
  if (bodyBytes !== null) {
    try {
      const text = new TextDecoder("utf-8", { fatal: true }).decode(bodyBytes);
      const v = JSON.parse(text);
      if (v !== null && typeof v === "object" && !Array.isArray(v)) obj = v;
    } catch {
      obj = null;
    }
  }
  let td_code = http;
  if (obj && obj.status === "error" && Number.isInteger(obj.code)) td_code = obj.code;
  const err = (reason) => ({ status: "error", td_code, reason });
  if (http === 200 && td_code === 200) {
    const bars = validTdValues(obj, bodyBytes);
    if (bars) return { status: "ok", td_code: 200, bars };
    return err("invalid");
  }
  if (td_code === 401) return err("key");
  if (td_code === 403) return err("plan");
  if (td_code === 429) return err("credits");
  if (td_code === 0 || (td_code >= 500 && td_code <= 599)) return err("server");
  return err("other");
}

function validTdValues(obj, bodyBytes) {
  if (!obj || bodyBytes.length > TD_MAX_BODY || obj.status !== "ok" || !Array.isArray(obj.values)) return null;
  const seen = new Set();
  const bars = [];
  for (const v of obj.values) {
    if (v === null || typeof v !== "object" || Array.isArray(v)) return null;
    const t = parseTdDatetime(v.datetime);
    if (t === null || seen.has(t)) return null;
    seen.add(t);
    const px = [v.open, v.high, v.low, v.close];
    if (!px.every((x) => typeof x === "string" && PRICE_RE.test(x))) return null;
    const [o, h, l, c] = px.map(Number);
    if (!(o > 0 && h > 0 && l > 0 && c > 0) || !(l <= Math.min(o, c)) || !(h >= Math.max(o, c))) return null;
    bars.push({ t, o, h, l, c });
  }
  return bars.sort((a, b) => a.t - b.t);
}

/** One request: at most 2 tries (the second only after td_code 0 or 500-599), 10 s a try, 8.0 s between starts. */
async function tdRequest(io, env, d, T) {
  const url = tdUrl(d, T);
  let res = null;
  let lastStart = null;
  for (let tryN = 1; tryN <= 2; tryN++) {
    if (lastStart !== null) {
      const wait = lastStart + TD_TRY_SPACING_MS - io.now();
      if (wait > 0) await io.sleep(wait);
    }
    lastStart = io.now();
    let http = 0;
    let body = null;
    try {
      const r = await io.fetch(url, {
        headers: { Authorization: `apikey ${env.TD_API_KEY}`, "User-Agent": TD_UA },
        signal: AbortSignal.timeout(TD_TRY_TIMEOUT_MS),
      });
      http = r.status;
      body = new Uint8Array(await r.arrayBuffer());
    } catch {
      http = 0;
      body = null;
    }
    res = classifyTd(http, body);
    res.tries = tryN;
    if (res.status === "ok") break;
    if (!(res.td_code === 0 || (res.td_code >= 500 && res.td_code <= 599))) break;
  }
  if (res.status === "ok") res.bars = res.bars.filter((b) => b.t <= T - 30 * MIN);
  return res;
}

// ---------------------------------------------------------------------------------------------------------------
// The inbox (public raw reads; a failed read counts as absent)

async function readJson(io, ref, path) {
  try {
    const r = await io.fetch(`${RAW}${ref}/${path}`, { signal: AbortSignal.timeout(READ_TIMEOUT_MS) });
    if (r.status !== 200) return null;
    const v = await r.json();
    return v !== null && typeof v === "object" && !Array.isArray(v) ? v : null;
  } catch {
    return null;
  }
}

function prefix(mode) {
  return mode === "canary" ? "analyst/canary/" : "analyst/";
}

/**
 * What the watcher can see of window w: attempted (an attempt record it can read), called (terminal on main is
 * `called`, or with no terminal its call file on main, else on the call branch, has status `called`) and the
 * attempt's wake.reason. Reads stop early where the answer is already known; with `noTrades` (a model day >=
 * a15_from) no call file is read, since O(T) is not formed.
 */
async function readWindow(io, mode, w, T, noTrades = false) {
  const P = prefix(mode);
  const attemptPath = `${P}attempts/${w.call_id}_1.json`;
  const callPath = `${P}calls/${w.call_id}.json`;
  const out = { attempted: false, called: false, reason: null };
  const term = await readJson(io, "main", `${P}terminal/${w.call_id}.json`);
  let attempt = null;
  if (term) {
    out.called = term.status === "called";
    out.attempted = Array.isArray(term.attempts) && term.attempts.length > 0;
    if (out.attempted && w.kind === "trigger") attempt = await readJson(io, "main", attemptPath);
  } else {
    attempt = (await readJson(io, "main", attemptPath))
      || (await readJson(io, `claude/analyst-${mode}-attempt-${w.call_id}-1`, attemptPath));
    out.attempted = !!attempt;
    if (out.attempted && !noTrades && w.exit > T + 30 * MIN) {
      const call = (await readJson(io, "main", callPath))
        || (await readJson(io, `claude/analyst-${mode}-call-${w.call_id}`, callPath));
      out.called = !!call && call.status === "called";
    }
  }
  if (attempt && attempt.wake && typeof attempt.wake.reason === "string") out.reason = attempt.wake.reason;
  return out;
}

// ---------------------------------------------------------------------------------------------------------------
// Fire (one POST, no text; one retry after 429, 5xx or a connection error, 60 s later, while now <= T + 10 min)

async function fire(io, env, T) {
  let http = null;
  let tries = 0;
  while (tries < 2) {
    if (io.now() > T + FIRE_LAST_MS) break;
    tries += 1;
    try {
      const r = await io.fetch(env.FIRE_URL, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.FIRE_TOKEN}`,
          "anthropic-beta": FIRE_BETA,
          "anthropic-version": FIRE_VERSION,
          "Content-Type": "application/json",
        },
        body: "{}",
        signal: AbortSignal.timeout(15_000),
      });
      http = r.status;
      try { await r.arrayBuffer(); } catch { /* the body is not kept */ }
    } catch {
      http = null;
    }
    if (http !== null && http >= 200 && http <= 299) return { fired: true, http, tries };
    const retryable = http === null || http === 429 || (http >= 500 && http <= 599);
    if (!retryable || tries >= 2) break;
    await io.sleep(FIRE_RETRY_AFTER_MS);
  }
  return { fired: false, http, tries };
}

// ---------------------------------------------------------------------------------------------------------------
// Log (Workers KV, key log/<model_day> = the day's entries in check order)

async function readLog(env, d) {
  try {
    const v = await env.WATCHER_LOG.get(`log/${d}`, { type: "json" });
    return Array.isArray(v) ? v : [];
  } catch {
    return [];
  }
}

async function writeLog(env, d, entry) {
  const entries = (await readLog(env, d)).filter((e) => e.check_utc !== entry.check_utc);
  entries.push(entry);
  entries.sort((a, b) => (a.check_utc < b.check_utc ? -1 : a.check_utc > b.check_utc ? 1 : 0));
  await env.WATCHER_LOG.put(`log/${d}`, JSON.stringify(entries));
}

// ---------------------------------------------------------------------------------------------------------------
// One check

/** Runs the check of a cron run scheduled at scheduledMs. Returns the log entry, or null when it does not act. */
async function runCheck(scheduledMs, env, io) {
  const T = Math.floor(scheduledMs / (30 * MIN)) * (30 * MIN);
  const d = tday(T);
  if (!inGrid(d, T)) return null;
  const [modeDoc, stateDoc] = await Promise.all([
    readJson(io, "main", "analyst/MODE.json"),
    readJson(io, "main", "analyst/STATE.json"),
  ]);
  const a14 = modeDoc && typeof modeDoc.a14_from === "string" ? modeDoc.a14_from : null;
  if (modeDoc && (a14 === null || d < a14)) return null; // A14 does not apply to d
  const a15 = modeDoc && typeof modeDoc.a15_from === "string" ? modeDoc.a15_from : null;
  const reviews = a15 !== null && d >= a15; // A15: no O(T), never `trade_open`
  const log = await readLog(env, d);
  if (log.some((e) => e.check_utc === isoZ(T))) return null; // a repeated run of a check that already acted
  const entry = { check_utc: isoZ(T), model_day: d, decision: "none", fired: false, http: null, tries: 0, utc: null };
  const finish = async () => {
    entry.utc = isoZ(io.now());
    await writeLog(env, d, entry);
    return entry;
  };
  const mode = modeDoc ? modeDoc.mode : null;
  if (mode !== "canary" && mode !== "live") {
    entry.decision = "mode_off"; // mode off, or MODE.json unreadable
    return finish();
  }
  if (!stateDoc || stateDoc.calls_enabled !== true) {
    entry.decision = "calls_disabled"; // calls_enabled false, or STATE.json unreadable
    return finish();
  }

  const firedTriggers = log
    .filter((e) => e.fired && typeof e.decision === "string" && e.decision.startsWith("trigger:"))
    .map((e) => ({ ...triggerWindow(d, Date.parse(e.check_utc)), loggedReason: e.decision.slice(8) }));
  const firesToday = log.filter((e) => e.fired).length;
  const candidates = [...scheduledWindows(d), ...firedTriggers].filter((w) => w.check < T);
  const seen = await Promise.all(candidates.map((w) => readWindow(io, mode, w, T, reviews)));
  const E = [];
  const O = [];
  candidates.forEach((w, i) => {
    const s = seen[i];
    if (s.attempted) E.push({ check: w.check, kind: w.kind, reason: s.reason ?? w.loggedReason ?? null });
    if (!reviews && s.called && w.exit > T + 30 * MIN) O.push(w);
  });

  let r = ruleR(T, d, E, O, undefined);
  if (r.result === "trigger_due") {
    const td = await tdRequest(io, env, d, T);
    if (td.status !== "ok") {
      entry.decision = `td_${td.reason}`;
      return finish();
    }
    r = ruleR(T, d, E, O, conditionsAt(T, d, td.bars));
  }
  if (r.result === "trade_open" || r.result === "day_cap") {
    entry.decision = r.result;
    return finish();
  }
  if (r.result !== "window") {
    entry.decision = "none";
    return finish();
  }
  const w = r.window;
  if (w.kind === "fixed") {
    entry.decision = "fixed"; // W1 and W2 are fired by their own routines
    return finish();
  }
  entry.decision = w.kind === "trigger" ? `trigger:${w.reason}` : w.name;
  if (firesToday >= FIRE_CAP) {
    entry.decision = "fire_cap";
    return finish();
  }
  const f = await fire(io, env, T);
  entry.fired = f.fired;
  entry.http = f.http;
  entry.tries = f.tries;
  return finish();
}

// ---------------------------------------------------------------------------------------------------------------
// The Worker

const DAY_RE = /^\d{4}-\d{2}-\d{2}$/;

async function handleFetch(request, env) {
  const url = new URL(request.url);
  if (request.method !== "GET" || url.pathname !== "/log") return new Response("not found\n", { status: 404 });
  const day = url.searchParams.get("day") || "";
  if (!DAY_RE.test(day) || !Number.isFinite(dayStart(day)) || ymd(dayStart(day)) !== day) {
    return new Response("day must be YYYY-MM-DD\n", { status: 400 });
  }
  const body = JSON.stringify(await readLog(env, day));
  return new Response(body, {
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store",
      "access-control-allow-origin": "*" },
  });
}

const realIo = {
  fetch: (url, init) => fetch(url, { ...init, cache: "no-store" }),
  now: () => Date.now(),
  sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
};

export default {
  async fetch(request, env) {
    return handleFetch(request, env);
  },
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(runCheck(controller.scheduledTime, env, realIo).catch(() => {
      console.log("watcher: check failed"); // never the error text (it could carry a URL with a secret)
    }));
  },
  // Pure parts, for the tests (node:test). The runtime never calls them from outside.
  internals: {
    tday, isWeekday, localToUtc, modelDayEnd, lastCheck, grid, inGrid, scheduledWindows, triggerWindow,
    isHolidayDay, isHolidayFor, median, conditionsAt, ruleR, tdUrl, classifyTd, tdRequest, readWindow, fire, runCheck,
    handleFetch, HOLIDAYS_CSV, INBOX_REPO, CONDITIONS,
  },
};
