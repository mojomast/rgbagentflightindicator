// Paint: put colour and lanes on the map. Static overrides (lamp and zone) and
// the lane pool are first-class here; the state palette previews real colours.

import { h, clear } from "../lib/dom.js";
import { button, field, selectInput, toast } from "../components/ui.js";
import { createMapPane } from "../components/mappane.js";
import { resolveLayout, keysByLamp } from "../lib/layout.js";
import { effectiveLamp } from "../lib/effective.js";
import { lampsOf, zoneOfLamp } from "../lib/topology.js";

export function render(ctx) {
  const capability = ctx.device;
  const root = h("div", { class: "stack" });
  if (!capability) {
    root.appendChild(h("div", { class: "empty" }, "No device detected."));
    return root;
  }
  root.appendChild(h("div", { class: "view-head" },
    h("h1", {}, "Paint"),
    h("p", { class: "muted" },
      "Pick a tool, then click lamps. Override paints one colour that ignores lane state; " +
      "Lane appends lamps to the lane pool in click order; Erase removes overrides.")));

  const palette = h("div", { class: "row palette-row" });
  const paneHost = h("div", { class: "map-host" });
  const side = h("div", { class: "paint-side" });
  const overridesCard = h("div", { class: "card" });
  const zonesCard = h("div", { class: "card" });
  const poolCard = h("div", { class: "card" });
  side.append(overridesCard, zonesCard, poolCard);
  root.append(palette, paneHost, side);

  let pane;
  const refreshAll = () => {
    renderPalette(ctx, palette, pane);
    renderOverrides(ctx, capability, overridesCard, () => { pane.refresh(); });
    renderZones(ctx, capability, zonesCard, pane);
    renderPool(ctx, capability, poolCard, pane);
  };
  pane = createMapPane(ctx, {
    mode: "paint",
    capability,
    describe: (lamp) => describeLamp(ctx, capability, lamp),
    hud: () => [
      button("Clear selection", () => { ctx.setSelection([]); ctx.refresh(); },
        { variant: "ghost", sm: true }),
    ],
    onPaintApplied: () => { renderOverrides(ctx, capability, overridesCard, () => pane.refresh());
                            renderPool(ctx, capability, poolCard, pane); },
  });
  paneHost.appendChild(pane.el);
  refreshAll();
  return root;
}

function renderPalette(ctx, host, pane) {
  clear(host);
  host.appendChild(h("span", { class: "muted small" }, "Palette:"));
  for (const [name, spec] of Object.entries(ctx.draft.appearance?.states || {})) {
    if (!spec.color || spec.pattern === "off") continue;
    host.appendChild(h("button", {
      class: "swatch-button", type: "button",
      title: `Paint with the ${name} colour (${spec.color})`,
      onclick: () => { pane.setTool("override"); pane.setPaintColor(spec.color);
                       pane.refresh(); },
    }, h("span", { class: "dot", style: { background: spec.color } }), name));
  }
  const custom = h("input", {
    type: "color", value: pane.paintColor, "aria-label": "Custom paint colour",
    oninput: (event) => { pane.setTool("override"); pane.setPaintColor(event.target.value); },
  });
  host.appendChild(custom);
}

function renderOverrides(ctx, capability, host, onChange) {
  clear(host);
  const overrides = (ctx.draft.lamp_overrides || [])
    .filter((override) => override.device === capability.name);
  host.appendChild(h("h3", {}, `Lamp overrides (${overrides.length})`));
  if (!overrides.length) {
    host.appendChild(h("p", { class: "muted" }, "None. Use Override and click a lamp."));
    return;
  }
  for (const override of overrides) {
    host.appendChild(h("div", { class: "row override-row" },
      h("span", { class: "mono" }, `lamp ${override.lamp}`),
      h("span", { class: "swatch", style: { background: override.color } }),
      h("span", { class: "muted small" }, override.color),
      button("✕", () => {
        ctx.update("lamp_overrides",
          (ctx.draft.lamp_overrides || []).filter((entry) => entry !== override),
          "remove override");
        onChange();
      }, { variant: "ghost", sm: true, title: `Remove override for lamp ${override.lamp}` })));
  }
  host.appendChild(button("Clear all overrides", () => {
    ctx.update("lamp_overrides",
      (ctx.draft.lamp_overrides || []).filter((entry) => entry.device !== capability.name),
      "clear overrides");
    onChange();
  }, { variant: "danger", sm: true }));
}

