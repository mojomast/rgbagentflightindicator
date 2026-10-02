import { test } from "node:test";
import assert from "node:assert/strict";

import { autoLayout, codeLabel, defaultLanePool, keysByLamp, laneKeys, toCsv } from "../lib/layout.js";

const lamps = [
  { index: 0, label: "`", group: "number-row" },
  { index: 1, label: "1", group: "number-row" },
  { index: 2, label: "2", group: "number-row" },
  { index: 13, label: "KeyQ", group: "alphas" },
];

test("autoLayout keeps lamp order and wraps into rows", () => {
  const layout = autoLayout(lamps, 2);
  assert.deepEqual(layout.keys.map((k) => k.lamp), [0, 1, 2, 13]);
  assert.deepEqual([layout.keys[0].x, layout.keys[0].y], [0, 0]);
  assert.deepEqual([layout.keys[2].x, layout.keys[2].y], [0, 1]);
});

test("defaultLanePool prefers the number row", () => {
  const layout = autoLayout(lamps, 2);
  assert.deepEqual(defaultLanePool(layout, 3), [0, 1, 2]);
});

test("laneKeys falls back to the default pool when config has none", () => {
  const capability = { name: "dummy", lamps, pool: [0, 1, 2] };
  const keys = laneKeys(capability, { devices: {}, layouts: {} }, 2);
  assert.deepEqual(keys.map((k) => k.lamp), [0, 1]);
  assert.equal(keys[0].label, "`");
});

test("codeLabel humanises event.code", () => {
  assert.equal(codeLabel("Backquote"), "`");
  assert.equal(codeLabel("KeyA"), "A");
  assert.equal(codeLabel("Digit7"), "7");
  assert.equal(codeLabel("Numpad5"), "Num 5");
  assert.equal(codeLabel("F11"), "F11");
});

test("keysByLamp indexes and toCsv renders", () => {
  const layout = autoLayout(lamps, 2);
  const map = keysByLamp(layout);
  assert.equal(map.get(13).label, "KeyQ");
  const csv = toCsv(layout.keys);
  assert.ok(csv.startsWith("lamp,code,label"));
  assert.equal(csv.trim().split("\n").length, 5);
});
