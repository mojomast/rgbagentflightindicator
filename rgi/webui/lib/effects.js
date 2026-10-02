// The browser mirror of rgi/appearance.py. It must produce the same RGB for
// the same state, config and elapsed time as the daemon; the cross-language
// fixture test (tests/fixtures/effects.json) holds the two together.

import { hexToRgb } from "./color.js";

const INT = (value, fallback, low, high) => {
  if (typeof value !== "number" || !Number.isFinite(value)) return fallback;
  return Math.max(low, Math.min(high, Math.trunc(value)));
};

const FLOAT = (value, fallback, low, high) => {
  if (typeof value !== "number" || !Number.isFinite(value)) return fallback;
  return Math.max(low, Math.min(high, value));
};

export function scale(colour, brightness) {
  if (brightness >= 255) return [colour[0], colour[1], colour[2]];
  if (brightness <= 0) return [0, 0, 0];
  const factor = brightness / 255;
  return colour.map((c) => Math.max(0, Math.min(255, Math.round(c * factor))));
}

export function stateSpec(config, name) {
  const appearance = (config && config.appearance) || {};
  const blink = appearance.blink || {};
  const raw = (appearance.states || {})[name] || {};
  const globalPeriod = INT(blink.period_ms, 560, 40, 10000);
  const globalDuty = FLOAT(blink.duty, 0.5, 0, 1);
  return {
    color: hexToRgb(raw.color || "#000000"),
    pattern: typeof raw.pattern === "string" ? raw.pattern : "steady",
    periodMs: INT(raw.period_ms, globalPeriod, 40, 10000),
    duty: FLOAT(raw.duty, globalDuty, 0, 1),
    cycles: INT(raw.cycles, 0, 0, 1000000),
    then: raw.then === "off" ? "off" : "steady",
    brightness: INT(raw.brightness, 255, 0, 255),
  };
}

export function renderSpec(spec, elapsed, quiet = false) {
  const base = scale(spec.color, spec.brightness);
  if (spec.pattern === "off" || (base[0] === 0 && base[1] === 0 && base[2] === 0)) return [0, 0, 0];
  if (quiet || spec.pattern === "steady") return base;
  const period = Math.max(0.04, spec.periodMs / 1000);
  if (spec.cycles && elapsed >= spec.cycles * period) {
    return spec.then === "steady" ? base : [0, 0, 0];
  }
  const phase = (Math.max(0, elapsed) % period) / period;
  if (spec.pattern === "blink") {
    return phase < Math.max(0, Math.min(1, spec.duty)) ? base : [0, 0, 0];
  }
  if (spec.pattern === "breathe") {
    const level = 0.5 - 0.5 * Math.cos(2 * Math.PI * phase);
    return scale(base, Math.max(1, Math.round(level * 255)));
  }
  return base;
}

export function frameAt(config, state, elapsed, quiet = false) {
  const states = (config && config.appearance && config.appearance.states) || {};
  // mirrors Appearance.spec(): an unknown state follows idle
  const name = Object.prototype.hasOwnProperty.call(states, state) ? state : "idle";
  return renderSpec(stateSpec(config, name), elapsed, quiet);
}
