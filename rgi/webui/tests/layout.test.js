import { test } from "node:test";
import assert from "node:assert/strict";

import {
  autoLayout, codeLabel, defaultLanePool, keysByLamp, laneKeys, resolveLayout, toCsv,
} from "../lib/layout.js";

const LAMPS = [
  { index: 0, label: "`", group: "number-row" },
  { index: 1, label: "1", group: "number-row" },
  { index: 2, label: "2", group: "number-row" },
  { index: 13, label: "Q", group: "alphas" },
];

test("autoLayout keeps lamp order and wraps into rows", () => {
  const layout = autoLayout(LAMPS, 2);
  assert.deepEqual(layout.keys.map((k) => k.id), ["k0", "k1", "k2", "k3"]);
  assert.deepEqual(layout.lamps.map((l) => l.index), [0, 1, 2, 13]);
  assert.deepEqual([layout.keys[0].geometry.x, layout.keys[0].geometry.y], [0, 0]);
  assert.deepEqual([layout.keys[2].geometry.x, layout.keys[2].geometry.y], [0, 1]);
});

test("defaultLanePool prefers the number row", () => {
  const layout = autoLayout(LAMPS, 2);
  assert.deepEqual(defaultLanePool(layout, 3), [0, 1, 2]);
});

test("resolveLayout falls back to auto when the config has no layout", () => {
  const capability = { name: "dummy", lamps: LAMPS, pool: [0, 1, 2] };
  const layout = resolveLayout({ devices: {}, layouts: {} }, capability);
  assert.equal(layout.keys.length, 4);
  const configured = { format_version: 2, keys: [{ id: "k0", label: "only" }], lamps: [] };
  const resolved = resolveLayout(
    { devices: { dummy: { layout: "mine" } }, layouts: { mine: configured } }, capability);
  assert.equal(resolved.keys.length, 1);
});

test("laneKeys falls back to the default pool when config has none", () => {
  const capability = { name: "dummy", lamps: LAMPS, pool: [0, 1, 2] };
  const keys = laneKeys(capability, { devices: {}, layouts: {} }, 2);
  assert.deepEqual(keys.map((k) => k.lamp), [0, 1]);
  assert.equal(keys[0].label, "`");
});

test("keysByLamp maps keys through lamp records", () => {
  const layout = autoLayout(LAMPS, 2);
  const map = keysByLamp(layout);
  assert.equal(map.get(13).label, "Q");
});

test("codeLabel humanises event.code", () => {
  assert.equal(codeLabel("Backquote"), "`");
  assert.equal(codeLabel("KeyA"), "A");
  assert.equal(codeLabel("Digit7"), "7");
  assert.equal(codeLabel("Numpad5"), "Num 5");
  assert.equal(codeLabel("F11"), "F11");
});

test("toCsv renders one row per lamp", () => {
  const layout = autoLayout(LAMPS, 2);
  const csv = toCsv(layout.lamps);
  assert.ok(csv.startsWith("lamp,kind,key,code,label"));
  assert.equal(csv.trim().split("\n").length, 5);
});
