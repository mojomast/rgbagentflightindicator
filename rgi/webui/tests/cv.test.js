import { test } from "node:test";
import assert from "node:assert/strict";

import {
  applyH, blobStats, detectLattice, findPeaks, orderOnPath, profilePeaks,
  refineHomography, samplePath, solveHomography, subPixelCentroid,
} from "../lib/cv.js";

test("homography recovers a projective transform from four points", () => {
  const H = [[34, 3, 100], [1.5, 32, 40], [0.0012, 0.0004, 1]];
  const units = [[0, 0], [10, 0], [10, 5], [0, 5], [3, 2], [7, 4], [5, 1], [2, 3]];
  const pairs = units.map(([x, y]) => {
    const p = applyH(H, x, y);
    return [x, y, p.x, p.y];
  });
  const solved = solveHomography(pairs.slice(0, 4));
  assert.ok(solved, "solver returned null");
  for (const [x, y] of units.slice(4)) {
    const a = applyH(H, x, y);
    const b = applyH(solved, x, y);
    assert.ok(Math.hypot(a.x - b.x, a.y - b.y) < 0.5,
              `(${a.x},${a.y}) vs (${b.x},${b.y})`);
  }
});

test("refineHomography tolerates outliers", () => {
  const H = [[34, 3, 100], [1.5, 32, 40], [0.0008, 0.0003, 1]];
  const pairs = [];
  for (let i = 0; i < 24; i += 1) {
    const x = (i % 8) * 1.5;
    const y = Math.floor(i / 8) * 1.5;
    const p = applyH(H, x, y);
    pairs.push([x, y, p.x, p.y]);
  }
  pairs[3][2] += 40;
  pairs[10][3] -= 35;
  pairs[20][2] += 25;
  const { inliers, residual } = refineHomography(pairs, { thresholdPx: 3 });
  assert.ok(inliers >= 19, `inliers=${inliers}`);
  assert.ok(residual < 1.5, `residual=${residual}`);
});

function gridFrame(width = 640, height = 360) {
  const gray = new Float32Array(width * height).fill(4);
  const x0 = 60, y0 = 70, key = 30, gap = 6, cols = 14, rows = 6;
  for (let r = 0; r < rows; r += 1) {
    for (let c = 0; c < cols; c += 1) {
      for (let y = 0; y < key; y += 1) {
        for (let x = 0; x < key; x += 1) {
          gray[(y0 + r * (key + gap) + y) * width + (x0 + c * (key + gap) + x)] = 200;
        }
      }
    }
  }
  return gray;
}

test("detectLattice finds a synthetic keycap grid", () => {
  const width = 640, height = 360;
  const lattice = detectLattice(gridFrame(width, height), width, height);
  assert.ok(lattice, "no lattice found");
  assert.ok(lattice.xs.length >= 10, `xs=${lattice.xs.length}`);
  assert.ok(lattice.ys.length >= 5, `ys=${lattice.ys.length}`);
  assert.ok(Math.abs(lattice.xPitch - 36) <= 4, `xPitch=${lattice.xPitch}`);
});

test("detectLattice refuses a flat frame", () => {
  const flat = new Float32Array(320 * 240).fill(10);
  assert.equal(detectLattice(flat, 320, 240), null);
});

test("findPeaks finds separated bumps", () => {
  const profile = new Float32Array(60).fill(1);
  for (const centre of [10, 30, 50]) {
    profile[centre - 1] = 6;
    profile[centre] = 10;
    profile[centre + 1] = 6;
  }
  const peaks = findPeaks(profile, { minProminence: 0.3, minSeparation: 5 });
  assert.deepEqual(peaks.map((p) => p.index), [10, 30, 50]);
});

test("blobStats measures a compact spot and its centroid", () => {
  const width = 120, height = 90;
  const diff = new Float32Array(width * height);
  for (let y = 40; y < 47; y += 1) {
    for (let x = 50; x < 57; x += 1) diff[y * width + x] = 100;
  }
  const { blobs, peak } = blobStats(diff, width, height, { noise: 0.5, absoluteFloor: 4 });
  assert.equal(peak, 100);
  assert.ok(blobs.length >= 1);
  const blob = blobs[0];
  assert.ok(Math.abs(blob.cx - 53) < 1.5, `cx=${blob.cx}`);
  assert.ok(Math.abs(blob.cy - 43) < 1.5, `cy=${blob.cy}`);
  assert.ok(blob.elongation < 1.8, `elongation=${blob.elongation}`);
  assert.ok(blob.concentration >= 1, `concentration=${blob.concentration}`);
  const refined = subPixelCentroid(diff, width, height, blob.cx, blob.cy, 5);
  assert.ok(Math.abs(refined.x - 53) < 1, `refined x=${refined.x}`);
});

test("samplePath and profilePeaks locate a light on the case edge", () => {
  const width = 200, height = 200;
  const diff = new Float32Array(width * height);
  for (let y = 80; y < 100; y += 1) {
    diff[y * width + 180] = 120;
    diff[y * width + 181] = 80;
  }
  const path = [[20, 20], [180, 20], [180, 180], [20, 180]];
  const profile = samplePath(diff, width, height, path, true,
                             { halfWidth: 4, samples: 320 });
  const peaks = profilePeaks(profile, { minProminence: 0.25 });
  assert.ok(peaks.length >= 1, "no peak on the path");
  assert.ok(peaks.some((peak) => peak.t > 0.30 && peak.t < 0.42),
            `t values: ${peaks.map((p) => p.t.toFixed(3))}`);
  assert.ok(peaks[0].width > 0);
});

test("orderOnPath sorts shuffled perimeter detections by arc position", () => {
  const path = [[0, 0], [10, 0], [10, 10], [0, 10]];
  const points = [[0, 6], [10, 3], [10, 8], [0, 2]].map(([x, y]) => ({ x, y }));
  const strips = orderOnPath(points, path, true, { gapFraction: 0.6 });
  assert.equal(strips.length, 1);
  const ordered = strips[0].points.map((p) => `${p.x},${p.y}`);
  assert.deepEqual(ordered, ["10,3", "10,8", "0,6", "0,2"]);
});

test("orderOnPath splits two separate strips at a large gap", () => {
  const path = [[0, 0], [10, 0], [10, 10], [0, 10]];
  const points = [[0, 1], [0, 2], [0, 3], [5, 0], [6, 0], [7, 0]]
    .map(([x, y]) => ({ x, y }));
  const strips = orderOnPath(points, path, true, { gapFraction: 0.1 });
  assert.ok(strips.length >= 2, `strips=${strips.length}`);
});
