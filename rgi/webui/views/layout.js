// Layout editor: which physical keys are the indicator, what they are called,
// where they sit, and which order becomes lanes 0..N-1.

import { h, clear } from "../lib/dom.js";
import { autoLayout, codeLabel, defaultLanePool, download, keysByLamp, layoutFor, toCsv } from "../lib/layout.js";
import { setPath } from "../lib/commands.js";
import { banner, button, captureKey, field, numberInput, selectInput, textInput, toast, confirmDialog } from "../components/ui.js";
import { renderDiagram, sessionsBySlot, stateForSlot } from "../components/diagram.js";

export function render(ctx) {
  const { draft, capabilities, status } = ctx;
  const cap = ctx.capability(ctx.deviceName) || capabilities[0];
  const root = h("div", { class: "stack" });
  if (!cap) {
    root.appendChild(h("div", { class: "empty" }, "No device detected."));
    return root;
  }
  const entry = draft.devices?.[cap.name] || {};
  const hasLayout = entry.layout && draft.layouts?.[entry.layout];
  const layout = hasLayout ? draft.layouts[entry.layout] : autoLayout(cap.lamps);
  const selection = ctx.selection;
  const byLamp = keysByLamp(layout);

  root.appendChild(h("h1", {}, `Layout — ${cap.label}`));
  root.appendChild(h("p", { class: "muted" },
    hasLayout
      ? "Click a key to edit it; shift-click to multi-select. Changes stay a draft until Apply."
      : "This device is using an auto-generated diagram. Edit anything and a real layout is created."));

  root.appendChild(h("div", { class: "toolbar" },
    button("Use auto-layout", () => {
      const id = ensureLayout(ctx, cap);
      ctx.replaceDraft((d) => {
        d.layouts[id] = autoLayout(cap.lamps);
        return d;
      }, "auto layout");
      ctx.setSelection([]);
      ctx.refresh();
    }),
    button("Add missing lamps", () => {
      const id = ensureLayout(ctx, cap);
      ctx.replaceDraft((d) => {
        const current = d.layouts[id];
        const present = new Set(current.keys.map((k) => k.lamp));
        let row = current.keys.reduce((max, k) => Math.max(max, (k.y || 0) + 1), 0);
        for (const lamp of cap.lamps) {
          if (present.has(lamp.index)) continue;
          current.keys.push({ lamp: lamp.index, label: lamp.label || `lamp ${lamp.index}`,
            code: null, x: 0, y: row, w: 1, h: 1, group: lamp.group || "" });
          row += 1;
        }
        return d;
      }, "add missing lamps");
      ctx.refresh();
    }, { variant: "ghost" }),
    button("Flash selected (5 s)", () => {
      if (!selection.length) return toast("Select at least one key first");
      ctx.test({ device: cap.name, lamps: selection.map((lamp) => ({ index: lamp, rgb: [255, 255, 255] })), duration_ms: 5000 });
    }, { variant: "primary" }),
    button("Export JSON", () => {
      const id = ensureLayout(ctx, cap);
      download(`${cap.name}-layout.json`, JSON.stringify(draft.layouts[id], null, 2));
    }),
    button("Export CSV", () => {
      const id = ensureLayout(ctx, cap);
      download(`${cap.name}-layout.csv`, toCsv(draft.layouts[id].keys), "text/csv");
    }),
    importButton(ctx, cap)));

  if (!hasLayout) {
    root.appendChild(banner("info", "Auto-layout is a guess from lamp order. Use the mapping wizard to find the real keys."));
  }

  // -- diagram + inspector ---------------------------------------------
  const bySlot = sessionsBySlot(status);
  const overrides = new Map();
  for (const override of draft.lamp_overrides || []) {
    if (override.device === cap.name) overrides.set(override.lamp, override.color);
  }
  const diagram = renderDiagram({
    layout,
    stateFor: (lamp) => {
      const base = stateForSlot(bySlot.get((cap.pool || []).indexOf(lamp)));
      return { state: base.state, blink: base.blink, override: overrides.get(lamp) || null };
    },
    selected: new Set(selection),
    laneSet: new Set(entry.lane_pool || []),
    blinkPeriod: draft.appearance?.blink?.period_ms || 560,
    onSelect: (key, { additive, focusOnly }) => {
      if (focusOnly) return;
      if (additive) {
        const next = selection.includes(key.lamp) ? selection.filter((l) => l !== key.lamp) : [...selection, key.lamp];
        ctx.setSelection(next);
      } else {
        ctx.setSelection([key.lamp]);
      }
      ctx.refresh();
    },
  });

  const panel = h("div", { class: "grid wide" },
    h("div", { class: "card keyboard-wrap" }, diagram),
    h("div", { class: "card" }, inspector(ctx, cap, layout, selection, byLamp)));

  root.appendChild(panel);
  root.appendChild(lanePoolCard(ctx, cap, layout, byLamp));
  return root;
}

function ensureLayout(ctx, cap) {
  const entry = ctx.draft.devices?.[cap.name] || {};
  let id = entry.layout;
  if (id && ctx.draft.layouts?.[id]) return id;
  id = `${cap.name}-default`;
  ctx.replaceDraft((d) => {
    if (!d.layouts) d.layouts = {};
    d.layouts[id] = autoLayout(cap.lamps);
    if (!d.devices) d.devices = {};
    d.devices[cap.name] = { ...(d.devices[cap.name] || {}), layout: id };
    return d;
  }, "create layout");
  return id;
}

