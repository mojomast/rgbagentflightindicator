// The shared map surface used by Live, Layout, Paint, Identify and Mapping.
//
// It owns zoom/pan, zone visibility, selection gestures, the tooltip and the
// selection HUD; views own what the modes mean. All edits go through
// ctx.update/ctx.replaceDraft, so undo, drafts and Apply behave identically
// wherever the change was made.

import { h, clear } from "../lib/dom.js";
import { renderMap } from "./diagram.js";
import { resolveLayout } from "../lib/layout.js";
import { effectiveLamp, hexToRgb, rgbCss } from "../lib/effective.js";
import {
  geometryOf, lampsOf, orderedPathLamps, subPathD, pathD, zoneOfLamp,
} from "../lib/topology.js";

const SNAP = 0.25;

export function createMapPane(ctx, opts = {}) {
  const mode = opts.mode || "live";
  const capability = opts.capability;
  const state = {
    zoom: 1,
    panX: 0,
    panY: 0,
    map: null,
    tool: opts.paintTool || "override",
    paintColor: "#00aaff",
    nextLane: null,
    zoneVisibility: loadZoneVisibility(capability ? capability.name : ""),
    drag: null,
    marquee: null,
  };

  const toolbar = h("div", { class: "map-toolbar" });
  const viewport = h("div", { class: "map-viewport" });
  const hud = h("div", { class: "map-hud" });
  const tooltip = h("div", { class: "map-tooltip", hidden: true });
  viewport.append(hud, tooltip);
  const el = h("div", { class: "map-pane" }, toolbar, viewport);

  function refresh() {
    if (!ctx.draft || !capability) return;
    const layout = resolveLayout(ctx.draft, capability);
    renderToolbar(layout);
    clear(viewport);
    viewport.append(hud, tooltip);
    const selection = new Set(ctx.selection);
    state.map = renderMap({
      layout,
      capability,
      status: ctx.status,
      config: ctx.draft,
      selection,
      mode,
      activeLamp: opts.activeLamp ? opts.activeLamp() : null,
      quiet: ctx.quiet(),
      nowMs: performance.now(),
      reduceMotion: ctx.reduceMotion(),
      zoneVisibility: state.zoneVisibility,
      showLaneBadges: mode !== "layout" || true,
      onActivate: (lamp, info) => {
        if (info && info.focusOnly) return;
        handleActivate(lamp, info);
      },
    });
    viewport.insertBefore(state.map.svg, hud);
    state.map.applyViewport(state);
    renderOverlayDots(layout);
    bindEvents();
    renderHud();
  }

  function renderToolbar(layout) {
    clear(toolbar);
    const zones = [{ id: null, label: `All ${lampsOf(layout).length}` }, ...(layout.zones || [])];
    for (const zone of zones) {
      const count = zone.id
        ? (zone.lamps || []).length
        : lampsOf(layout).length;
      const visible = state.zoneVisibility.get(zone.id) !== false;
      toolbar.appendChild(h("button", {
        class: `zone-chip${visible ? "" : " off"}`,
        type: "button",
        title: zone.id ? `Show/hide zone ${zone.label || zone.id}` : "Show every lamp",
        onclick: () => {
          if (!zone.id) { state.zoneVisibility.clear(); }
          else state.zoneVisibility.set(zone.id, !visible);
          saveZoneVisibility(capability ? capability.name : "", state.zoneVisibility);
          refresh();
          opts.onZoneToggle?.(zone.id, !visible);
        },
      }, `${zone.label || zone.id} ${count}`));
    }
    toolbar.appendChild(h("span", { class: "spacer" }));
    if (mode === "paint") {
      const tools = [["override", "Override"], ["lane", "Lane"], ["erase", "Erase"]];
      for (const [id, label] of tools) {
        toolbar.appendChild(h("button", {
          class: `btn xs${state.tool === id ? " primary" : " ghost"}`,
          type: "button",
          onclick: () => { state.tool = id; refresh(); },
        }, label));
      }
      const colour = h("input", {
        type: "color", value: state.paintColor, "aria-label": "Paint colour",
        oninput: (event) => { state.paintColor = event.target.value; },
      });
      toolbar.appendChild(colour);
      if (state.tool === "lane") {
        toolbar.appendChild(h("span", { class: "muted small" },
          state.nextLane == null ? "Click lamps in lane order" : `next lane ${state.nextLane}`));
        toolbar.appendChild(h("button", {
          class: "btn xs ghost", type: "button",
          onclick: () => { state.nextLane = 0; refresh(); },
        }, "Restart lane order"));
      }
    }
    toolbar.appendChild(h("button", {
      class: "btn xs ghost", type: "button", title: "Zoom out",
      onclick: () => { state.zoom = Math.max(0.4, state.zoom / 1.25); state.map?.applyViewport(state); },
    }, "−"));
    toolbar.appendChild(h("button", {
      class: "btn xs ghost", type: "button", title: "Fit to width",
      onclick: () => { state.zoom = 1; state.panX = 0; state.panY = 0;
                       state.map?.applyViewport(state); },
    }, `${Math.round(state.zoom * 100)}%`));
    toolbar.appendChild(h("button", {
      class: "btn xs ghost", type: "button", title: "Zoom in",
      onclick: () => { state.zoom = Math.min(6, state.zoom * 1.25); state.map?.applyViewport(state); },
    }, "+"));
  }

  function renderOverlayDots(layout) {
    if (!opts.identifyOverlay) return;
    const overlay = opts.identifyOverlay();
    if (!overlay || !overlay.size) return;
    const svg = state.map?.svg;
    if (!svg) return;
    const layer = svg.querySelector(".layer-overlay") || svg;
    for (const [lamp, dot] of overlay) {
      layer.appendChild(h("g", { class: `detect kind-${dot.kind || "unknown"}` },
        h("circle", { cx: dot.x, cy: dot.y, r: 0.1 }),
        h("circle", { class: "detect-ring", cx: dot.x, cy: dot.y,
                      r: 0.12 + 0.18 * (dot.confidence ?? 0.5) }),
        h("title", null, `lamp ${lamp} · ${dot.kind || "?"} · ` +
          `${Math.round((dot.confidence ?? 0) * 100)}%`)));
    }
    void layout;
  }

  function renderHud() {
    clear(hud);
    const count = ctx.selection.length;
    if (!opts.hud) { hud.hidden = count === 0; return; }
    const actions = opts.hud(count) || [];
    if (!actions.length && !count) { hud.hidden = true; return; }
    hud.hidden = false;
    hud.appendChild(h("span", { class: "hud-count" },
      `${count} lamp${count === 1 ? "" : "s"} selected`));
    for (const action of actions) hud.appendChild(action);
  }

  // -- gestures -----------------------------------------------------------
  function unitPoint(event) {
    const svg = state.map.svg;
    const ctm = svg.getScreenCTM();
    if (!ctm) return { x: 0, y: 0 };
    const point = new DOMPoint(event.clientX, event.clientY)
      .matrixTransform(ctm.inverse());
    return { x: point.x, y: point.y };
  }

  function handleActivate(lamp, info = {}) {
    if (opts.onActivate) { opts.onActivate(lamp, info); return; }
    select(lamp, info.additive);
  }

  function select(lamp, additive) {
    const current = new Set(ctx.selection);
    if (additive) {
      if (current.has(lamp)) current.delete(lamp); else current.add(lamp);
    } else {
      current.clear();
      current.add(lamp);
    }
    ctx.setSelection([...current]);
    // update classes in place: a full re-render here would detach the node
    // mid-gesture and break pointer capture during a drag
    for (const [index, node] of state.map ? state.map.nodes : []) {
      node.classList.toggle("selected", current.has(index));
      if (node.getAttribute("role")) {
        node.setAttribute("aria-selected", current.has(index) ? "true" : "false");
      }
    }
    renderHud();
    opts.onSelectionChange?.([...current]);
  }

  function bindEvents() {
    const svg = state.map.svg;
    svg.addEventListener("pointerdown", onPointerDown);
    svg.addEventListener("pointermove", onPointerMove);
    svg.addEventListener("pointerleave", () => { tooltip.hidden = true; });
    svg.addEventListener("pointerup", onPointerUp);
    svg.addEventListener("wheel", (event) => {
      if (event.ctrlKey || event.metaKey) {
        event.preventDefault();
        state.zoom = Math.max(0.4, Math.min(6,
          state.zoom * (event.deltaY < 0 ? 1.12 : 1 / 1.12)));
        state.map.applyViewport(state);
      }
    }, { passive: false });
  }

  function lampFromEvent(event) {
    const target = event.target.closest("[data-lamp]");
    if (!target) return null;
    return Number(target.getAttribute("data-lamp"));
  }

  function onPointerDown(event) {
    if (event.button !== 0) return;
    const svg = state.map.svg;
    const point = unitPoint(event);
    // zone vertex drag (layout mode)
    const handle = event.target.closest("[data-zone-point]");
    if (handle && mode === "layout") {
      state.drag = { kind: "zone-point", zone: handle.getAttribute("data-zone"),
                     index: Number(handle.getAttribute("data-zone-point")),
                     start: point, node: handle };
      svg.setPointerCapture(event.pointerId);
      return;
    }
    const lamp = lampFromEvent(event);
    if (mode === "paint" && lamp != null) {
      paint(lamp);
      state.drag = { kind: "paint" };
      svg.setPointerCapture(event.pointerId);
      return;
    }
    if (lamp != null) {
      const selected = ctx.selection.includes(lamp);
      if (!selected) select(lamp, event.shiftKey);
      else if (mode === "live") select(lamp, event.shiftKey);
      if (mode === "layout") {
        const layout = resolveLayout(ctx.draft, capability);
        state.drag = { kind: "move", start: point, layout,
                       lamps: new Set(ctx.selection), moved: false };
        svg.setPointerCapture(event.pointerId);
      }
      return;
    }
    if (mode === "layout" && (event.target.closest("svg") === svg)) {
      state.drag = { kind: "marquee", start: point };
      state.marquee = h("rect", { class: "marquee", x: point.x, y: point.y,
                                  width: 0, height: 0 });
      svg.querySelector(".layer-overlay")?.appendChild(state.marquee);
      svg.setPointerCapture(event.pointerId);
      return;
    }
    if (!event.shiftKey) {
      ctx.setSelection([]);
      refresh();
    }
  }

  function onPointerMove(event) {
    const hovered = lampFromEvent(event);
    if (hovered != null) {
      showTooltip(event, hovered);
    } else {
      tooltip.hidden = true;
    }
    if (!state.drag) return;
    const point = unitPoint(event);
    if (state.drag.kind === "move") {
      const dx = snap(point.x - state.drag.start.x, event.ctrlKey);
      const dy = snap(point.y - state.drag.start.y, event.ctrlKey);
      if (dx || dy) state.drag.moved = true;
      for (const lamp of state.drag.lamps) {
        const node = state.map.nodes.get(lamp);
        if (node) node.setAttribute("transform", `translate(${dx} ${dy})`);
      }
    } else if (state.drag.kind === "marquee") {
      const start = state.drag.start;
      state.marquee.setAttribute("x", Math.min(start.x, point.x));
      state.marquee.setAttribute("y", Math.min(start.y, point.y));
      state.marquee.setAttribute("width", Math.abs(point.x - start.x));
      state.marquee.setAttribute("height", Math.abs(point.y - start.y));
    } else if (state.drag.kind === "zone-point") {
      const dx = snap(point.x - state.drag.start.x, event.ctrlKey);
      const dy = snap(point.y - state.drag.start.y, event.ctrlKey);
      state.drag.dx = dx;
      state.drag.dy = dy;
      updateZonePreview(state.drag.zone, state.drag.index, dx, dy);
    } else if (state.drag.kind === "paint") {
      const lamp = lampFromEvent(event);
      if (lamp != null) paint(lamp);
    }
  }

  function onPointerUp(event) {
    const drag = state.drag;
    state.drag = null;
    if (!drag) return;
    try { state.map.svg.releasePointerCapture(event.pointerId); } catch { /* already gone */ }
    if (drag.kind === "move" && drag.moved) {
      const dx = snap(unitPoint(event).x - drag.start.x, event.ctrlKey);
      const dy = snap(unitPoint(event).y - drag.start.y, event.ctrlKey);
      const lamps = drag.lamps;
      const target = ensureLayoutId(ctx, capability);
      ctx.replaceDraft((draft) => {
        const node = draft.layouts[target];
        for (const key of node.keys) {
          const lamp = lampOfKey(node, key, lamps);
          if (lamp == null) continue;
          const geometry = geometryOf(key) || { type: "rect", x: 0, y: 0, w: 1, h: 1 };
          geometry.x = (geometry.x ?? 0) + dx;
          geometry.y = (geometry.y ?? 0) + dy;
          key.geometry = geometry;
        }
        for (const lamp of node.lamps || []) {
          if (!lamps.has(lamp.index)) continue;
          const geometry = geometryOf(lamp);
          if (geometry && geometry.type !== "path") {
            geometry.x = (geometry.x ?? 0) + dx;
            geometry.y = (geometry.y ?? 0) + dy;
            lamp.geometry = geometry;
          }
        }
        return draft;
      }, "move keys", "geometry-move");
      refresh();
    } else if (drag.kind === "marquee") {
      const box = {
        x: Number(state.marquee.getAttribute("x")),
        y: Number(state.marquee.getAttribute("y")),
        w: Number(state.marquee.getAttribute("width")),
        h: Number(state.marquee.getAttribute("height")),
      };
      const hits = [];
      for (const [lamp, position] of state.map.positions) {
        if (position.x >= box.x && position.x <= box.x + box.w &&
            position.y >= box.y && position.y <= box.y + box.h) {
          hits.push(lamp);
        }
      }
      state.marquee.remove();
      state.marquee = null;
      if (hits.length) {
        ctx.setSelection(event.shiftKey
          ? [...new Set([...ctx.selection, ...hits])] : hits);
        refresh();
      }
    } else if (drag.kind === "zone-point" && (drag.dx || drag.dy)) {
      const target = ensureLayoutId(ctx, capability);
      ctx.replaceDraft((draft) => {
        const zone = (draft.layouts[target].zones || [])
          .find((entry) => entry.id === drag.zone);
        if (!zone || !zone.geometry || !zone.geometry.points) return draft;
        const points = zone.geometry.points;
        points[drag.index] = [points[drag.index][0] + drag.dx,
                              points[drag.index][1] + drag.dy];
        return draft;
      }, "move zone point", "zone-geometry");
      refresh();
    }
  }

  function updateZonePreview(zoneId, index, dx, dy) {
    const layout = resolveLayout(ctx.draft, capability);
    const zone = (layout.zones || []).find((entry) => entry.id === zoneId);
    if (!zone || !zone.geometry || zone.geometry.type !== "path") return;
    const points = zone.geometry.points.map((point, i) =>
      i === index ? [point[0] + dx, point[1] + dy] : point);
    const closed = Boolean(zone.geometry.closed);
    const svg = state.map.svg;
    for (const node of svg.querySelectorAll(`[data-zone="${cssEscape(zoneId)}"]`)) {
      if (node.dataset.t != null && node.dataset.lamp != null) {
        const t = Number(node.dataset.t);
        const ordered = orderedPathLamps(layout, zone, lampsOf(layout));
        const rank = ordered.findIndex((entry) => String(entry.index) === node.dataset.lamp);
        const count = Math.max(1, ordered.length);
        const nextT = rank + 1 < ordered.length
          ? (ordered[rank + 1].t ?? (rank + 1) / count)
          : (ordered[0].t ?? 0) + 1;
        const span = (((nextT - t) % 1) + 1) % 1 || 1 / count;
        node.setAttribute("d", subPathD(points, closed, t, (t + span * 0.92) % 1));
      } else if (node.classList.contains("strip-glow") ||
                 node.classList.contains("strip")) {
        node.setAttribute("d", pathD(points, closed));
      }
    }
    const handle = state.map.svg.querySelector(
      `[data-zone-point="${index}"][data-zone="${cssEscape(zoneId)}"]`);
    if (handle) {
      const base = zone.geometry.points[index];
      handle.setAttribute("cx", base[0] + dx);
      handle.setAttribute("cy", base[1] + dy);
    }
  }

  function paint(lamp) {
    if (!capability) return;
    if (state.tool === "lane") {
      const entry = (ctx.draft.devices || {})[capability.name] || {};
      const pool = Array.isArray(entry.lane_pool) ? [...entry.lane_pool] : [];
      if (!pool.includes(lamp)) {
        ctx.update(`devices.${capability.name}.lane_pool`, [...pool, lamp],
                   "lane pool", "lane-paint");
      }
      if (state.nextLane == null) state.nextLane = pool.length;
      state.nextLane += pool.includes(lamp) ? 0 : 1;
    } else if (state.tool === "erase") {
      const overrides = (ctx.draft.lamp_overrides || [])
        .filter((override) => !(override.device === capability.name && override.lamp === lamp));
      ctx.update("lamp_overrides", overrides, "remove override", "override-paint");
    } else {
      const overrides = (ctx.draft.lamp_overrides || [])
        .filter((override) => !(override.device === capability.name && override.lamp === lamp));
      overrides.push({ device: capability.name, lamp, mode: "static",
                       color: state.paintColor });
      ctx.update("lamp_overrides", overrides, "paint lamp", "override-paint");
    }
    const look = opts.onPaintApplied;
    look?.(lamp);
    // immediate feedback without rebuilding the svg mid-gesture
    const node = state.map?.nodes.get(lamp);
    if (node) {
      if (state.tool === "override") {
        node.style.setProperty("--lamp-fill", rgbCss(hexToRgb(state.paintColor)));
      } else if (state.tool === "erase") {
        const layout = resolveLayout(ctx.draft, capability);
        const effective = effectiveLamp(ctx.draft, capability, lamp, ctx.status,
          performance.now(), { quiet: ctx.quiet(),
                               zoneOfLamp: (index) => zoneOfLamp(layout).get(index) });
        node.style.setProperty("--lamp-fill", rgbCss(effective.color));
      }
    }
  }

  function showTooltip(event, lamp) {
    if (!opts.describe) return;
    tooltip.innerHTML = "";
    const content = opts.describe(lamp);
    if (!content) { tooltip.hidden = true; return; }
    tooltip.appendChild(content);
    tooltip.hidden = false;
    const bounds = viewport.getBoundingClientRect();
    tooltip.style.left = `${Math.min(bounds.width - 220, event.clientX - bounds.left + 12)}px`;
    tooltip.style.top = `${Math.max(8, event.clientY - bounds.top - 12)}px`;
  }

  return {
    el,
    refresh,
    get selection() { return [...ctx.selection]; },
    get positions() { return state.map ? state.map.positions : new Map(); },
    setTool(tool) { state.tool = tool; refresh(); },
    get tool() { return state.tool; },
    get paintColor() { return state.paintColor; },
    setPaintColor(colour) { state.paintColor = colour; },
    destroy() {
      clear(el);
    },
  };
}

