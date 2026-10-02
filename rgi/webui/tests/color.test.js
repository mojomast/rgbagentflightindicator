import { test } from "node:test";
import assert from "node:assert/strict";

import { contrast, hexToRgb, luminance, readableOn, rgbToHex, simulate } from "../lib/color.js";

test("hex parsing handles short and long forms", () => {
  assert.deepEqual(hexToRgb("#abc"), [170, 187, 204]);
  assert.deepEqual(hexToRgb("#00ff00"), [0, 255, 0]);
  assert.deepEqual(hexToRgb("nope"), [0, 0, 0]);
  assert.equal(rgbToHex([0, 170, 255]), "#00aaff");
});

test("contrast uses the WCAG ratio", () => {
  const ratio = contrast([0, 0, 0], [255, 255, 255]);
  assert.ok(Math.abs(ratio - 21) < 0.01, `got ${ratio}`);
  assert.ok(contrast([255, 255, 255], [255, 255, 255]) === 1);
});

test("readableOn picks a foreground", () => {
  assert.equal(readableOn([0, 0, 0]), "#ffffff");
  assert.equal(readableOn([255, 255, 0]), "#000000");
});

test("colour-blind simulation is deterministic and changes the colour", () => {
  const sim = simulate([255, 0, 0], "deuteranopia");
  assert.equal(sim.length, 3);
  assert.notDeepEqual(sim, [255, 0, 0]);
  assert.deepEqual(sim, simulate([255, 0, 0], "deuteranopia"));
});

test("luminance is monotonic in brightness", () => {
  assert.ok(luminance([128, 128, 128]) > luminance([64, 64, 64]));
});
