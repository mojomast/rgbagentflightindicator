// Layout mode: the keyboard is the editor. Drag, nudge, resize, rename, set
// kinds and zones; every gesture is one undo step and lands in the draft.

import { h, clear } from "../lib/dom.js";
import {
  banner, button, captureKey, confirmDialog, field, numberInput, selectInput,
  textInput, toast,
} from "../components/ui.js";
import { createMapPane } from "../components/mappane.js";
import { renderLampTable } from "../components/lamptable.js";
import { autoLayout, codeLabel, download, resolveLayout, toCsv } from "../lib/layout.js";
import { migrateLayout } from "../lib/topology.js";
import { caseOf, geometryOf, keysById, lampsOf, zoneOfLamp } from "../lib/topology.js";

const KINDS = ["key", "perimeter", "logo", "indicator", "accent", "unknown"];

export function render(ctx) {
  const capability = ctx.device;
  const root = h("div", { class: "stack" });
  if (!capability) {
    root.appendChild(h("div", { class: "empty" }, "No device detected."));
    return root;
  }

  root.appendChild(h("div", { class: "view-head" },
    h("h1", {}, "Layout"),
    h("p", { class: "muted" },
      "Drag to move (0.25u snap, Ctrl to disable), arrow keys to nudge, click a strip to edit its zone. " +
      "Everything here is a draft until Apply.")));

  renderProfileBanner(ctx, capability, root);

  const toolbar = h("div", { class: "toolbar" });
  root.appendChild(toolbar);
  const paneHost = h("div", { class: "map-host" });
  const lower = h("div", { class: "layout-lower" });
  const inspectorHost = h("div", { class: "card inspector" });
  const trayHost = h("div", { class: "card tray" });
  lower.append(inspectorHost, trayHost);
  const tableBody = h("div");
  const tableHost = h("details", { class: "card" },
    h("summary", {}, "Lamp table — the accessible, bulk view"),
    tableBody);
  root.append(paneHost, lower, tableHost);

  const pane = createMapPane(ctx, {
    mode: "layout",
    capability,
    describe: (lamp) => describeLamp(ctx, capability, lamp),
    hud: (count) => hudActions(ctx, capability, count),
    onSelectionChange: () => {
      renderInspector(ctx, capability, pane, inspectorHost);
      renderLampTable(ctx, capability, tableBody, {
        onChanged: () => renderInspector(ctx, capability, pane, inspectorHost),
      });
    },
  });
  paneHost.appendChild(pane.el);

  renderToolbar(ctx, capability, toolbar, pane);
  renderInspector(ctx, capability, pane, inspectorHost);
  renderTray(ctx, capability, pane, trayHost);
  renderLampTable(ctx, capability, tableBody, {
    onChanged: () => renderInspector(ctx, capability, pane, inspectorHost),
  });
  return root;
}

function renderProfileBanner(ctx, capability, root) {
  const suggestion = capability.profile;
  if (!suggestion || suggestion.active) return;
  const card = banner("info",
    `Built-in profile available: ${suggestion.label || suggestion.id}` +
    `${suggestion.verified === false ? " (unverified template)" : ""}. ` +
    "It ships key geometry only — no lamp is claimed.",
    []);
  card.querySelector("div").appendChild(h("div", { class: "row", style: { marginTop: "6px" } },
    button("Use this template", () => ctx.useProfile(suggestion.id, capability), { variant: "primary" }),
    button("Keep auto grid", () => {
      ctx.replaceDraft((draft) => {
        draft.profiles = draft.profiles || {};
        draft.profiles[suggestion.id] = { enabled: false };
        return draft;
      }, "ignore profile");
      card.remove();
    }, { variant: "ghost" })));
  root.appendChild(card);
}

