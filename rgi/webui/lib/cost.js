// Context and cost helpers for the Live lane table and the "while you were
// away" digest. Everything here is pure: no DOM, no storage, no clock unless
// the caller passes one in.
//
// Context pressure thresholds (shared by the table colour and the tooltip):
//
//   < 70%   normal
//   >= 70%  amber  ("warn")
//   >= 90%  red    ("bad")
//
// The daemon reports per-session detail in `info`:
//
//   tokens:  { input, output, cache_read, cost }   cost is already US dollars
//   context: { entries, compactions, percent, limit }
//
// `limit` is a token count, compacted with binary units because model windows
// are powers of two (131072 -> "128k"). `aggregate()` accepts either live
// session objects from /status or /ui/api/history entries; history entries are
// recognised by their `at` timestamp.

export const CONTEXT_WARN_PERCENT = 70;
export const CONTEXT_BAD_PERCENT = 90;

const STATES = ["working", "done", "blocked", "error", "idle"];

/**
 * "ok" | "warn" | "bad" for a context percentage, or null when it is unknown.
 * The daemon may send percent: null when it has no context limit configured.
 */
export function contextLevel(percent) {
  const value = percent == null || percent === "" ? NaN : Number(percent);
  if (!Number.isFinite(value)) return null;
  if (value >= CONTEXT_BAD_PERCENT) return "bad";
  if (value >= CONTEXT_WARN_PERCENT) return "warn";
  return "ok";
}

/** 131072 -> "128k", 1048576 -> "1M". Binary units; null for unknown. */
export function compactTokens(value) {
  if (value == null || value === "") return null;
  const n = Number(value);
  if (!Number.isFinite(n) || n < 0) return null;
  if (n >= 1024 * 1024) return `${Math.round(n / (1024 * 1024))}M`;
  if (n >= 1024) return `${Math.round(n / 1024)}k`;
  return String(Math.round(n));
}

/**
 * The compact Context cell: "42% · 128k". Falls back to "12 entries" when no
 * percentage is configured, and to "—" when nothing is known.
 */
export function formatContext(info) {
  const context = contextOf(info);
  const parts = [];
  const percent = Number(context.percent);
  if (context.percent != null && context.percent !== "" && Number.isFinite(percent)) {
    parts.push(`${Math.round(percent)}%`);
  } else {
    const entries = Number(context.entries);
    if (context.entries != null && context.entries !== "" && Number.isFinite(entries)) {
      parts.push(`${Math.round(entries)} entries`);
    }
  }
  if (context.limit != null) {
    const limit = compactTokens(context.limit);
    if (limit != null) parts.push(limit);
  }
  return parts.length ? parts.join(" · ") : "—";
}

/**
 * The Cost cell: "$4.12". Accepts the session `info` blob or a bare number.
 * Very small non-zero costs keep four decimals so they are not rounded to
 * nothing; unknown cost is "—".
 */
export function formatCost(info) {
  const raw = typeof info === "number" ? info
    : (info && typeof info === "object" ? info.tokens?.cost : undefined);
  if (raw == null || raw === "") return "—";
  const cost = Number(raw);
  if (!Number.isFinite(cost)) return "—";
  if (cost > 0 && cost < 0.01) return `$${cost.toFixed(4)}`;
  return `$${cost.toFixed(2)}`;
}

/**
 * Summarise what happened for the digest card. Records may be live sessions
 * (from /status) or history entries (from /ui/api/history); the two shapes are
 * told apart by `at` (history) vs `changed_at`/`sessionID` (live). Mixing the
 * two is not expected but is tolerated.
 *
 * Live sessions contribute one count each. History entries contribute the
 * latest non-terminal state per session, so a burst of working->done events
 * counts as one done lane, and an `end`/`clear` event removes the lane from
 * every count. Cost and token counters are cumulative on the wire, so the
 * latest non-null value per session is used and then summed across sessions
 * (never summed over every line, which would multiply a running total).
 *
 * The longest wait is the longest blocked window: from the session timing
 * fields live, and from per-session history transitions otherwise. A blocked
 * interval closes on the next state change away from blocked or a terminal
 * end/clear; a still-blocked trailing entry waits until `now`, when the caller
 * passes it in seconds.
 *
 * Returns { total, counts, cost, tokens_in, tokens_out, longest_wait_s,
 *           blocked: [{session, slot, label, host, wait_s}] }
 */
