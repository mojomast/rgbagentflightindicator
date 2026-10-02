// The single source of truth for "what colour should this lamp be right now".
// It mirrors the daemon's precedence exactly:
//
//   test overlay -> lamp override -> zone override -> lane state -> off
//
// The map, the inspector and the legend all read this one function, so the UI
// cannot drift from the hardware (they share the effect maths in effects.js).

import { renderSpec, stateSpec } from "./effects.js";

export function sessionsBySlot(status) {
  const map = new Map();
  for (const session of Object.values((status && status.sessions) || {})) {
    map.set(session.slot, session);
  }
  return map;
}

export function sessionForLamp(status, capability, lampIndex) {
  const slot = (capability && capability.pool ? capability.pool : []).indexOf(lampIndex);
  if (slot < 0) return null;
  return sessionsBySlot(status).get(slot) || null;
}

export function overridesFor(config, deviceName) {
  const lamps = new Map();
  for (const override of (config && config.lamp_overrides) || []) {
    if (override && override.device === deviceName && typeof override.lamp === "number") {
      lamps.set(override.lamp, override.color);
    }
  }
  const zones = new Map();
  for (const override of (config && config.zone_overrides) || []) {
    if (override && override.device === deviceName && override.zone) {
      zones.set(override.zone, override.color);
    }
  }
  return { lamps, zones };
}

/**
 * Effective appearance for one lamp.
 * Returns { color, pattern, periodMs, duty, source, state, session }.
 */
export function effectiveLamp(config, capability, lampIndex, status, nowMs,
                              { quiet = false, zoneOfLamp = () => null,
                                overrides = null } = {}) {
  const deviceName = capability ? capability.name : "";
  const maps = overrides || overridesFor(config, deviceName);
  const lampOverride = maps.lamps.get(lampIndex);
  if (lampOverride) {
    return { color: hexToRgb(lampOverride), pattern: "steady", periodMs: 560,
             duty: 0.5, source: "lamp override", state: null, session: null };
  }
  const zone = zoneOfLamp(lampIndex);
  const zoneOverride = zone ? maps.zones.get(zone) : null;
  if (zoneOverride) {
    return { color: hexToRgb(zoneOverride), pattern: "steady", periodMs: 560,
             duty: 0.5, source: "zone override", state: null, session: null };
  }
  const session = sessionForLamp(status, capability, lampIndex);
  const state = session ? (session.state || "idle") : "off";
  if (state === "off") {
    return { color: [0, 0, 0], pattern: "off", periodMs: 560, duty: 0.5,
             source: "off", state, session };
  }
  const spec = stateSpec(config, state);
  const elapsed = session ? Math.max(0, (nowMs - (session.changedMs ?? nowMs)) / 1000) : 0;
  if (quiet || spec.pattern === "steady") {
    return { color: renderSpec({ ...spec, pattern: "steady" }, 0, true),
             pattern: "steady", periodMs: spec.periodMs, duty: spec.duty,
             source: "lane", state, session };
  }
  return { color: spec.color, pattern: spec.pattern, periodMs: spec.periodMs,
           duty: spec.duty, source: "lane", state, session };
}

export function hexToRgb(hex) {
  if (typeof hex !== "string") return [0, 0, 0];
  let text = hex.trim().replace(/^#/, "");
  if (text.length === 3) text = text.split("").map((c) => c + c).join("");
  const n = parseInt(text, 16);
  if (!Number.isFinite(n) || !/^[0-9a-fA-F]{6}$/.test(text)) return [0, 0, 0];
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

export function rgbCss(rgb) {
  return `rgb(${rgb[0]}, ${rgb[1]}, ${rgb[2]})`;
}

export function formatDuration(seconds) {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${String(m % 60).padStart(2, "0")}m`;
}

export function elapsedFor(session, nowMs) {
  if (!session) return null;
  if (session.changedMs) return (nowMs - session.changedMs) / 1000;
  return session.in_flight_s ?? session.idle_s ?? null;
}