function renderToolbar(ctx, capability, host, pane) {
  clear(host);
  host.append(
    button("Use auto grid", async () => {
      if (!await confirmDialog("Replace the geometry with an auto grid?",
          "The current key positions and sizes are replaced. Undo can bring them back.")) return;
      const entry = (ctx.draft.devices || {})[capability.name] || {};
      const id = entry.layout || `${capability.name}-default`;
      ctx.replaceDraft((draft) => {
        draft.layouts[id] = autoLayout(capability.lamps);
        draft.devices[capability.name] = { ...(entry), layout: id };
        return draft;
      }, "auto grid");
      toast("Auto grid applied to the draft");
      ctx.refresh();
    }),
    button("Add missing lamps", () => {
      const layout = resolveLayout(ctx.draft, capability);
      const mapped = new Set(lampsOf(layout).map((lamp) => lamp.index));
      const missing = capability.lamps.filter((lamp) => !mapped.has(lamp.index));
      if (!missing.length) return toast("Every lamp already has a place");
      const id = ensureLayout(ctx, capability);
      ctx.replaceDraft((draft) => {
        const node = draft.layouts[id];
        let row = node.keys.reduce((max, key) =>
          Math.max(max, (geometryOf(key)?.y ?? 0) + 1), 0);
        for (const lamp of missing) {
          const keyId = uniqueId(node.keys, `k${lamp.index}`);
          node.keys.push({ id: keyId, code: null, label: lamp.label || String(lamp.index),
                           group: lamp.group || "unmapped",
                           geometry: { type: "rect", x: 0, y: row, w: 1, h: 1 } });
          node.lamps.push({ index: lamp.index, kind: "key", key: keyId, source: "hand" });
          row += 1;
        }
        return draft;
      }, "add missing lamps");
      toast(`Placed ${missing.length} lamps in the tray column — drag them into shape`);
      ctx.refresh();
    }, { variant: "ghost" }),
    button("Add base/perimeter zone", () => {
      const id = ensureLayout(ctx, capability);
      const layout = resolveLayout(ctx.draft, capability);
      const box = caseOf(layout) || { x: -0.5, y: -0.5, w: 10, h: 4 };
      const zoneId = uniqueZoneId(layout, "perimeter");
      ctx.replaceDraft((draft) => {
        const node = draft.layouts[id];
        node.zones.push({
          id: zoneId, label: "Base / underglow", kind: "perimeter",
          geometry: { type: "path", closed: true, width: 0.22,
                      points: [[box.x, box.y], [box.x + box.w, box.y],
                               [box.x + box.w, box.y + box.h], [box.x, box.y + box.h]] },
          lamps: [...ctx.selection],
          ordering: "cw", source: "hand",
          assumptions: ["path is a case-sized rectangle until edited"],
        });
        for (const lamp of node.lamps) {
          if (ctx.selection.includes(lamp.index)) {
            lamp.zone = zoneId;
            lamp.kind = "perimeter";
          }
        }
        return draft;
      }, "add perimeter zone");
      toast(`Zone ${zoneId} added — drag its corner handles to fit the case`);
      ctx.refresh();
    }, { variant: "ghost" }),
    button("Export JSON", () => {
      const layout = resolveLayout(ctx.draft, capability);
      download(`${capability.name}-layout.json`, JSON.stringify(layout, null, 2));
    }),
    button("Export CSV", () => {
      const layout = resolveLayout(ctx.draft, capability);
      download(`${capability.name}-lamps.csv`, toCsv(lampsOf(layout)), "text/csv");
    }),
    importButton(ctx, capability),
    button("Fit map", () => pane.refresh(), { variant: "ghost" }));
}

function importButton(ctx, capability) {
  const input = h("input", {
    type: "file", accept: ".json,application/json", style: { display: "none" },
    onchange: async (event) => {
      const file = event.target.files?.[0];
      if (!file) return;
      try {
        const parsed = JSON.parse(await file.text());
        const layout = migrateLayout(parsed);
        if (!layout || !Array.isArray(layout.keys)) throw new Error("no keys array");
        const id = ensureLayout(ctx, capability);
        ctx.replaceDraft((draft) => { draft.layouts[id] = layout; return draft; },
                         "import layout");
        toast(`Imported ${layout.keys.length} keys`);
        ctx.refresh();
      } catch (err) {
        toast(`Import failed: ${err.message}`, "bad");
      } finally {
        event.target.value = "";
      }
    },
  });
  return h("span", null, input, button("Import JSON", () => input.click(), { variant: "ghost" }));
}