function inspector(ctx, cap, layout, selection, byLamp) {
  if (!selection.length) {
    return h("div", null,
      h("h3", {}, "Inspector"),
      h("p", { class: "muted" }, "Select a key in the diagram (or tab to it and press Enter)."),
      h("p", { class: "muted small" }, "Shift-click extends the selection. Arrow keys move between keys."));
  }
  if (selection.length > 1) {
    return h("div", null,
      h("h3", {}, `${selection.length} keys selected`),
      h("div", { class: "stack" },
        field("Label for all selected", textInput("", (value) => {
          const id = ensureLayout(ctx, cap);
          ctx.replaceDraft((d) => {
            for (const key of d.layouts[id].keys) if (selection.includes(key.lamp)) key.label = value;
            return d;
          }, "label keys");
          ctx.refresh();
        })),
        button("Delete selected keys", () => {
          const id = ensureLayout(ctx, cap);
          ctx.replaceDraft((d) => {
            d.layouts[id].keys = d.layouts[id].keys.filter((key) => !selection.includes(key.lamp));
            return d;
          }, "delete keys");
          ctx.setSelection([]);
          ctx.refresh();
        }, { variant: "danger" })));
  }
  const lamp = selection[0];
  const key = byLamp.get(lamp) || { lamp, label: "", code: null, x: 0, y: 0, w: 1, h: 1 };
  const id = ensureLayout(ctx, cap);
  const patch = (field, value, label = field) => {
    ctx.replaceDraft((d) => {
      let node = d.layouts[id].keys.find((k) => k.lamp === lamp);
      if (!node) { node = { lamp }; d.layouts[id].keys.push(node); }
      node[field] = value;
      return d;
    }, label, `${lamp}-${field}`);
  };
  return h("div", null,
    h("h3", {}, `Lamp ${lamp}`),
    field("Label", textInput(key.label || "", (value) => patch("label", value, "key label"),
      { ariaLabel: "Key label" })),
    field("Physical key", h("div", { class: "row" },
      h("span", { class: "mono" }, key.code || "not identified"),
      button("Press it…", () => captureKey((code) => {
        patch("code", code, "key code");
        if (!key.label || key.label === `lamp ${lamp}`) patch("label", codeLabel(code), "key label");
        toast(`Captured ${code} — ${codeLabel(code)}`);
        ctx.refresh();
      }))),
      { hint: "lights nothing; press the key the lamp is under and it names itself" }),
    h("div", { class: "row" },
      field("x", numberInput(key.x ?? 0, (value) => patch("x", value), { step: 0.25 })),
      field("y", numberInput(key.y ?? 0, (value) => patch("y", value), { step: 0.25 })),
      field("w", numberInput(key.w ?? 1, (value) => patch("w", value), { step: 0.25, min: 0.25 })),
      field("h", numberInput(key.h ?? 1, (value) => patch("h", value), { step: 0.25, min: 0.25 }))),
    field("Group", textInput(key.group || "", (value) => patch("group", value))),
    button("Delete this key", () => {
      const next = selection.map((l) => l).filter((l) => l !== lamp);
      ctx.replaceDraft((d) => {
        d.layouts[id].keys = d.layouts[id].keys.filter((k) => k.lamp !== lamp);
        return d;
      }, "delete key");
      ctx.setSelection(next);
      ctx.refresh();
    }, { variant: "danger" }));
}

function lanePoolCard(ctx, cap, layout, byLamp) {
  const entry = ctx.draft.devices?.[cap.name] || {};
  const pool = Array.isArray(entry.lane_pool) ? entry.lane_pool : defaultLanePool(layout, ctx.count());
  const rows = pool.map((lamp, lane) => {
    const key = byLamp.get(lamp);
    return h("tr", null,
      h("td", { class: "mono" }, lane),
      h("td", { class: "mono" }, lamp),
      h("td", null, key ? key.label : "—"),
      h("td", { class: "muted" }, key ? (key.code || "") : ""));
  });
  return h("div", { class: "stack" },
    h("h2", {}, "Lane pool"),
    h("p", { class: "muted" }, "Lane 0 gets the first lamp. Agents land on these keys by default."),
    h("table", { class: "table" },
      h("thead", null, h("tr", null, h("th", {}, "Lane"), h("th", {}, "Lamp"), h("th", {}, "Key"), h("th", {}, "Code"))),
      h("tbody", null, rows)),
    h("div", { class: "row" },
      button("Set pool to selection order", () => {
        if (!ctx.selection.length) return toast("Select keys first");
        ctx.update(`devices.${cap.name}.lane_pool`, [...ctx.selection], "lane pool");
        ctx.refresh();
      }),
      button("Reset to number row first", () => {
        ctx.update(`devices.${cap.name}.lane_pool`, defaultLanePool(layout, ctx.count()), "lane pool");
        ctx.refresh();
      }, { variant: "ghost" })));
}

function importButton(ctx, cap) {
  const input = h("input", {
    type: "file", accept: ".json,application/json", style: { display: "none" },
    onchange: async (event) => {
      const file = event.target.files?.[0];
      if (!file) return;
      try {
        const parsed = JSON.parse(await file.text());
        if (!parsed || !Array.isArray(parsed.keys)) throw new Error("no keys array");
        const id = ensureLayout(ctx, cap);
        ctx.replaceDraft((d) => { d.layouts[id] = parsed; return d; }, "import layout");
        toast(`Imported ${parsed.keys.length} keys`);
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