function snap(value, disabled) {
  if (disabled) return Math.round(value * 100) / 100;
  return Math.round(value / SNAP) * SNAP;
}

function lampOfKey(layout, key, lamps) {
  for (const lamp of layout.lamps || []) {
    if (lamp.key === key.id && lamps.has(lamp.index)) return lamp.index;
  }
  if (typeof key.lamp === "number" && lamps.has(key.lamp)) return key.lamp;
  return null;
}

function ensureLayoutId(ctx, capability) {
  const entry = (ctx.draft.devices || {})[capability.name] || {};
  if (entry.layout && ctx.draft.layouts && ctx.draft.layouts[entry.layout]) {
    return entry.layout;
  }
  const id = `${capability.name}-default`;
  ctx.replaceDraft((draft) => {
    draft.layouts = draft.layouts || {};
    if (!draft.layouts[id]) {
      draft.layouts[id] = { format_version: 2, source: "hand",
                            keys: [], lamps: [], zones: [] };
    }
    draft.devices = draft.devices || {};
    draft.devices[capability.name] = { ...(draft.devices[capability.name] || {}),
                                       layout: id };
    return draft;
  }, "create layout");
  return id;
}

function loadZoneVisibility(deviceName) {
  try {
    const raw = localStorage.getItem(`rgi.zones.${deviceName}`);
    return new Map(raw ? JSON.parse(raw) : []);
  } catch {
    return new Map();
  }
}

function saveZoneVisibility(deviceName, map) {
  try {
    localStorage.setItem(`rgi.zones.${deviceName}`,
      JSON.stringify([...map.entries()]));
  } catch { /* private mode */ }
}

function cssEscape(value) {
  return String(value).replace(/["\\]/g, "\\$&");
}
