import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { frameAt } from "../lib/effects.js";

const here = dirname(fileURLToPath(import.meta.url));
const groups = JSON.parse(readFileSync(join(here, "fixtures", "effects.json"), "utf8"));

test("effects match the Python-generated fixture exactly", () => {
  let cases = 0;
  for (const group of groups) {
    for (const item of group.cases) {
      const got = frameAt(group.config, item.state, item.elapsed, item.quiet);
      assert.deepEqual(got, item.expect, item.name);
      cases += 1;
    }
  }
  assert.ok(cases > 100, `expected a substantial fixture, got ${cases} cases`);
});

test("an unknown state follows idle when idle is configured", () => {
  const config = { appearance: { states: { idle: { color: "#282828", pattern: "steady", brightness: 255 } } } };
  assert.deepEqual(frameAt(config, "stopping", 0), [40, 40, 40]);
});

test("an empty palette renders off", () => {
  assert.deepEqual(frameAt({ appearance: { states: {} } }, "nope", 0), [0, 0, 0]);
});
