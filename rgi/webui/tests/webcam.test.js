import { test } from "node:test";
import assert from "node:assert/strict";

import { classifyEvidence, confidenceFor } from "../lib/webcam.js";

test("a compact blob in a key cell is a key", () => {
  assert.equal(classifyEvidence({ dCell: 0.2, dPath: 1, elongation: 1.3,
                                  compact: true, hasCell: true }), "key");
});

test("a wide blob on the case path is a perimeter lamp", () => {
  assert.equal(classifyEvidence({ dCell: 0.9, dPath: 0.15, elongation: 3.4,
                                  compact: false, hasCell: true }), "perimeter");
});

test("a compact blob inside the case but away from keys is a logo", () => {
  assert.equal(classifyEvidence({ dCell: 1.2, dPath: 0.9, elongation: 1.2,
                                  compact: true, hasCell: false, insideCase: true }), "logo");
});

test("a blob outside the case is unknown", () => {
  assert.equal(classifyEvidence({ dCell: 2, dPath: 1.5, insideCase: false }), "unknown");
});

test("key wins over the path when both are plausible", () => {
  assert.equal(classifyEvidence({ dCell: 0.3, dPath: 0.3, elongation: 1.1,
                                  compact: true, hasCell: true }), "key");
});

test("confidence rises with snr and falls with distance", () => {
  const strong = confidenceFor({ snr: 14, dCell: 0.1, margin: 0.5, compact: true });
  const weak = confidenceFor({ snr: 2.5, dCell: 0.45, margin: 0.05, compact: false });
  assert.ok(strong > weak, `${strong} should beat ${weak}`);
  assert.ok(strong <= 1 && weak >= 0.05);
});
