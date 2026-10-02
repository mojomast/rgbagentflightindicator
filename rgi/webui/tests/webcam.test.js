import { test } from "node:test";
import assert from "node:assert/strict";

import { findBlob, frameDiff, median } from "../lib/webcam.js";

test("median works for odd and even counts", () => {
  assert.equal(median([3, 1, 2]), 2);
  assert.equal(median([4, 1, 3, 2]), 2.5);
  assert.equal(median([]), 0);
});

test("frameDiff keeps only positive changes", () => {
  const diff = frameDiff(Float32Array.from([10, 0, 5]), Float32Array.from([4, 9, 5]));
  assert.deepEqual([...diff], [6, 0, 0]);
});

test("findBlob locates a synthetic bright spot", () => {
  const width = 40, height = 30;
  const lit = new Float32Array(width * height);
  for (let y = 14; y < 18; y += 1) {
    for (let x = 19; x < 23; x += 1) lit[y * width + x] = 120;
  }
  const blob = findBlob(frameDiff(lit, new Float32Array(width * height)), width, height);
  assert.ok(blob, "expected a blob");
  assert.ok(Math.abs(blob.x - 20.5) < 1.5, `x=${blob.x}`);
  assert.ok(Math.abs(blob.y - 15.5) < 1.5, `y=${blob.y}`);
  assert.ok(blob.confidence > 0.2, `confidence=${blob.confidence}`);
  assert.ok(blob.pixels >= 16);
});

test("findBlob ignores noise and darkness", () => {
  const width = 40, height = 30;
  const dark = new Float32Array(width * height);
  assert.equal(findBlob(dark, width, height), null);
  const noise = new Float32Array(width * height).map(() => 2);
  assert.equal(findBlob(noise, width, height), null);
});

test("findBlob stays with the strongest of two spots", () => {
  const width = 60, height = 30;
  const lit = new Float32Array(width * height);
  for (let y = 4; y < 8; y += 1) for (let x = 4; x < 8; x += 1) lit[y * width + x] = 40;
  for (let y = 20; y < 24; y += 1) for (let x = 40; x < 44; x += 1) lit[y * width + x] = 150;
  const blob = findBlob(lit, width, height);
  assert.ok(blob.x > 30, `kept the weak spot: x=${blob.x}`);
});
