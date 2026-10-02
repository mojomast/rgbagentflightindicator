// Presentation helpers around the topology model: labels, pools, exports and
// the client-side resolution of "which layout is this device using".

import { autoLayout, keysById, lampsOf } from "./topology.js";

export { autoLayout, migrateLayout } from "./topology.js";

const LABELS = {
  Backquote: "`", Minus: "-", Equal: "=", Backspace: "Backspace", Tab: "Tab",
  BracketLeft: "[", BracketRight: "]", Backslash: "\\", CapsLock: "Caps",
  Semicolon: ";", Quote: "'", Enter: "Enter", ShiftLeft: "Shift",
  ShiftRight: "Shift", Comma: ",", Period: ".", Slash: "/", Space: "Space",
  ControlLeft: "Ctrl", ControlRight: "Ctrl", AltLeft: "Alt", AltRight: "Alt",
  MetaLeft: "Meta", MetaRight: "Meta", Escape: "Esc",
  ArrowUp: "↑", ArrowDown: "↓", ArrowLeft: "←", ArrowRight: "→",
  Insert: "Ins", Delete: "Del", Home: "Home", End: "End", PageUp: "PgUp",
  PageDown: "PgDn", PrintScreen: "PrtSc", ScrollLock: "ScrLk", Pause: "Pause",
  ContextMenu: "Menu", NumLock: "Num", NumpadDivide: "/", NumpadMultiply: "*",
  NumpadSubtract: "-", NumpadAdd: "+", NumpadEnter: "Enter",
  NumpadDecimal: ".", NumpadComma: ",",
};

export function codeLabel(code) {
  if (!code) return "";
  if (LABELS[code]) return LABELS[code];
  if (code.startsWith("Key")) return code.slice(3);
  if (code.startsWith("Digit")) return code.slice(5);
  if (code.startsWith("Numpad")) return "Num " + code.slice(6);
  if (code.startsWith("F") && /^F\d{1,2}$/.test(code)) return code;
  return code;
}

/** The device's own layout, or an honest auto-grid of its lamps. */
export function resolveLayout(config, capability) {
  const entry = (config.devices || {})[capability.name] || {};
  const layout = entry.layout && (config.layouts || {})[entry.layout];
  if (layout && Array.isArray(layout.keys) && layout.keys.length) return layout;
  return autoLayout(capability.lamps || []);
}

/** v0.6 name; kept so older views keep working while the UI is rebuilt. */
export const layoutFor = resolveLayout;

export function keysByLamp(layout) {
  const map = new Map();
  const keys = keysById(layout);
  for (const lamp of lampsOf(layout)) {
    if (lamp.key && keys.has(lamp.key)) map.set(lamp.index, keys.get(lamp.key));
  }
  for (const key of (layout && layout.keys) || []) {
    if (typeof key.lamp === "number" && !map.has(key.lamp)) map.set(key.lamp, key);
  }
  return map;
}

/** Lamps that can carry lanes: number row first, then keys, then anything. */
export function defaultLanePool(layout, count = 12) {
  const keys = keysByLamp(layout);
  const ranked = lampsOf(layout)
    .filter((lamp) => lamp.present !== false)
    .map((lamp) => {
      const keyRecord = keys.get(lamp.index);
      const group = (keyRecord && keyRecord.group) || "";
      const rank = group === "number-row" ? 0
        : group === "function-row" ? 1
        : lamp.kind === "key" ? 2
        : lamp.kind === "perimeter" ? 4 : 3;
      return { index: lamp.index, rank };
    })
    .sort((a, b) => (a.rank - b.rank) || (a.index - b.index));
  return ranked.slice(0, count).map((entry) => entry.index);
}

export function laneKeys(capability, config, count = 12) {
  const layout = resolveLayout(config, capability);
  const byLamp = keysByLamp(layout);
  const entry = (config.devices || {})[capability.name] || {};
  const pool = Array.isArray(entry.lane_pool) && entry.lane_pool.length
    ? entry.lane_pool
    : defaultLanePool(layout, count);
  return pool.slice(0, count).map((lamp) => {
    const key = byLamp.get(lamp);
    return { lamp, label: key ? (key.label || key.code || key.id) : `lamp ${lamp}`,
             code: (key && key.code) || null };
  });
}

export function toCsv(keys) {
  const head = "lamp,kind,key,code,label,x,y,w,h,zone,t,source,confidence";
  const rows = keys.map((k) => [
    k.index ?? k.lamp ?? "", k.kind || "", k.key || "", k.code || "",
    JSON.stringify(k.label || ""), k.geometry ? "" : (k.x ?? ""),
    k.geometry ? "" : (k.y ?? ""), k.geometry ? "" : (k.w ?? ""),
    k.geometry ? "" : (k.h ?? ""), k.zone || "",
    (k.path && k.path.t != null) ? k.path.t : "",
    k.source || "", k.confidence ?? "",
  ].join(","));
  return [head, ...rows].join("\n") + "\n";
}

export function download(name, text, type = "application/json") {
  const blob = new Blob([text], { type });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
