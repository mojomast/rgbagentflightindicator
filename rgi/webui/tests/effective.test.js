import { test } from "node:test";
import assert from "node:assert/strict";

import { effectiveLamp, formatDuration, overridesFor } from "../lib/effective.js";

const CONFIG = {
  appearance: {
    blink: { period_ms: 560, duty: 0.5 },
    states: {
      working: { color: "#00ff00", pattern: "steady", brightness: 255 },
      done: { color: "#ffffff", pattern: "blink", period_ms: 560, duty: 0.5,
              cycles: 10, then: "steady", brightness: 255 },
      blocked: { color: "#ff0000", pattern: "blink", period_ms: 560, duty: 0.5,
                 cycles: 0, then: "steady", brightness: 255 },
      idle: { color: "#282828", pattern: "steady", brightness: 255 },
      off: { color: "#000000", pattern: "off", brightness: 0 },
    },
  },
  lamp_overrides: [{ device: "dummy", lamp: 2, mode: "static", color: "#0000ff" }],
  zone_overrides: [{ device: "dummy", zone: "base", mode: "static", color: "#00aaff" }],
};

const CAP = { name: "dummy", pool: [0, 1, 2] };
const STATUS = { sessions: {
  a: { sessionID: "a", slot: 0, state: "working", changedMs: 1000 },
  b: { sessionID: "b", slot: 1, state: "blocked", changedMs: 1000 },
} };

test("lane state picks the state colour", () => {
  const look = effectiveLamp(CONFIG, CAP, 0, STATUS, 2000, { quiet: true });
  assert.equal(look.source, "lane");
  assert.deepEqual(look.color, [0, 255, 0]);
});

test("blocked keeps its blink pattern when not quiet", () => {
  const look = effectiveLamp(CONFIG, CAP, 1, STATUS, 2000, { quiet: false });
  assert.equal(look.pattern, "blink");
  assert.deepEqual(look.color, [255, 0, 0]);
});

test("quiet holds a steady colour", () => {
  const look = effectiveLamp(CONFIG, CAP, 1, STATUS, 2000, { quiet: true });
  assert.equal(look.pattern, "steady");
  assert.deepEqual(look.color, [255, 0, 0]);
});

test("lamp override beats every lane", () => {
  const look = effectiveLamp(CONFIG, CAP, 2, STATUS, 2000, { quiet: false });
  assert.equal(look.source, "lamp override");
  assert.deepEqual(look.color, [0, 0, 255]);
});

test("zone override paints unclaimed zone lamps", () => {
  const look = effectiveLamp(CONFIG, CAP, 9, { sessions: {} }, 0, {
    quiet: false, zoneOfLamp: () => "base",
  });
  assert.equal(look.source, "zone override");
  assert.deepEqual(look.color, [0, 170, 255]);
});

test("off state is black and source off", () => {
  const look = effectiveLamp(CONFIG, CAP, 5, { sessions: {} }, 0, {});
  assert.equal(look.source, "off");
  assert.deepEqual(look.color, [0, 0, 0]);
});

test("overridesFor indexes both lamp and zone records", () => {
  const maps = overridesFor(CONFIG, "dummy");
  assert.equal(maps.lamps.get(2), "#0000ff");
  assert.equal(maps.zones.get("base"), "#00aaff");
});

test("duration formatting is compact", () => {
  assert.equal(formatDuration(42), "42s");
  assert.equal(formatDuration(125), "2m 05s");
  assert.equal(formatDuration(7325), "2h 02m");
  assert.equal(formatDuration(null), "—");
});
