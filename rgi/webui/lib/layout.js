// Layout helpers: key codes, auto-layout from the backend's lamps, default
// lane pools, and export formats. Geometry is in keyboard units (1u = one
// standard key width); pixel positions from webcam detection live on the key
// as px/py so a layout can carry both.

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

/** A sensible grid when the backend does not describe geometry. */
export function autoLayout(lamps, columns = 13) {
  const keys = [...lamps]
    .sort((a, b) => a.index - b.index)
    .map((lamp, i) => ({
      lamp: lamp.index,
      label: lamp.label || `lamp ${lamp.index}`,
      code: null,
      x: i % columns,
      y: Math.floor(i / columns),
      w: 1,
      h: 1,
      group: lamp.group || "",
    }));
  return { format_version: 1, source: "auto", keys };
}

export function layoutFor(config, capability) {
  const entry = (config.devices || {})[capability.name] || {};
  const layout = (config.layouts || {})[entry.layout];
  if (layout && Array.isArray(layout.keys) && layout.keys.length) return layout;
  return autoLayout(capability.lamps || []);
}

export function keysByLamp(layout) {
  const map = new Map();
  for (const key of (layout && layout.keys) || []) {
    if (typeof key.lamp === "number") map.set(key.lamp, key);
  }
  return map;
}

/** Number row first (it is the proven indicator pool), then everything else. */
export function defaultLanePool(layout, count = 12) {
  const keys = (layout && layout.keys) || [];
  const preferred = keys.filter((k) => k.group === "number-row");
  const rest = keys.filter((k) => k.group !== "number-row");
  return [...preferred, ...rest].slice(0, count).map((k) => k.lamp);
}

export function laneKeys(capability, config, count = 12) {
  const layout = layoutFor(config, capability);
  const byLamp = keysByLamp(layout);
  const entry = (config.devices || {})[capability.name] || {};
  const pool = Array.isArray(entry.lane_pool) && entry.lane_pool.length
    ? entry.lane_pool
    : defaultLanePool(layout, count);
  return pool.slice(0, count).map((lamp) => {
    const key = byLamp.get(lamp);
    return { lamp, label: key ? key.label : `lamp ${lamp}`, code: key ? key.code : null };
  });
}

export function toCsv(keys) {
  const head = "lamp,code,label,x,y,w,h,source,confidence";
  const rows = keys.map((k) => [
    k.lamp, k.code || "", JSON.stringify(k.label || ""), k.x ?? "", k.y ?? "",
    k.w ?? 1, k.h ?? 1, k.source || "", k.confidence ?? "",
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
