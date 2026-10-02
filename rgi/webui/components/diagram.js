// SVG keyboard diagram: one node per lamp, keyboard navigable, CSS-styled.
// At 126 keys this is far below the scale where SVG struggles, and every key
// keeps an accessible name, hover title and focus ring.

import { h } from "../lib/dom.js";
import { rgbToHex } from "../lib/color.js";

const CELL = 34;
const GAP = 2;

export function renderDiagram({
  layout,
  stateFor = () => ({}),
  selected = new Set(),
  laneSet = new Set(),
  overrides = new Map(),
  quiet = false,
  blinkPeriod = 560,
  onSelect = null,
  labelFor = null,
} = {}) {
  const keys = (layout && layout.keys) || [];
  const cols = Math.max(1, ...keys.map((k) => (k.x || 0) + (k.w || 1)));
  const rows = Math.max(1, ...keys.map((k) => (k.y || 0) + (k.h || 1)));
  const width = cols * CELL + (cols + 1) * GAP;
  const height = rows * CELL + (rows + 1) * GAP;
  const svg = h("svg", {
    class: "keyboard",
    viewBox: `0 0 ${width} ${height}`,
    role: "group",
    "aria-label": "Keyboard diagram",
    style: { "--blink-period": `${blinkPeriod}ms` },
  });

  const order = [...keys].sort((a, b) => (a.y - b.y) || (a.x - b.x));
  const nodes = [];

  order.forEach((key, index) => {
    if (typeof key.lamp !== "number") return;
    const { state = "off", override = null, blink = false } = stateFor(key.lamp) || {};
    const x = GAP + (key.x || 0) * (CELL + GAP);
    const y = GAP + (key.y || 0) * (CELL + GAP);
    const w = (key.w || 1) * CELL + ((key.w || 1) - 1) * GAP;
    const ht = (key.h || 1) * CELL + ((key.h || 1) - 1) * GAP;
    const isSelected = selected.has(key.lamp);
    const group = h("g", {
      class: `key${isSelected ? " selected" : ""}${laneSet.has(key.lamp) ? " lane" : ""}`,
      "data-state": override ? "override" : state,
      "data-blink": (!quiet && blink) ? "true" : null,
      "data-override": override ? "true" : null,
      "data-lamp": key.lamp,
      role: "button",
      tabindex: index === 0 ? "0" : "-1",
      "aria-label": `${labelFor ? labelFor(key) : key.label || `lamp ${key.lamp}`} — ${state}`,
      onclick: onSelect ? () => onSelect(key, { additive: false }) : null,
      onkeydown: onSelect ? (event) => {
        let next = null;
        const current = nodes.indexOf(event.currentTarget);
        if (event.key === "ArrowRight") next = current + 1;
        if (event.key === "ArrowLeft") next = current - 1;
        if (event.key === "ArrowDown") next = findBelow(order, key, 1);
        if (event.key === "ArrowUp") next = findBelow(order, key, -1);
        if (event.key === "Home") next = 0;
        if (event.key === "End") next = nodes.length - 1;
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onSelect(key, { additive: event.shiftKey });
          return;
        }
        if (next == null || next < 0 || next >= nodes.length) return;
        event.preventDefault();
        nodes.forEach((node) => node.setAttribute("tabindex", "-1"));
        nodes[next].setAttribute("tabindex", "0");
        nodes[next].focus();
        onSelect(order[next], { additive: event.shiftKey, focusOnly: true });
      } : null,
    });
    const rect = h("rect", { x, y, width: w, height: ht, rx: 5 });
    if (override) rect.setAttribute("fill", rgbToHex(override));
    group.appendChild(rect);
    group.appendChild(h("text", {
      x: x + w / 2, y: y + ht / 2 + 3, "text-anchor": "middle",
    }, key.label || ""));
    const title = h("title", {}, `${labelFor ? labelFor(key) : key.label || key.lamp} (lamp ${key.lamp})`);
    group.appendChild(title);
    svg.appendChild(group);
    nodes.push(group);
  });

  svg.focusKey = (lamp) => {
    const node = nodes.find((n) => Number(n.dataset.lamp) === Number(lamp));
    node?.focus();
  };
  return svg;
}

function findBelow(order, key, direction) {
  const targetY = (key.y || 0) + direction;
  let best = null;
  let bestDistance = Infinity;
  for (const candidate of order) {
    if (typeof candidate.lamp !== "number") continue;
    if ((candidate.y || 0) !== targetY) continue;
    const distance = Math.abs((candidate.x || 0) - (key.x || 0));
    if (distance < bestDistance) { best = candidate; bestDistance = distance; }
  }
  return best ? order.indexOf(best) : null;
}

/** Session per lane slot, for painting the diagram from a /status snapshot. */
export function sessionsBySlot(status) {
  const map = new Map();
  for (const session of Object.values(status?.sessions || {})) {
    map.set(session.slot, session);
  }
  return map;
}

export function stateForSlot(session) {
  const state = session ? (session.state || "idle") : "off";
  return { state, blink: state === "done" || state === "blocked" || state === "error" };
}
