// The keyboard map. One SVG for the whole board:
//
//   case -> underglow -> perimeter strips -> keycaps -> points/logo ->
//   selection, lane badges, override rings (the overlay)
//
// It renders the *draft* configuration through lib/effective.js, so what is on
// screen is what Apply will write. Interaction (drag, paint, marquee) is bound
// by the map pane through data attributes; this module is presentational plus
// the keyboard-accessible cursor.

import { h } from "../lib/dom.js";
import { rgbCss, effectiveLamp, overridesFor } from "../lib/effective.js";
import {
  boundsOf, caseOf, geometryOf, lampsOf, keysById, zoneOfLamp, zoneOf,
  orderedPathLamps, pointAtPath, pathD, subPathD,
} from "../lib/topology.js";

const PAD = 0.35;              // viewBox padding in units
const PERIMETER_WIDTH = 0.22;

export function renderMap({
  layout,
  capability,
  status,
  config,
  selection = new Set(),
  mode = "live",
  activeLamp = null,
  quiet = true,
  nowMs = performance.now(),
  reduceMotion = false,
  showLaneBadges = true,
  zoneVisibility = null,      // Map<zoneId, boolean>
  onActivate = null,          // (lampIndex, {additive}) for keyboard Enter/Space
} = {}) {
  const lamps = lampsOf(layout);
  const byIndex = new Map(lamps.map((lamp) => [lamp.index, lamp]));
  const keyMap = keysById(layout);
  const zoneMap = zoneOfLamp(layout);
  const overrides = overridesFor(config, capability ? capability.name : "");
  const svg = h("svg", {
    class: "map",
    role: "application",
    tabindex: "0",
    "aria-label": `Keyboard map, ${lamps.length} lamps`,
    "aria-describedby": "map-help",
  });

  const bounds = boundsOf(layout);
  const baseView = {
    x: bounds.minX - PAD,
    y: bounds.minY - PAD,
    w: Math.max(0.5, bounds.maxX - bounds.minX + PAD * 2),
    h: Math.max(0.5, bounds.maxY - bounds.minY + PAD * 2),
  };

  const defs = h("defs");
  const glow = h("filter", { id: "rgi-glow", x: "-40%", y: "-40%",
                             width: "180%", height: "180%" });
  glow.appendChild(h("feGaussianBlur", { stdDeviation: "3.2" }));
  defs.appendChild(glow);
  svg.appendChild(defs);

  const caseLayer = h("g", { class: "layer-case" });
  const glowLayer = h("g", { class: "layer-glow" });
  const stripLayer = h("g", { class: "layer-strips" });
  const keyLayer = h("g", { class: "layer-keys" });
  const pointLayer = h("g", { class: "layer-points" });
  const overlay = h("g", { class: "layer-overlay" });
  svg.append(caseLayer, glowLayer, stripLayer, keyLayer, pointLayer, overlay);

  const box = caseOf(layout);
  if (box) {
    caseLayer.appendChild(h("rect", {
      class: "case", x: box.x, y: box.y, width: box.w, height: box.h,
      rx: box.rx ?? 0.35,
    }));
  }

  const appearance = new Map();
  for (const lamp of lamps) {
    appearance.set(lamp.index, effectiveLamp(
      config, capability, lamp.index, status, nowMs,
      { quiet, zoneOfLamp: (index) => zoneMap.get(index) || null, overrides }));
  }

  const nodes = new Map();          // lamp index -> g
  const positions = new Map();      // lamp index -> {x, y} for keyboard nav
  const patternNodes = [];

  // -- keys ---------------------------------------------------------------
  const lampsByKey = new Map();
  for (const lamp of lamps) {
    if (lamp.key) {
      if (!lampsByKey.has(lamp.key)) lampsByKey.set(lamp.key, []);
      lampsByKey.get(lamp.key).push(lamp.index);
    }
  }
  for (const key of (layout && layout.keys) || []) {
    const geometry = geometryOf(key);
    if (!geometry) continue;
    const lampIndexes = [...(lampsByKey.get(key.id) || [])];
    if (typeof key.lamp === "number" && !lampIndexes.includes(key.lamp)) {
      lampIndexes.push(key.lamp);
    }
    const present = lampIndexes.filter((index) => {
      const lamp = byIndex.get(index);
      return !lamp || lamp.present !== false;
    });
    const primary = present[0] ?? null;
    const look = primary != null ? appearance.get(primary) : null;
    const group = h("g", {
      class: "key",
      "data-key": key.id,
      "data-lamps": lampIndexes.join(","),
      "data-kind": "key",
    });
    if (primary != null) {
      group.setAttribute("data-lamp", String(primary));
      group.setAttribute("id", `lamp-${primary}`);
      if (selection.has(primary)) group.classList.add("selected");
      group.setAttribute("role", "option");
      group.setAttribute("aria-selected", selection.has(primary) ? "true" : "false");
      group.setAttribute("aria-label", lampLabel(key, primary, lampIndexes.length));
      nodes.set(primary, group);
    }
    const shapes = geometry.type === "union" ? geometry.shapes : [geometry];
    for (const shape of shapes) {
      group.appendChild(keyShape(shape, box));
    }
    if (look) group.style.setProperty("--lamp-fill", rgbCss(look.color));
    if (key.label) {
      const centre = firstCentre(geometry);
      group.appendChild(h("text", {
        x: centre.x, y: centre.y + 0.13, "text-anchor": "middle",
        textLength: (geometry.w ?? 1) > 1.1 ? Math.max(0.4, (geometry.w ?? 1) - 0.45) : null,
        lengthAdjust: "spacingAndGlyphs",
      }, key.label));
    } else if (key.code) {
      const centre = firstCentre(geometry);
      group.appendChild(h("text", { x: centre.x, y: centre.y + 0.13,
                                    "text-anchor": "middle" }, key.code));
    }
    if (primary != null && look && (look.pattern === "blink" || look.pattern === "breathe")) {
      patternNodes.push({ node: group, look });
    }
    keyLayer.appendChild(group);
    if (primary != null) positions.set(primary, firstCentre(geometry));
  }

  // -- zones (perimeter strips, maybe with per-lamp subpaths) -------------
  for (const zone of (layout && layout.zones) || []) {
    if (!zone || !zone.id) continue;
    if (zoneVisibility && zoneVisibility.get(zone.id) === false) continue;
    const geometry = geometryOf(zone);
    if (!geometry || geometry.type !== "path" || (geometry.points || []).length < 2) {
      continue;
    }
    const closed = Boolean(geometry.closed);
    const width = geometry.width ?? PERIMETER_WIDTH;
    const ordered = orderedPathLamps(layout, zone, lamps);
    const visible = ordered.filter(({ index }) => {
      const lamp = byIndex.get(index);
      return !lamp || lamp.present !== false;
    });
    const uniform = zone.addressable === false || visible.length === 1;

    // glow underlay
    const firstLook = visible.length ? appearance.get(visible[0].index) : null;
    const underlay = h("path", {
      class: "strip-glow",
      d: pathD(geometry.points, closed),
      "stroke-width": width * 3.2,
    });
    if (firstLook) underlay.style.setProperty("--lamp-fill", rgbCss(firstLook.color));
    glowLayer.appendChild(underlay);

    const fractions = new Map();
    void fractions;
    if (uniform) {
      const indexes = visible.map((entry) => entry.index);
      const look = appearance.get(indexes[0]);
      const path = h("path", {
        class: "strip",
        d: pathD(geometry.points, closed),
        "stroke-width": width,
        "data-zone": zone.id,
      });
      if (look) path.style.setProperty("--lamp-fill", rgbCss(look.color));
      if (indexes.includes(activeLamp)) path.classList.add("active");
      stripLayer.appendChild(path);
      if (indexes.length) {
        const point = pointAtPath(geometry.points, closed, 0.5);
        positions.set(indexes[0], point);
        nodes.set(indexes[0], path);
      }
      continue;
    }

    const count = visible.length;
    visible.forEach((entry, rank) => {
      const t = typeof entry.t === "number" ? entry.t : rank / count;
      const nextT = rank + 1 < visible.length
        ? (typeof visible[rank + 1].t === "number"
            ? visible[rank + 1].t : (rank + 1) / count)
        : (typeof visible[0].t === "number" ? visible[0].t : 0) + (closed ? 1 : 1 - t);
      const span = (((nextT - t) % 1) + 1) % 1 || 1 / count;
      const end = closed ? (t + span * 0.92) % 1 : Math.min(1, t + span * 0.92);
      const look = appearance.get(entry.index);
      const d = subPathD(geometry.points, closed, t, end);
      const node = h("path", {
        class: "strip",
        d,
        "stroke-width": width,
        "data-lamp": entry.index,
        "data-zone": zone.id,
        "data-kind": "perimeter",
        id: `lamp-${entry.index}`,
        role: "option",
        "aria-selected": selection.has(entry.index) ? "true" : "false",
        "aria-label": `Lamp ${entry.index}, ${zone.label || zone.id}, ` +
          (look && look.state ? look.state : "off"),
      });
      node.dataset.t = String(t);
      if (look) node.style.setProperty("--lamp-fill", rgbCss(look.color));
      if (selection.has(entry.index)) node.classList.add("selected");
      if (entry.index === activeLamp) node.classList.add("active");
      if (look && (look.pattern === "blink" || look.pattern === "breathe")) {
        patternNodes.push({ node, look });
      }
      stripLayer.appendChild(node);
      nodes.set(entry.index, node);
      positions.set(entry.index, pointAtPath(geometry.points, closed, t));
    });

    if (mode === "layout") {
      geometry.points.forEach((point, index) => {
        overlay.appendChild(h("circle", {
          class: "zone-handle",
          "data-zone": zone.id,
          "data-zone-point": String(index),
          cx: point[0], cy: point[1], r: 0.09,
        }));
      });
    }
  }

  // -- lamps with no key and no strip: points, logos, indicators ----------
  for (const lamp of lamps) {
    if (nodes.has(lamp.index)) continue;
    if (lamp.zone && zoneOf(layout, lamp.zone)) continue;
    if (lamp.present === false) {
      const geometry = geometryOf(lamp);
      const x = geometry ? (geometry.x ?? 0) : 0;
      const y = geometry ? (geometry.y ?? 0) : 0;
      overlay.appendChild(h("circle", { class: "lamp-reserved", cx: x, cy: y, r: 0.1 }));
      continue;
    }
    const geometry = geometryOf(lamp) || { type: "point", x: 0, y: -0.5, size: 0.16 };
    const look = appearance.get(lamp.index);
    const node = h("g", {
      class: `point kind-${lamp.kind || "unknown"}`,
      "data-lamp": lamp.index,
      "data-kind": lamp.kind || "unknown",
      id: `lamp-${lamp.index}`,
      role: "option",
      "aria-selected": selection.has(lamp.index) ? "true" : "false",
      "aria-label": `Lamp ${lamp.index}, ${lamp.kind || "unknown"}`,
    });
    if (look) node.style.setProperty("--lamp-fill", rgbCss(look.color));
    if (geometry.type === "point") {
      node.appendChild(h("circle", { class: "point-shape",
                                     cx: geometry.x, cy: geometry.y,
                                     r: Math.max(0.06, (geometry.size ?? 0.16) / 2) }));
      positions.set(lamp.index, { x: geometry.x, y: geometry.y });
    } else {
      const centre = firstCentre(geometry);
      const shape = keyShape(geometry, box);
      shape.classList.add("point-shape");
      node.appendChild(shape);
      positions.set(lamp.index, centre);
    }
    if (selection.has(lamp.index)) node.classList.add("selected");
    if (lamp.index === activeLamp) node.classList.add("active");
    if (look && (look.pattern === "blink" || look.pattern === "breathe")) {
      patternNodes.push({ node, look });
    }
    pointLayer.appendChild(node);
    nodes.set(lamp.index, node);
    if (lamp.label && !/^\d+$/.test(lamp.label)) {
      const centre = positions.get(lamp.index);
      pointLayer.appendChild(h("text", { class: "point-label", x: centre.x,
                                         y: centre.y - 0.14, "text-anchor": "middle" },
                               lamp.label));
    }
  }

  // -- overlay: lane badges, override rings ------------------------------
  const pool = (capability && capability.pool) || [];
  for (const [index, node] of nodes) {
    const lamp = byIndex.get(index);
    if (lamp && lamp.present === false) continue;
    const centre = positions.get(index);
    if (!centre) continue;
    if (overrides.lamps.has(index)) {
      overlay.appendChild(h("circle", { class: "override-ring", cx: centre.x,
                                        cy: centre.y, r: 0.24 }));
    } else {
      const zoneId = zoneMap.get(index);
      if (zoneId && overrides.zones.has(zoneId)) {
        overlay.appendChild(h("circle", { class: "override-ring", cx: centre.x,
                                          cy: centre.y, r: 0.24 }));
      }
    }
    const lane = pool.indexOf(index);
    if (showLaneBadges && lane >= 0) {
      overlay.appendChild(h("g", { class: "lane-badge" },
        h("circle", { cx: centre.x + 0.24, cy: centre.y - 0.24, r: 0.13 }),
        h("text", { x: centre.x + 0.24, y: centre.y - 0.195, "text-anchor": "middle" },
          String(lane))));
    }
  }

  startPatterns(patternNodes, reduceMotion);

  // -- keyboard cursor ----------------------------------------------------
  const order = navigableOrder(layout, lamps, positions, zoneMap);
  let cursor = order.indexOf(activeLamp);
  if (cursor < 0) cursor = 0;
  const setCursor = (next) => {
    if (!order.length) return;
    cursor = (next + order.length) % order.length;
    const lamp = order[cursor];
    for (const node of nodes.values()) node.classList.remove("active");
    nodes.get(lamp)?.classList.add("active");
    svg.setAttribute("aria-activedescendant", `lamp-${lamp}`);
    onActivate?.(lamp, { focusOnly: true });
  };
  if (activeLamp != null) svg.setAttribute("aria-activedescendant", `lamp-${activeLamp}`);
  svg.addEventListener("keydown", (event) => {
    if (!order.length) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onActivate?.(order[cursor], { additive: event.shiftKey });
      return;
    }
    const step = event.key === "ArrowRight" || event.key === "ArrowDown" ? 1
      : event.key === "ArrowLeft" || event.key === "ArrowUp" ? -1
      : event.key === "Home" ? -Infinity : event.key === "End" ? Infinity : null;
    if (step === null) return;
    event.preventDefault();
    if (step === -Infinity) setCursor(0);
    else if (step === Infinity) setCursor(order.length - 1);
    else setCursor(cursor + (event.shiftKey ? step * 3 : step));
  });
  svg.addEventListener("click", (event) => {
    const target = event.target.closest("[data-lamp]");
    if (!target) return;
    const lamp = Number(target.getAttribute("data-lamp"));
    onActivate?.(lamp, { additive: event.shiftKey });
  });

  // -- viewport -----------------------------------------------------------
  const applyViewport = ({ zoom = 1, panX = 0, panY = 0 } = {}) => {
    const w = baseView.w / zoom;
    const h = baseView.h / zoom;
    const x = baseView.x + (baseView.w - w) / 2 + panX * baseView.w;
    const y = baseView.y + (baseView.h - h) / 2 + panY * baseView.h;
    svg.setAttribute("viewBox", `${x} ${y} ${w} ${h}`);
    svg.classList.toggle("zoomed", zoom > 1.25);
  };
  applyViewport();

  return {
    svg,
    applyViewport,
    baseView,
    focusLamp(index) {
      const position = positions.get(index);
      if (!position) return;
      svg.dispatchEvent(new CustomEvent("rgi-focus", { detail: position }));
    },
    positions,
    nodes,
    order,
  };
}

