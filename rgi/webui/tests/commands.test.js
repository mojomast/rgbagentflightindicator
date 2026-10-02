import { test } from "node:test";
import assert from "node:assert/strict";

import { createHistory, deepClone, getPath, setPath } from "../lib/commands.js";

test("setPath and getPath round-trip", () => {
  const obj = {};
  setPath(obj, "a.b.c", 7);
  assert.equal(getPath(obj, "a.b.c"), 7);
  assert.equal(getPath(obj, "a.missing"), undefined);
});

test("deepClone does not share references", () => {
  const original = { a: { b: [1, 2] } };
  const copy = deepClone(original);
  copy.a.b.push(3);
  assert.equal(original.a.b.length, 2);
});

test("undo and redo walk the snapshot stack", () => {
  const history = createHistory();
  history.push({ name: "one" }, "first");
  history.push({ name: "two" }, "second");
  const back = history.undo({ name: "three" });
  assert.deepEqual(back, { name: "two" });
  assert.equal(history.canUndo(), true);
  const forward = history.redo({ name: "two" });
  assert.deepEqual(forward, { name: "three" });
  assert.equal(history.canRedo(), false);
});

test("rapid same-key edits coalesce into one undo step", () => {
  const history = createHistory();
  history.push({ label: "a" }, "label", "colour");
  history.push({ label: "ab" }, "label", "colour");
  history.push({ label: "abc" }, "label", "colour");
  const back = history.undo({ label: "abc" });
  assert.deepEqual(back, { label: "a" });
  assert.equal(history.canUndo(), false);
});

test("a new edit clears the redo stack", () => {
  const history = createHistory();
  history.push({ v: 1 }, "one");
  history.undo({ v: 2 });
  assert.equal(history.canRedo(), true);
  history.push({ v: 3 }, "new");
  assert.equal(history.canRedo(), false);
});
