// The lamp table: the map's accessible and power-user equivalent. Every lamp
// is a row with its label, kind, zone, lane and effective state; rows select
// and flash. Filters cover the questions the map is bad at answering in bulk.

import { h, clear } from "../lib/dom.js";
import { button, selectInput, toast } from "./ui.js";
import { effectiveLamp, rgbCss, sessionForLamp, formatDuration } from "../lib/effective.js";
import { resolveLayout, keysByLamp } from "../lib/layout.js";
import { lampsOf, zoneOfLamp } from "../lib/topology.js";

export function renderLampTable(ctx, capability, host, { onChanged } = {}) {
  const layout = resolveLayout(ctx.draft, capability);
  const byLamp = keysByLamp(layout);
  const zones = zoneOfLamp(layout);
  const pool = (ctx.draft.devices?.[capability.name]?.lane_pool) || [];
  const overridden = new Set((ctx.draft.lamp_overrides || [])
    .filter((override) => override.device === capability.name)
    .map((override) => override.lamp));
  const lamps = lampsOf(layout);

  const filter = h("select", { class: "select", style: { width: "auto" },
                                "aria-label": "Filter lamps" },
    h("option", { value: "all" }, `All lamps (${lamps.length})`),
    h("option", { value: "unmapped" }, "No key"),
    h("option", { value: "overridden" }, "Overridden"),
    h("option", { value: "pool" }, `Lane pool (${pool.length})`),
    h("option", { value: "unknown" }, "Kind unknown"));
  const search = h("input", { class: "input", type: "search", placeholder: "Filter label…",
                              style: { maxWidth: "220px" } });
  const body = h("tbody");

  function visibleLamps() {
    const needle = search.value.trim().toLowerCase();
    return lamps.filter((lamp) => {
      const key = byLamp.get(lamp.index);
      const label = (key && (key.label || key.code)) || lamp.label || "";
      if (filter.value === "unmapped" && key) return false;
      if (filter.value === "overridden" && !overridden.has(lamp.index)) return false;
      if (filter.value === "pool" && !pool.includes(lamp.index)) return false;
      if (filter.value === "unknown" && lamp.kind !== "unknown") return false;
      if (needle && !`${lamp.index} ${label} ${key?.code || ""}`.toLowerCase().includes(needle)) {
        return false;
      }
      return true;
    });
  }

  function renderRows() {
    clear(body);
    const list = visibleLamps();
    if (!list.length) {
      body.appendChild(h("tr", null,
        h("td", { colspan: "7", class: "muted" }, "No lamps match this filter.")));
      return;
    }
    for (const lamp of list) {
      const key = byLamp.get(lamp.index);
      const look = effectiveLamp(ctx.draft, capability, lamp.index, ctx.status,
        Date.now(), { quiet: ctx.quiet(),
                      zoneOfLamp: (index) => zones.get(index) });
      const session = sessionForLamp(ctx.status, capability, lamp.index);
      const lane = pool.indexOf(lamp.index);
      body.appendChild(h("tr", {
        class: ctx.selection.includes(lamp.index) ? "selected" : "",
      },
        h("td", { class: "mono" }, lamp.index),
        h("td", null, (key && (key.label || key.code)) || lamp.label || "—",
          key?.code ? h("span", { class: "muted small" }, ` · ${key.code}`) : null,
          lamp.verified !== true && lamp.source && lamp.source !== "press"
            ? h("span", { class: "badge warn" }, lamp.source) : null),
        h("td", null, lamp.kind || "unknown"),
        h("td", { class: "muted" }, zones.get(lamp.index) || "—"),
        h("td", { class: "mono" }, lane >= 0 ? String(lane) : "—"),
        h("td", null, h("span", { class: "swatch",
                                  style: { background: rgbCss(look.color) } }),
          " ", look.state || look.source,
          session?.changedMs
            ? h("span", { class: "muted small" },
                ` ${formatDuration((Date.now() - session.changedMs) / 1000)}`)
            : null),
        h("td", null,
          button("Flash", () => ctx.hardwareTest({ device: capability.name,
            lamps: [{ index: lamp.index, rgb: [255, 255, 255] }], duration_ms: 4000 }),
            { variant: "ghost", sm: true }),
          button("Select", () => {
            ctx.setSelection([lamp.index]);
            onChanged?.();
            renderRows();
          }, { variant: "ghost", sm: true }))));
    }
  }
  filter.addEventListener("change", renderRows);
  search.addEventListener("input", renderRows);

  host.appendChild(h("h3", {}, "Lamp table"));
  host.appendChild(h("p", { class: "muted small" },
    "The equivalent of the map for bulk reading and screen readers. " +
    "Click a row's Select to edit it in the inspector."));
  host.appendChild(h("div", { class: "row" }, filter, search));
  const table = h("table", { class: "table" },
    h("thead", null, h("tr", null,
      h("th", { scope: "col" }, "#"), h("th", { scope: "col" }, "Key"),
      h("th", { scope: "col" }, "Kind"), h("th", { scope: "col" }, "Zone"),
      h("th", { scope: "col" }, "Lane"), h("th", { scope: "col" }, "Now"),
      h("th", { scope: "col" }, ""))));
  table.appendChild(body);
  host.appendChild(table);
  renderRows();
  void toast;
}