function renderInspector(ctx, capability, pane, host) {
  clear(host);
  const selection = ctx.selection;
  host.appendChild(h("h3", {}, selection.length
    ? `${selection.length} lamp${selection.length === 1 ? "" : "s"} selected`
    : "Inspector"));
  if (!selection.length) {
    host.appendChild(h("p", { class: "muted" },
      "Click a key on the map. Shift-click extends; drag a box on the case to marquee."));
    return;
  }
  const layout = resolveLayout(ctx.draft, capability);
  const lamp = selection[0];
  const record = lampsOf(layout).find((entry) => entry.index === lamp);
  const keys = keysById(layout);
  const key = record?.key ? keys.get(record.key) : null;

  const patchLamp = (fields, label = "lamp") => {
    const id = ensureLayout(ctx, capability);
    ctx.replaceDraft((draft) => {
      const node = draft.layouts[id];
      let target = (node.lamps || []).find((entry) => entry.index === lamp);
      if (!target) { target = { index: lamp }; node.lamps.push(target); }
      Object.assign(target, fields);
      return draft;
    }, label, `lamp-${lamp}`);
  };
  const patchKey = (fields, label = "key") => {
    if (!key) return;
    const id = ensureLayout(ctx, capability);
    ctx.replaceDraft((draft) => {
      const target = draft.layouts[id].keys.find((entry) => entry.id === key.id);
      Object.assign(target, fields);
      return draft;
    }, label, `key-${key.id}`);
  };

  if (key) {
    host.appendChild(field("Label", textInput(key.label || "", (value) => {
      patchKey({ label: value }, "key label");
    }, { ariaLabel: "Key label" })));
    host.appendChild(field("Physical key", h("div", { class: "row" },
      h("span", { class: "mono" }, key.code || "not identified"),
      button("Press it…", () => captureKey((code) => {
        patchKey({ code, label: key.label && key.label !== `lamp ${lamp}` ? key.label
          : codeLabel(code) }, "key code");
        toast(`Captured ${code}`);
        ctx.refresh();
      }))), { hint: "press the key the lamp sits under; it names itself" }));
    const geometry = geometryOf(key) || { type: "rect", x: 0, y: 0, w: 1, h: 1 };
    host.appendChild(h("div", { class: "row" },
      field("x", numberInput(geometry.x ?? 0, (value) => {
        patchKey({ geometry: { ...geometry, x: value } }, "move key");
      }, { step: 0.25 })),
      field("y", numberInput(geometry.y ?? 0, (value) => {
        patchKey({ geometry: { ...geometry, y: value } }, "move key");
      }, { step: 0.25 })),
      field("w", numberInput(geometry.w ?? 1, (value) => {
        patchKey({ geometry: { ...geometry, w: Math.max(0.25, value) } }, "size key");
      }, { step: 0.25, min: 0.25 })),
      field("h", numberInput(geometry.h ?? 1, (value) => {
        patchKey({ geometry: { ...geometry, h: Math.max(0.25, value) } }, "size key");
      }, { step: 0.25, min: 0.25 }))));
  } else {
    host.appendChild(h("p", { class: "muted" },
      "This lamp has no key yet. Assign it a kind, or press it to create one."));
    host.appendChild(button("Create a key here", () => {
      const id = ensureLayout(ctx, capability);
      const zone = zoneOfLamp(layout).get(lamp);
      ctx.replaceDraft((draft) => {
        const node = draft.layouts[id];
        const keyId = uniqueId(node.keys, `k${lamp}`);
        node.keys.push({ id: keyId, code: null, label: `lamp ${lamp}`,
                         group: "unmapped",
                         geometry: { type: "rect", x: 0, y: 0, w: 1, h: 1 } });
        let target = (node.lamps || []).find((entry) => entry.index === lamp);
        if (!target) { target = { index: lamp }; node.lamps.push(target); }
        target.kind = "key";
        target.key = keyId;
        if (zone) delete target.zone;
        return draft;
      }, "create key");
      ctx.refresh();
    }, { variant: "primary" }));
  }

  host.appendChild(field("Kind", selectInput(KINDS, record?.kind || "unknown", (value) => {
    patchLamp({ kind: value }, "lamp kind");
    ctx.refresh();
  })));

  const zones = (layout.zones || []).map((zone) => ({ value: zone.id, label: zone.label || zone.id }));
  host.appendChild(field("Zone", selectInput([{ value: "", label: "— none —" }, ...zones],
    record?.zone || "", (value) => {
      patchLamp({ zone: value || undefined }, "lamp zone");
      ctx.refresh();
    })));

  const entry = (ctx.draft.devices || {})[capability.name] || {};
  const inPool = (entry.lane_pool || []).includes(lamp);
  host.appendChild(h("div", { class: "row" },
    button(inPool ? "Remove from lane pool" : "Add to lane pool", () => {
      const pool = [...(entry.lane_pool || [])];
      const next = inPool ? pool.filter((value) => value !== lamp) : [...pool, lamp];
      ctx.update(`devices.${capability.name}.lane_pool`, next, "lane pool");
      toast(inPool ? "Removed from the lane pool" : "Added to the lane pool");
    }),
    button("Delete key", () => {
      if (!key) return;
      const id = ensureLayout(ctx, capability);
      ctx.replaceDraft((draft) => {
        const node = draft.layouts[id];
        node.keys = node.keys.filter((entry) => entry.id !== key.id);
        const record = (node.lamps || []).find((entry) => entry.index === lamp);
        if (record) delete record.key;   // the lamp stays, the key goes
        return draft;
      }, "delete key");
      toast("Lamp returned to the tray");
      ctx.refresh();
    }, { variant: "danger" })));

  if (selection.length > 1) {
    host.appendChild(button("Assign all selected as lanes", () => {
      const pool = [...new Set([...(entry.lane_pool || []), ...selection])];
      ctx.update(`devices.${capability.name}.lane_pool`, pool, "lane pool");
      toast(`${selection.length} lamps added to the lane pool`);
    }, { variant: "primary" }));
  }
}