function renderZones(ctx, capability, host, pane) {
  clear(host);
  const layout = resolveLayout(ctx.draft, capability);
  const zones = layout.zones || [];
  const overrides = (ctx.draft.zone_overrides || [])
    .filter((override) => override.device === capability.name);
  host.appendChild(h("h3", {}, "Zone overrides"));
  if (!zones.length) {
    host.appendChild(h("p", { class: "muted" },
      "No zones yet. Add a base/perimeter zone in Layout."));
    return;
  }
  for (const override of overrides) {
    const zone = zones.find((entry) => entry.id === override.zone);
    host.appendChild(h("div", { class: "row override-row" },
      h("span", {}, zone ? (zone.label || zone.id) : override.zone),
      h("span", { class: "swatch", style: { background: override.color } }),
      button("✕", () => {
        ctx.update("zone_overrides",
          (ctx.draft.zone_overrides || []).filter((entry) => entry !== override),
          "remove zone override");
        pane.refresh();
        renderZones(ctx, capability, host, pane);
      }, { variant: "ghost", sm: true })));
  }
  const select = selectInput(zones.map((zone) => ({ value: zone.id,
    label: `${zone.label || zone.id} (${(zone.lamps || []).length})` })), zones[0].id, () => {});
  const colour = h("input", { type: "color", value: "#00aaff",
                              "aria-label": "Zone colour" });
  host.appendChild(h("div", { class: "row" }, select, colour,
    button("Paint zone", () => {
      const next = (ctx.draft.zone_overrides || [])
        .filter((entry) => !(entry.device === capability.name && entry.zone === select.value));
      next.push({ device: capability.name, zone: select.value, mode: "static",
                  color: colour.value });
      ctx.update("zone_overrides", next, "zone override");
      pane.refresh();
      renderZones(ctx, capability, host, pane);
    })));
  const selectedZones = new Set(ctx.selection.map((lamp) => zoneOfLamp(layout).get(lamp)));
  if (selectedZones.size === 1 && !selectedZones.has(undefined)) {
    host.appendChild(button("Select every lamp in this zone", () => {
      const zone = zones.find((entry) => entry.id === [...selectedZones][0]);
      if (!zone) return;
      ctx.setSelection(zone.lamps || []);
      pane.refresh();
    }, { variant: "ghost", sm: true }));
  }
}

function renderPool(ctx, capability, host, pane) {
  clear(host);
  const layout = resolveLayout(ctx.draft, capability);
  const byLamp = keysByLamp(layout);
  const entry = ctx.draft.devices?.[capability.name] || {};
  const pool = entry.lane_pool || [];
  host.appendChild(h("h3", {}, `Lane pool (${pool.length})`));
  host.appendChild(h("p", { class: "muted small" },
    "Lane 0 is the first entry. Paint tool “Lane” appends in click order."));
  for (let index = 0; index < pool.length; index += 1) {
    const lamp = pool[index];
    const key = byLamp.get(lamp);
    host.appendChild(h("div", { class: "row pool-row" },
      h("span", { class: "mono lane-index" }, String(index)),
      h("span", { class: "mono" }, String(lamp)),
      h("span", {}, key ? (key.label || key.code || key.id) : "—"),
      button("↑", () => movePool(ctx, capability, index, -1, pane),
        { variant: "ghost", sm: true, disabled: index === 0, title: "Move earlier" }),
      button("↓", () => movePool(ctx, capability, index, 1, pane),
        { variant: "ghost", sm: true, disabled: index === pool.length - 1,
          title: "Move later" }),
      button("✕", () => {
        ctx.update(`devices.${capability.name}.lane_pool`,
          pool.filter((value) => value !== lamp), "lane pool");
        renderPool(ctx, capability, host, pane);
        pane.refresh();
      }, { variant: "ghost", sm: true, title: "Remove" })));
  }
  host.appendChild(h("div", { class: "row" },
    button("Set pool from selection in reading order", () => {
      if (!ctx.selection.length) return toast("Select lamps first");
      const positions = pane.positions || new Map();
      const ordered = [...ctx.selection].sort((a, b) => {
        const pa = positions.get(a) || { x: 0, y: 0 };
        const pb = positions.get(b) || { x: 0, y: 0 };
        return (pa.y - pb.y) || (pa.x - pb.x);
      });
      ctx.update(`devices.${capability.name}.lane_pool`, ordered, "lane pool");
      toast("Pool replaced from the selection");
      renderPool(ctx, capability, host, pane);
      pane.refresh();
    })));
}

function movePool(ctx, capability, index, delta, pane) {
  const pool = [...((ctx.draft.devices?.[capability.name] || {}).lane_pool || [])];
  const target = index + delta;
  if (target < 0 || target >= pool.length) return;
  [pool[index], pool[target]] = [pool[target], pool[index]];
  ctx.update(`devices.${capability.name}.lane_pool`, pool, "lane pool");
  const host = document.querySelector(".paint-side .card:last-child");
  if (host) renderPool(ctx, capability, host, pane);
  pane.refresh();
}

function describeLamp(ctx, capability, lampIndex) {
  const layout = resolveLayout(ctx.draft, capability);
  const record = lampsOf(layout).find((lamp) => lamp.index === lampIndex);
  const look = effectiveLamp(ctx.draft, capability, lampIndex, ctx.status, Date.now(), {
    quiet: ctx.quiet(), zoneOfLamp: (index) => zoneOfLamp(layout).get(index) });
  const inPool = ((ctx.draft.devices?.[capability.name] || {}).lane_pool || [])
    .indexOf(lampIndex);
  return h("div", { class: "stack tiny" },
    h("strong", {}, `Lamp ${lampIndex} · ${record?.kind || "unknown"}`),
    h("span", {}, `effective: ${look.source}`),
    h("span", {}, inPool >= 0 ? `lane ${inPool}` : "not a lane"),
    h("span", {}, `zone: ${zoneOfLamp(layout).get(lampIndex) || "—"}`));
}
