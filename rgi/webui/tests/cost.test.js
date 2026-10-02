import { test } from "node:test";
import assert from "node:assert/strict";

import {
  CONTEXT_BAD_PERCENT, CONTEXT_WARN_PERCENT, aggregate, compactTokens,
  contextLevel, formatContext, formatCost,
} from "../lib/cost.js";

test("contextLevel flags 70 and 90 percent", () => {
  assert.equal(contextLevel(0), "ok");
  assert.equal(contextLevel(69.9), "ok");
  assert.equal(contextLevel(CONTEXT_WARN_PERCENT), "warn");
  assert.equal(contextLevel(89.9), "warn");
  assert.equal(contextLevel(CONTEXT_BAD_PERCENT), "bad");
  assert.equal(contextLevel(120), "bad");
});

test("contextLevel is null when the percentage is unknown", () => {
  assert.equal(contextLevel(null), null);
  assert.equal(contextLevel(undefined), null);
  assert.equal(contextLevel(""), null);
  assert.equal(contextLevel("not a number"), null);
  assert.equal(contextLevel(NaN), null);
});

test("compactTokens uses binary units", () => {
  assert.equal(compactTokens(950), "950");
  assert.equal(compactTokens(8192), "8k");
  assert.equal(compactTokens(131072), "128k");
  assert.equal(compactTokens(1048576), "1M");
  assert.equal(compactTokens(null), null);
});

test("formatContext shows percent and the window size", () => {
  assert.equal(formatContext({ context: { percent: 42, limit: 131072 } }), "42% · 128k");
  assert.equal(formatContext({ context: { percent: 42.4 } }), "42%");
  assert.equal(formatContext({ context: { percent: 7, limit: 8192 } }), "7% · 8k");
});

test("formatContext falls back to entries and handles unknown data", () => {
  assert.equal(formatContext({ context: { entries: 12 } }), "12 entries");
  assert.equal(formatContext({ context: { entries: 12, limit: 32000 } }), "12 entries · 31k");
  assert.equal(formatContext({}), "—");
  assert.equal(formatContext(null), "—");
  assert.equal(formatContext({ context: { percent: null, limit: null } }), "—");
  // context may also be handed over directly
  assert.equal(formatContext({ percent: 91, limit: 131072 }), "91% · 128k");
});

test("formatCost renders dollars and hides unknown spend", () => {
  assert.equal(formatCost({ tokens: { cost: 4.12 } }), "$4.12");
  assert.equal(formatCost({ tokens: { cost: 0 } }), "$0.00");
  assert.equal(formatCost({ tokens: { cost: 12 } }), "$12.00");
  assert.equal(formatCost(0.0042), "$0.0042");
  assert.equal(formatCost({ tokens: {} }), "—");
  assert.equal(formatCost({ tokens: { cost: null } }), "—");
  assert.equal(formatCost(null), "—");
});

test("aggregate counts live sessions and finds the longest blocked wait", () => {
  const sessions = [
    { sessionID: "a", slot: 0, state: "working", in_flight_s: 12,
      info: { tokens: { cost: 1.5, input: 100, output: 20 } } },
    { sessionID: "b", slot: 1, state: "blocked", label: "review", host: "box",
      in_flight_s: 300 },
    { sessionID: "c", slot: 2, state: "blocked", label: "stuck", host: "laptop",
      in_flight_s: 45, stale: true, stale_s: 600,
      info: { tokens: { cost: 2.5 } } },
    { sessionID: "d", slot: 3, state: "done", idle_s: 5 },
  ];
  const summary = aggregate(sessions, { now: 1000 });
  assert.deepEqual(summary.counts,
    { working: 1, done: 1, blocked: 2, error: 0, idle: 0, other: 0 });
  assert.equal(summary.total, 4);
  assert.equal(summary.cost, 4);
  assert.equal(summary.tokens_in, 100);
  assert.equal(summary.tokens_out, 20);
  assert.equal(summary.longest_wait_s, 300);
  assert.deepEqual(summary.blocked.map((entry) => entry.session), ["b", "c"]);
  assert.equal(summary.blocked[0].wait_s, 300);
  assert.equal(summary.blocked[1].wait_s, 45);
});

test("aggregate defaults a missing live state to idle and reports unknown ones", () => {
  const summary = aggregate([
    { sessionID: "a", slot: 0 },
    { sessionID: "b", slot: 1, state: "stopping" },
  ]);
  assert.equal(summary.counts.idle, 1);
  assert.equal(summary.counts.other, 1);
});