function renderTray(ctx, capability, pane, host) {
  clear(host);
  const layout = resolveLayout(ctx.draft, capability);
  const keys = keysById(layout);
  const unplaced = lampsOf(layout).filter((lamp) =>
    (!lamp.key || !keys.has(lamp.key)) && lamp.kind !== "perimeter");
  host.appendChild(h("h3", {}, `Unplaced lamps (${unplaced.length})`));
  if (!unplaced.length) {
    host.appendChild(h("p", { class: "muted" }, "Every lamp has a place."));
    return;
  }
  const row = h("div", { class: "row tray-row" });
  for (const lamp of unplaced.slice(0, 40)) {
    row.appendChild(button(`lamp ${lamp.index}`, () => {
      const id = ensureLayout(ctx, capability);
      const selection = ctx.selection.length ? ctx.selection : [];
      const anchor = selection.length
        ? layout.keys.find((key) => {
            const record = lampsOf(layout).find((entry) => entry.index === selection[0]);
            return record && record.key === key.id;
          })
        : null;
      const base = anchor ? geometryOf(anchor) : { x: 0, y: 0 };
      ctx.replaceDraft((draft) => {
        const node = draft.layouts[id];
        const keyId = uniqueId(node.keys, `k${lamp.index}`);
        node.keys.push({ id: keyId, code: null, label: lamp.label || String(lamp.index),
                         group: "unmapped",
                         geometry: { type: "rect", x: (base.x ?? 0) + 1, y: base.y ?? 0,
                                     w: 1, h: 1 } });
        let record = (node.lamps || []).find((entry) => entry.index === lamp.index);
        if (!record) { record = { index: lamp.index }; node.lamps.push(record); }
        record.kind = "key";
        record.key = keyId;
        delete record.zone;
        return draft;
      }, "place lamp");
      ctx.setSelection([lamp.index]);
      ctx.refresh();
    }, { variant: "ghost", sm: true }));
  }
  host.appendChild(row);
  if (unplaced.length > 40) {
    host.appendChild(h("p", { class: "muted small" },
      `…and ${unplaced.length - 40} more. Add missing lamps places them in a column.`));
  }
  void pane;
}

