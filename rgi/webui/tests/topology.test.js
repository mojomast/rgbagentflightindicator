import { test } from "node:test";
import assert from "node:assert/strict";

import {
  autoLayout, boundsOf, caseOf, lampsOf, migrateLayout, orderedPathLamps,
  pathD, pointAtPath, projectOnPath, subPathD, zoneOfLamp,
} from "../lib/topology.js";

const LAMPS = [
  { index: 0, label: "`", group: "number-row" },
  { index: 1, label: "1", group: "number-row" },
  { index: 104, label: "base 0", group: "zone" },
  { index: 105, label: "base 1", group: "zone" },
];

test("autoLayout produces v2 keys, lamp records and a keys zone", () => {
  const layout = autoLayout(LAMPS, 2);
  assert.equal(layout.format_version, 2);
  assert.equal(layout.keys.length, 4);
  assert.equal(layout.lamps.length, 4);
  assert.equal(layout.lamps[0].kind, "key");
  assert.equal(layout.lamps[2].kind, "accent");
  assert.deepEqual(layout.zones[0].lamps, [0, 1]);
  assert.deepEqual(layout.lamps[0].key, layout.keys[0].id);
});

test("v1 layouts migrate to v2 keys + lamps and keep legacy fields", () => {
  const v1 = {
    format_version: 1,
    source: "auto",
    keys: [{ lamp: 7, label: "A", x: 2, y: 3, w: 1, h: 1, future: true }],
  };
  const migrated = migrateLayout(v1);
  assert.equal(migrated.format_version, 2);
  assert.equal(migrated.keys[0].id, "k0");
  assert.deepEqual(migrated.keys[0].geometry, { type: "rect", x: 2, y: 3, w: 1, h: 1 });
  assert.equal(migrated.keys[0].future, true);
  assert.equal(migrated.lamps[0].index, 7);
  assert.equal(migrated.lamps[0].kind, "key");
  assert.equal(migrated.lamps[0].key, "k0");
});

test("bounds include the perimeter outside the key grid", () => {
  const layout = autoLayout(LAMPS, 2);
  layout.zones = [{ id: "base", kind: "perimeter", lamps: [104, 105],
                    geometry: { type: "path", closed: true, width: 0.2,
                                points: [[-0.6, -0.6], [3, -0.6], [3, 2.6], [-0.6, 2.6]] } }];
  layout.lamps[2].zone = "base";
  layout.lamps[3].zone = "base";
  const bounds = boundsOf(layout);
  assert.ok(bounds.minX < -0.5, `minX=${bounds.minX}`);
  assert.ok(bounds.maxX > 2.5);
  const box = caseOf({ keys: layout.keys, canvas: { margin: 0.5 } });
  assert.ok(box.x < 0 && box.w > 2);
});

test("lamp records are unique and sorted", () => {
  const layout = autoLayout(LAMPS, 2);
  layout.lamps.push({ index: 0, kind: "key", key: "k0" });
  assert.deepEqual(lampsOf(layout).map((l) => l.index), [0, 1, 104, 105]);
});

test("path arc-length maths round-trips", () => {
  const points = [[0, 0], [10, 0], [10, 10], [0, 10]];
  const d = pathD(points, true);
  assert.ok(d.endsWith("Z"));
  const mid = pointAtPath(points, true, 0.25);
  assert.ok(Math.abs(mid.x - 10) < 1e-6 && Math.abs(mid.y - 0) < 1e-6);
  const projection = projectOnPath(points, true, 9.5, 4);
  assert.ok(projection.dist < 0.51, `dist=${projection.dist}`);
  assert.ok(projection.t > 0.3 && projection.t < 0.4, `t=${projection.t}`);
});

test("subPathD slices a strip into a drawable segment", () => {
  const points = [[0, 0], [4, 0]];
  const d = subPathD(points, false, 0.25, 0.75);
  assert.ok(d.startsWith("M1,0"), d);
  assert.ok(d.includes("L3,0"), d);
});

test("zones map lamps and ordered strips respect t", () => {
  const layout = autoLayout(LAMPS, 2);
  layout.lamps[2].zone = "base";
  layout.lamps[2].path = { zone: "base", t: 0.8 };
  layout.lamps[3].zone = "base";
  layout.lamps[3].path = { zone: "base", t: 0.2 };
  layout.zones = [{ id: "base", kind: "perimeter", lamps: [104, 105] }];
  const map = zoneOfLamp(layout);
  assert.equal(map.get(104), "base");
  const ordered = orderedPathLamps(layout, layout.zones[0], lampsOf(layout));
  assert.deepEqual(ordered.map((entry) => entry.index), [105, 104]);
});