test("aggregate tolerates an empty or missing list", () => {
  const summary = aggregate(undefined);
  assert.equal(summary.total, 0);
  assert.equal(summary.longest_wait_s, null);
  assert.deepEqual(summary.blocked, []);
  assert.deepEqual(summary.counts,
    { working: 0, done: 0, blocked: 0, error: 0, idle: 0, other: 0 });
  assert.equal(summary.cost, null);
});

test("aggregate counts the latest history entry per session and uses cumulative cost", () => {
  const entries = [
    { at: 10, kind: "state", session: "a", slot: 0, state: "working", cost: 0.5 },
    { at: 15, kind: "state", session: "b", slot: 1, state: "blocked", label: "stuck" },
    { at: 20, kind: "state", session: "a", slot: 0, state: "done", cost: 1.25 },
    { at: 40, kind: "state", session: "b", slot: 1, state: "working" },
  ];
  const summary = aggregate(entries, { now: 100 });
  assert.equal(summary.total, 2);
  assert.equal(summary.counts.done, 1);
  assert.equal(summary.counts.working, 1);
  assert.equal(summary.counts.blocked, 0);
  assert.equal(summary.cost, 1.25);          // latest per session, not 0.5 + 1.25
  assert.equal(summary.longest_wait_s, 25);   // blocked at 15, working again at 40
  assert.deepEqual(summary.blocked, []);       // nothing is blocked right now
});

test("aggregate sums the latest cumulative cost across sessions", () => {
  const entries = [
    { at: 10, kind: "state", session: "a", state: "working", cost: 1.0, tokens_in: 10, tokens_out: 2 },
    { at: 20, kind: "state", session: "a", state: "working", cost: 1.5, tokens_in: 14, tokens_out: 5 },
    { at: 12, kind: "state", session: "b", state: "error", cost: 2.0, tokens_in: 7, tokens_out: 1 },
    { at: 18, kind: "state", session: "b", state: "idle", cost: 2.0, tokens_in: 7, tokens_out: 2 },
  ];
  const summary = aggregate(entries);
  assert.equal(summary.cost, 3.5);
  assert.equal(summary.tokens_in, 21);
  assert.equal(summary.tokens_out, 7);
});

test("aggregate measures a still-blocked history entry against now", () => {
  const entries = [
    { at: 100, kind: "state", session: "x", slot: 2, state: "blocked", label: "review" },
  ];
  const summary = aggregate(entries, { now: 400 });
  assert.equal(summary.counts.blocked, 1);
  assert.equal(summary.longest_wait_s, 300);
  assert.deepEqual(summary.blocked, [
    { session: "x", slot: 2, label: "review", host: "", wait_s: 300 },
  ]);
  // Without a clock the wait is unknown, but the lane is still listed.
  const open = aggregate(entries);
  assert.equal(open.longest_wait_s, null);
  assert.deepEqual(open.blocked, [
    { session: "x", slot: 2, label: "review", host: "", wait_s: null },
  ]);
});

test("aggregate closes a blocked wait at end and drops the ended lane", () => {
  const entries = [
    { at: 10, kind: "state", session: "x", slot: 0, state: "blocked", cost: 1.0 },
    { at: 30, kind: "end", session: "x", slot: 0, state: "blocked", cost: 1.0 },
  ];
  const summary = aggregate(entries, { now: 1000 });
  assert.equal(summary.total, 1);
  assert.equal(summary.counts.blocked, 0);
  assert.equal(summary.longest_wait_s, 20);   // 10 -> 30, not 10 -> now
  assert.deepEqual(summary.blocked, []);
  assert.equal(summary.cost, 1.0);
});

test("aggregate keeps a blocked interval open across state-less info entries", () => {
  const entries = [
    { at: 10, kind: "state", session: "x", state: "blocked" },
    { at: 20, kind: "info", session: "x", state: null },
    { at: 25, kind: "info", session: "x", state: null },
  ];
  const summary = aggregate(entries, { now: 50 });
  assert.equal(summary.longest_wait_s, 40);   // 10 -> now, heartbeats do not resolve it
  assert.equal(summary.blocked[0].wait_s, 40);
});

test("aggregate prefers an explicit live wait_s from the daemon", () => {
  const summary = aggregate([
    { sessionID: "a", slot: 0, state: "blocked", wait_s: 77 },
  ], { now: 100 });
  assert.equal(summary.longest_wait_s, 77);
});