function lampLabel(key, lampIndex, count) {
  const name = key.label || key.code || key.id;
  const suffix = count > 1 ? `, ${count} lamps` : "";
  return `${name} — lamp ${lampIndex}${suffix}`;
}

function firstCentre(geometry) {
  if (!geometry) return { x: 0, y: 0 };
  if (geometry.type === "union" && geometry.shapes && geometry.shapes[0]) {
    return firstCentre(geometry.shapes[0]);
  }
  if (geometry.type === "point") {
    return { x: geometry.x, y: geometry.y };
  }
  return { x: (geometry.x ?? 0) + (geometry.w ?? 1) / 2,
           y: (geometry.y ?? 0) + (geometry.h ?? 1) / 2 };
}

function keyShape(shape, box) {
  if (!shape || typeof shape !== "object") {
    return h("rect", { x: 0, y: 0, width: 1, height: 1, rx: 0.08 });
  }
  const gap = 0.06;
  if (shape.type === "point") {
    return h("circle", { cx: shape.x, cy: shape.y,
                         r: Math.max(0.05, (shape.size ?? 0.14) / 2) });
  }
  const x = (shape.x ?? 0) + gap / 2;
  const y = (shape.y ?? 0) + gap / 2;
  const w = Math.max(0.1, (shape.w ?? 1) - gap);
  const hh = Math.max(0.1, (shape.h ?? 1) - gap);
  const rect = h("rect", { x, y, width: w, height: hh, rx: 0.08 });
  if (shape.rotation && typeof shape.rotation.angle === "number") {
    const pivotX = (shape.rotation.rx ?? shape.x ?? 0);
    const pivotY = (shape.rotation.ry ?? shape.y ?? 0);
    rect.setAttribute("transform",
      `rotate(${shape.rotation.angle} ${pivotX} ${pivotY})`);
  }
  void box;
  return rect;
}