function hudActions(ctx, capability, count) {
  if (!count) return [];
  const actions = [
    button(`Flash ${count}`, () => {
      ctx.hardwareTest({ device: capability.name,
        lamps: ctx.selection.map((lamp) => ({ index: lamp, rgb: [255, 255, 255] })),
        duration_ms: 5000 });
    }, { sm: true }),
  ];
  if (count > 1) {
    actions.push(button("Align left", () => align(ctx, capability, "x"), { sm: true }),
                 button("Align top", () => align(ctx, capability, "y"), { sm: true }));
  }
  actions.push(button("Clear", () => { ctx.setSelection([]); ctx.refresh(); },
    { variant: "ghost", sm: true }));
  return actions;
}

function align(ctx, capability, axis) {
  const layout = resolveLayout(ctx.draft, capability);
  const keys = keysById(layout);
  const records = lampsOf(layout).filter((lamp) => ctx.selection.includes(lamp.index));
  const geometries = records.map((record) => geometryOf(keys.get(record.key)))
    .filter(Boolean);
  if (geometries.length < 2) return;
  const target = Math.min(...geometries.map((geometry) => geometry[axis] ?? 0));
  const id = ensureLayout(ctx, capability);
  ctx.replaceDraft((draft) => {
    const node = draft.layouts[id];
    for (const key of node.keys) {
      const record = (node.lamps || []).find((entry) => entry.key === key.id);
      if (!record || !ctx.selection.includes(record.index)) continue;
      const geometry = geometryOf(key);
      geometry[axis] = target;
      key.geometry = geometry;
    }
    return draft;
  }, `align ${axis}`);
  ctx.refresh();
}

function describeLamp(ctx, capability, lampIndex) {
  const layout = resolveLayout(ctx.draft, capability);
  const record = lampsOf(layout).find((lamp) => lamp.index === lampIndex);
  const key = record?.key ? keysById(layout).get(record.key) : null;
  return h("div", { class: "stack tiny" },
    h("strong", {}, `Lamp ${lampIndex}`),
    h("span", {}, key ? `key: ${key.label || key.code || key.id}` : "no key"),
    h("span", {}, `kind: ${record?.kind || "unknown"} · source: ${record?.source || "—"}`),
    h("span", {}, `zone: ${record?.zone || zoneOfLamp(layout).get(lampIndex) || "—"}`));
}

function ensureLayout(ctx, capability) {
  const entry = (ctx.draft.devices || {})[capability.name] || {};
  if (entry.layout && ctx.draft.layouts?.[entry.layout]) return entry.layout;
  const id = `${capability.name}-default`;
  if (!ctx.draft.layouts?.[id]) {
    ctx.replaceDraft((draft) => {
      draft.layouts = draft.layouts || {};
      draft.layouts[id] = autoLayout(capability.lamps);
      draft.devices = draft.devices || {};
      draft.devices[capability.name] = { ...(draft.devices[capability.name] || {}),
                                         layout: id };
      return draft;
    }, "create layout");
  }
  return id;
}

function uniqueId(keys, base) {
  let id = base;
  const used = new Set(keys.map((key) => key.id));
  while (used.has(id)) id += "_";
  return id;
}

function uniqueZoneId(layout, base) {
  let id = base;
  const used = new Set((layout.zones || []).map((zone) => zone.id));
  while (used.has(id)) id += "_";
  return id;
}