export function aggregate(records, { now } = {}) {
  const list = (Array.isArray(records) ? records : []).filter(
    (record) => record && typeof record === "object");
  const counts = { working: 0, done: 0, blocked: 0, error: 0, idle: 0, other: 0 };
  const totals = { cost: null, tokens_in: null, tokens_out: null };
  const blocked = [];
  let longest = null;
  let total = 0;

  const live = [];
  const groups = new Map();
  for (const record of list) {
    if (isHistoryEntry(record)) {
      const key = String(record.session ?? record.sessionID ?? record.slot ?? groups.size);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(record);
    } else {
      live.push(record);
    }
  }

  for (const session of live) {
    total += 1;
    addTotals(totals, session);
    const state = stateKey(session.state, "idle");
    counts[state] += 1;
    if (state === "blocked") {
      const wait = firstNumber(session.wait_s, session.in_flight_s, session.idle_s,
        session.stale ? session.stale_s : null);
      blocked.push(blockedEntry(session, wait));
      longest = maxWait(longest, wait);
    }
  }

  for (const [key, entries] of groups) {
    entries.sort((a, b) => numberOr(a.at, 0) - numberOr(b.at, 0));
    total += 1;
    let cost = null;
    let tokensIn = null;
    let tokensOut = null;
    let state = null;
    let blockSince = null;
    let wait = null;
    for (const entry of entries) {
      cost = pickNumber(entry.cost, cost);
      tokensIn = pickNumber(entry.tokens_in, tokensIn);
      tokensOut = pickNumber(entry.tokens_out, tokensOut);
      const at = numberOr(entry.at, null);
      if (isTerminalKind(entry)) {
        if (blockSince != null && at != null) {
          wait = maxWait(wait, Math.max(0, at - blockSince));
        }
        blockSince = null;
        state = null;
        continue;
      }
      const entryState = stateKey(entry.state, null);
      if (entryState === "blocked" && state !== "blocked" && at != null) {
        blockSince = at;
      } else if (state === "blocked" && entryState && entryState !== "blocked") {
        if (blockSince != null && at != null) {
          wait = maxWait(wait, Math.max(0, at - blockSince));
        }
        blockSince = null;
      }
      if (entryState) state = entryState;
    }
    let openWait = null;
    if (blockSince != null && Number.isFinite(Number(now)) && Number(now) >= blockSince) {
      openWait = Number(now) - blockSince;
    }
    longest = maxWait(longest, maxWait(wait, openWait));
    if (state) counts[state] += 1;
    if (state === "blocked") {
      blocked.push(blockedEntry(entries[entries.length - 1], openWait, key));
    }
    totals.cost = addNumber(totals.cost, cost);
    totals.tokens_in = addNumber(totals.tokens_in, tokensIn);
    totals.tokens_out = addNumber(totals.tokens_out, tokensOut);
  }

  blocked.sort((a, b) => numberOr(b.wait_s, -1) - numberOr(a.wait_s, -1));
  return {
    total,
    counts,
    cost: totals.cost,
    tokens_in: totals.tokens_in,
    tokens_out: totals.tokens_out,
    longest_wait_s: longest,
    blocked,
  };
}

function contextOf(info) {
  if (!info || typeof info !== "object") return {};
  if (info.context && typeof info.context === "object") return info.context;
  if (info.percent != null || info.limit != null || info.entries != null) return info;
  return {};
}

function isHistoryEntry(record) {
  return typeof record.at === "number" && typeof record.changed_at !== "number";
}

/** `end`/`clear` mean the lane is gone; the history module agrees. */
function isTerminalKind(entry) {
  return entry.kind === "end" || entry.kind === "clear";
}

/** Latest non-null cumulative counter wins; a real 0 is a value, not a gap. */
function pickNumber(value, current) {
  if (value == null || value === "") return current;
  const n = Number(value);
  return Number.isFinite(n) ? n : current;
}

function stateKey(value, fallback) {
  if (value == null || value === "") return fallback;
  const state = String(value).toLowerCase();
  return STATES.includes(state) ? state : "other";
}

function blockedEntry(record, wait, key = null) {
  return {
    session: String(key ?? record.sessionID ?? record.session ?? record.slot ?? ""),
    slot: Number.isFinite(Number(record.slot)) ? Number(record.slot) : null,
    label: typeof record.label === "string" ? record.label : "",
    host: typeof record.host === "string" ? record.host : "",
    wait_s: wait,
  };
}

function addTotals(totals, record) {
  const info = record.info && typeof record.info === "object" ? record.info : {};
  const tokens = info.tokens && typeof info.tokens === "object" ? info.tokens : {};
  totals.cost = addNumber(totals.cost, record.cost ?? tokens.cost);
  totals.tokens_in = addNumber(totals.tokens_in, record.tokens_in ?? tokens.input);
  totals.tokens_out = addNumber(totals.tokens_out, record.tokens_out ?? tokens.output);
}

function addNumber(sum, value) {
  if (value == null || value === "") return sum;
  const n = Number(value);
  if (!Number.isFinite(n)) return sum;
  return (sum ?? 0) + n;
}

function firstNumber(...values) {
  for (const value of values) {
    if (value == null || value === "") continue;
    const n = Number(value);
    if (Number.isFinite(n)) return n;
  }
  return null;
}

function numberOr(value, fallback) {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function maxWait(current, candidate) {
  if (candidate == null) return current;
  return current == null || candidate > current ? candidate : current;
}