function startPatterns(entries, reduceMotion) {
  if (reduceMotion || typeof Element.prototype.animate !== "function") return;
  for (const { node, look } of entries) {
    const period = Math.max(40, look.periodMs || 560);
    if (look.pattern === "blink") {
      const duty = Math.max(0.05, Math.min(0.95, look.duty ?? 0.5));
      node.style.setProperty("opacity", "1");
      node.animate([
        { opacity: 1, offset: 0 },
        { opacity: 1, offset: duty },
        { opacity: 0.12, offset: duty },
        { opacity: 0.12, offset: 1 },
      ], { duration: period, iterations: Infinity });
    } else if (look.pattern === "breathe") {
      const frames = [];
      for (let i = 0; i <= 12; i += 1) {
        const phase = i / 12;
        const level = 0.18 + 0.82 * (0.5 - 0.5 * Math.cos(2 * Math.PI * phase));
        frames.push({ opacity: level, offset: phase });
      }
      node.animate(frames, { duration: period, iterations: Infinity });
    }
  }
}

function navigableOrder(layout, lamps, positions, zoneMap) {
  const keyLamps = [];
  for (const key of (layout && layout.keys) || []) {
    const geometry = geometryOf(key);
    if (!geometry) continue;
    const centre = firstCentre(geometry);
    const indexes = lamps.filter((lamp) => lamp.key === key.id).map((l) => l.index);
    if (!indexes.length && typeof key.lamp === "number") indexes.push(key.lamp);
    for (const index of indexes) {
      keyLamps.push({ index, x: centre.x, y: centre.y, zone: zoneMap.get(index) || null });
    }
  }
  keyLamps.sort((a, b) => (a.y - b.y) || (a.x - b.x));
  const keyed = new Set(keyLamps.map((entry) => entry.index));
  const rest = lamps
    .filter((lamp) => !keyed.has(lamp.index))
    .map((lamp) => {
      const position = positions.get(lamp.index) || { x: 0, y: 0 };
      const zone = zoneMap.get(lamp.index) || "~";
      const t = lamp.path && typeof lamp.path.t === "number" ? lamp.path.t : 0.5;
      return { index: lamp.index, zone, t, x: position.x, y: position.y };
    });
  rest.sort((a, b) => String(a.zone).localeCompare(String(b.zone)) ||
                      (a.t - b.t) || (a.y - b.y) || (a.x - b.x));
  return [...keyLamps.map((entry) => entry.index), ...rest.map((entry) => entry.index)];
}
