// Devices: per-backend settings, lane pools, layouts, and configured endpoints.

import { h, clear } from "../lib/dom.js";
import { button, checkbox, field, selectInput, textInput, banner, table, toast } from "../components/ui.js";
import { autoLayout, defaultLanePool, layoutFor } from "../lib/layout.js";
import { setPath } from "../lib/commands.js";

export function render(ctx) {
  const { draft, capabilities } = ctx;
  const root = h("div", { class: "stack" });
  root.appendChild(h("h1", {}, "Devices"));
  root.appendChild(h("p", { class: "muted" },
    "Backends that are open right now. Pools and labels apply live; enabled and endpoint changes need a restart."));

  if (!capabilities.length) {
    root.appendChild(h("div", { class: "empty" }, "No devices detected. Run ", h("code", {}, "rgi detect"), "."));
    return root;
  }

  for (const cap of capabilities) {
    const entry = draft.devices?.[cap.name] || {};
    const card = h("div", { class: "card" });
    card.appendChild(h("h2", { style: { marginTop: "0" } }, `${cap.label} — ${cap.name}`));
    const bits = h("div", { class: "row small muted" },
      h("span", {}, `${cap.lamps.length} lamps`),
      h("span", {}, cap.per_lamp ? "per-key colour" : "single colour only"),
      h("span", {}, `min interval ${Math.round(cap.min_interval * 1000)} ms`));
    card.appendChild(bits);

    card.appendChild(field("Display label",
      textInput(entry.label || cap.label,
        (value) => ctx.update(`devices.${cap.name}.label`, value || cap.label, "device label", `label-${cap.name}`))));

    card.appendChild(checkbox("Enabled (restart required)", entry.enabled !== false,
      (checked) => ctx.update(`devices.${cap.name}.enabled`, checked, "device enabled")));

    const layoutIds = Object.keys(draft.layouts || {});
    const options = [{ value: "", label: "auto (from lamp order)" },
      ...layoutIds.map((id) => ({ value: id, label: `layout: ${id}` }))];
    card.appendChild(field("Layout",
      selectInput(options, entry.layout || "", (value) => {
        ctx.update(`devices.${cap.name}.layout`, value || null, "device layout");
        ctx.refresh();
      }),
      { hint: "controls the diagram, labels, and the default lane pool" }));

    // -- lane pool ------------------------------------------------------
    const layout = layoutFor(draft, cap);
    const pool = Array.isArray(entry.lane_pool) && entry.lane_pool.length
      ? entry.lane_pool : defaultLanePool(layout, ctx.count());
    const byLamp = new Map(layout.keys.map((key) => [key.lamp, key]));
    const poolRow = h("div", { class: "row" });
    pool.forEach((lamp, index) => {
      const key = byLamp.get(lamp);
      poolRow.appendChild(h("span", { class: "chip" },
        h("span", { class: "mono" }, String(lamp)),
        h("span", {}, key ? key.label : ""),
        button("↑", () => move(ctx, cap, pool, index, -1), { variant: "ghost" }),
        button("↓", () => move(ctx, cap, pool, index, 1), { variant: "ghost" }),
        button("✕", () => {
          ctx.update(`devices.${cap.name}.lane_pool`, pool.filter((p) => p !== lamp), "lane pool");
          ctx.refresh();
        }, { variant: "ghost" })));
    });
    card.appendChild(field("Lane pool", poolRow, {
      hint: "lane 0 gets the first lamp; the board's own keys still show what it can address",
    }));
    const unassigned = layout.keys.filter((key) => !pool.includes(key.lamp));
    const addSelect = selectInput(
      unassigned.slice(0, 200).map((key) => ({ value: String(key.lamp), label: `${key.label} (lamp ${key.lamp})` })),
      "", () => {});
    card.appendChild(h("div", { class: "row" },
      addSelect,
      button("Add lamp", () => {
        if (!addSelect.value) return;
        ctx.update(`devices.${cap.name}.lane_pool`, [...pool, Number(addSelect.value)], "lane pool");
        ctx.refresh();
      }),
      button("Reset to number row first", () => {
        ctx.update(`devices.${cap.name}.lane_pool`, defaultLanePool(layout, ctx.count()), "lane pool");
        ctx.refresh();
      }, { variant: "ghost" })));

    // -- layout management ----------------------------------------------
    const layoutId = entry.layout || "";
    card.appendChild(h("div", { class: "card-actions" },
      button(layoutId ? "Edit layout" : "Create layout from lamps", () => {
        if (!layoutId) {
          const id = `${cap.name}-default`;
          ctx.replaceDraft((d) => {
            d.layouts[id] = autoLayout(cap.lamps);
            setPath(d, `devices.${cap.name}.layout`, id);
            return d;
          }, "create layout");
        }
        ctx.go(`#/layout?device=${encodeURIComponent(cap.name)}`);
      }, { variant: "primary" })));
    root.appendChild(card);
  }

  // -- endpoints ---------------------------------------------------------
  root.appendChild(h("h2", {}, "Endpoints"));
  root.appendChild(h("p", { class: "muted" },
    "Connection details for network backends (OpenRGB, WLED). Stored here; the daemon reads them at startup."));
  const endpoints = draft.endpoints || [];
  const rows = endpoints.map((endpoint, index) => h("tr", null,
    h("td", null, textInput(endpoint.id || "", (value) => {
      ctx.update(`endpoints.${index}.id`, value, "endpoint id");
    })),
    h("td", null, selectInput(["openrgb", "wled", "sysfs", "evmux"], endpoint.kind || "openrgb",
      (value) => ctx.update(`endpoints.${index}.kind`, value, "endpoint kind"))),
    h("td", null, textInput(endpoint.url || "", (value) => {
      ctx.update(`endpoints.${index}.url`, value, "endpoint url");
    }, { mono: true })),
    h("td", null, checkbox("", endpoint.enabled !== false,
      (checked) => ctx.update(`endpoints.${index}.enabled`, checked, "endpoint enabled"))),
    h("td", null, button("Remove", () => {
      ctx.update("endpoints", endpoints.filter((_, i) => i !== index), "remove endpoint");
      ctx.refresh();
    }, { variant: "ghost" }))));
  root.appendChild(rows.length
    ? table(["Id", "Kind", "URL", "On", ""], rows)
    : h("div", { class: "empty" }, "No endpoints configured. Add one only if a network backend needs it."));
  root.appendChild(h("div", { class: "toolbar" },
    button("Add endpoint", () => {
      ctx.update("endpoints", [...endpoints, { id: `endpoint-${endpoints.length + 1}`, kind: "openrgb", url: "127.0.0.1:6742", enabled: true }], "add endpoint");
      ctx.refresh();
    })));

  if ((ctx.restart || []).length) {
    root.appendChild(banner("warn", `Restart required for: ${ctx.restart.join(", ")}`));
  }
  return root;
}

function move(ctx, cap, pool, index, delta) {
  const target = index + delta;
  if (target < 0 || target >= pool.length) return;
  const next = [...pool];
  [next[index], next[target]] = [next[target], next[index]];
  ctx.update(`devices.${cap.name}.lane_pool`, next, "lane pool");
  ctx.refresh();
}
