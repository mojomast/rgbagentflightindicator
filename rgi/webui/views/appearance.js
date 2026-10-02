// Appearance: every element of every state — colour, pattern, timing, label,
// brightness — plus static lamp overrides and accessibility warnings.

import { h, clear } from "../lib/dom.js";
import { button, colorField, field, numberInput, selectInput, textInput, banner, toast } from "../components/ui.js";
import { contrast, differsEnough, hexToRgb, readableOn, simulate } from "../lib/color.js";
import { renderSpec, stateSpec } from "../lib/effects.js";

const STATES = ["working", "done", "blocked", "error", "idle", "off"];
const PATTERNS = ["off", "steady", "blink", "breathe"];
const PRESETS = {
  classic: {
    working: { color: "#00ff00", pattern: "steady" },
    done: { color: "#ffffff", pattern: "blink", cycles: 10, then: "steady" },
    blocked: { color: "#ff0000", pattern: "blink", cycles: 0, then: "steady" },
    error: { color: "#ff0000", pattern: "steady" },
    idle: { color: "#282828", pattern: "steady" },
    off: { color: "#000000", pattern: "off" },
  },
  colorblind: {
    working: { color: "#3b82f6", pattern: "steady" },
    done: { color: "#22d3ee", pattern: "blink", cycles: 10, then: "steady" },
    blocked: { color: "#f97316", pattern: "blink", cycles: 0, then: "steady" },
    error: { color: "#e11d48", pattern: "steady" },
    idle: { color: "#3f3f46", pattern: "steady" },
    off: { color: "#000000", pattern: "off" },
  },
  highContrast: {
    working: { color: "#00ff00", pattern: "steady" },
    done: { color: "#ffffff", pattern: "steady" },
    blocked: { color: "#ff0000", pattern: "blink", cycles: 0, then: "steady" },
    error: { color: "#ff00ff", pattern: "blink", cycles: 0, then: "steady" },
    idle: { color: "#777777", pattern: "steady" },
    off: { color: "#000000", pattern: "off" },
  },
};

export function render(ctx) {
  const root = h("div", { class: "stack" });
  root.appendChild(h("h1", {}, "Appearance"));
  root.appendChild(h("p", { class: "muted" },
    "These are the actual colours and effects written to the LEDs. The preview on the right updates as you type."));

  root.appendChild(h("div", { class: "row" },
    button("Classic", () => applyPreset(ctx, "classic"), {}),
    button("Colour-blind safe", () => applyPreset(ctx, "colorblind"), {}),
    button("High contrast", () => applyPreset(ctx, "highContrast"), {})));

  const warnings = collectWarnings(ctx.draft);
  for (const warning of warnings) root.appendChild(banner("warn", warning));

  root.appendChild(h("h2", {}, "Global timing"));
  root.appendChild(h("div", { class: "card row" },
    field("Blink period (ms)",
      numberInput(ctx.get("appearance.blink.period_ms"), (value) => {
        ctx.update("appearance.blink.period_ms", value, "blink period");
      }, { min: 40, max: 10000, step: 20 })),
    field("Duty",
      numberInput(ctx.get("appearance.blink.duty"), (value) => {
        ctx.update("appearance.blink.duty", value, "blink duty");
      }, { min: 0, max: 1, step: 0.05 }))));

  root.appendChild(h("h2", {}, "States"));
  const grid = h("div", { class: "grid wide" });
  for (const state of STATES) {
    grid.appendChild(stateCard(ctx, state));
  }
  root.appendChild(grid);

  root.appendChild(lampOverrides(ctx));
  return root;
}

function stateCard(ctx, state) {
  const path = `appearance.states.${state}`;
  const card = h("div", { class: "card" });
  card.appendChild(h("h3", null,
    h("span", { class: "chip", "data-state": state },
      h("span", { class: "dot", "aria-hidden": "true" }), state)));
  card.appendChild(colorField("Colour", ctx.get(`${path}.color`), (value) => {
    ctx.update(`${path}.color`, value, "state colour", `${state}-color`);
  }));

  const spec = stateSpec(ctx.draft, state);
  const swatch = h("div", {
    class: "card", "aria-label": `${state} effect preview`,
    style: { background: "#000", color: "#fff", textAlign: "center",
             minHeight: "64px", display: "flex", alignItems: "center", justifyContent: "center",
             fontWeight: "600" },
  }, `${state} preview`);
  animateSwatch(swatch, () => stateSpec(ctx.draft, state), ctx.reduceMotion());
  card.appendChild(swatch);

  card.appendChild(field("Pattern",
    selectInput(PATTERNS, spec.pattern, (value) => {
      ctx.update(`${path}.pattern`, value, "state pattern");
      ctx.refresh();
    })));
  if (spec.pattern === "blink" || spec.pattern === "breathe") {
    card.appendChild(h("div", { class: "row" },
      field("Period (ms)", numberInput(ctx.get(`${path}.period_ms`), (value) => {
        ctx.update(`${path}.period_ms`, value, "state period", `${state}-period`);
      }, { min: 40, max: 10000, step: 20 })),
      field("Duty", numberInput(ctx.get(`${path}.duty`), (value) => {
        ctx.update(`${path}.duty`, value, "state duty", `${state}-duty`);
      }, { min: 0, max: 1, step: 0.05 })),
      field("Cycles (0 = forever)", numberInput(ctx.get(`${path}.cycles`), (value) => {
        ctx.update(`${path}.cycles`, value, "state cycles", `${state}-cycles`);
      }, { min: 0, max: 10000 }))));
    card.appendChild(field("After the cycles",
      selectInput(["steady", "off"], ctx.get(`${path}.then`), (value) => {
        ctx.update(`${path}.then`, value, "state then");
      })));
  }
  card.appendChild(h("div", { class: "row" },
    field("Brightness (0–255)", numberInput(ctx.get(`${path}.brightness`), (value) => {
      ctx.update(`${path}.brightness`, value, "state brightness", `${state}-brightness`);
    }, { min: 0, max: 255 })),
    field("Label", textInput(ctx.get(`${path}.label`), (value) => {
      ctx.update(`${path}.label`, value, "state label");
    }))));
  return card;
}

function animateSwatch(el, getSpec, quiet) {
  const started = performance.now();
  const tick = (now) => {
    if (!el.isConnected) return;
    const spec = getSpec();
    const colour = renderSpec(spec, (now - started) / 1000, quiet);
    el.style.background = `rgb(${colour[0]}, ${colour[1]}, ${colour[2]})`;
    el.style.color = readableOn(colour);
    el.textContent = `${spec.pattern}${spec.pattern === "steady" || spec.pattern === "off" ? "" : ` · ${spec.periodMs}ms`}`;
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

function applyPreset(ctx, name) {
  ctx.replaceDraft((draft) => {
    for (const [state, patch] of Object.entries(PRESETS[name])) {
      draft.appearance.states[state] = { ...draft.appearance.states[state], ...patch };
    }
    return draft;
  }, `preset ${name}`);
  toast(`Applied the ${name} palette as a draft`);
  ctx.refresh();
}

function collectWarnings(draft) {
  const out = [];
  const states = draft.appearance?.states || {};
  const surface = hexToRgb("#18191b");
  for (const [name, spec] of Object.entries(states)) {
    if (!spec.color || spec.pattern === "off") continue;
    const rgb = hexToRgb(spec.color);
    if (spec.pattern !== "steady") continue; // blinking spends time dark; only steady colours are judged
    const ratio = contrast(rgb, surface);
    if (ratio < 3) out.push(`${name}: colour ${spec.color} is only ${ratio.toFixed(1)}:1 against the panel background — the label carries the meaning.`);
  }
  const kinds = ["protanopia", "deuteranopia", "tritanopia"];
  const names = Object.keys(states).filter((n) => states[n].color && states[n].pattern !== "off");
  for (const kind of kinds) {
    for (let i = 0; i < names.length; i += 1) {
      for (let j = i + 1; j < names.length; j += 1) {
        const a = simulate(hexToRgb(states[names[i]].color), kind);
        const b = simulate(hexToRgb(states[names[j]].color), kind);
        if (!differsEnough(a, b)) {
          out.push(`${names[i]} and ${names[j]} look alike with ${kind}; the icons/labels still distinguish them.`);
        }
      }
    }
  }
  return [...new Set(out)];
}

function lampOverrides(ctx) {
  const overrides = ctx.draft.lamp_overrides || [];
  const cap = ctx.capability(ctx.deviceName) || ctx.capabilities[0];
  const card = h("div", { class: "card" });
  card.appendChild(h("h2", { style: { marginTop: "0" } }, "Static lamp overrides"));
  card.appendChild(h("p", { class: "muted" },
    "A lamp with a static override ignores lane state and stays one colour — useful for a logo key or a reference light."));
  const rows = overrides.map((override, index) => h("tr", null,
    h("td", null, override.device),
    h("td", { class: "mono" }, override.lamp),
    h("td", null, h("span", {
      style: { display: "inline-block", width: "18px", height: "18px", borderRadius: "4px",
               background: override.color, border: "1px solid var(--border)" },
    })),
    h("td", null, override.color),
    h("td", null, button("Remove", () => {
      ctx.update("lamp_overrides", overrides.filter((_, i) => i !== index), "remove override");
      ctx.refresh();
    }, { variant: "ghost" }))));
  if (rows.length) {
    card.appendChild(h("table", { class: "table" },
      h("thead", null, h("tr", null,
        h("th", {}, "Device"), h("th", {}, "Lamp"), h("th", {}, "Colour"), h("th", {}, "Hex"), h("th", {}, ""))),
      h("tbody", null, rows)));
  }
  if (cap) {
    const lampSelect = selectInput(cap.lamps.slice(0, 200).map((lamp) => ({
      value: String(lamp.index), label: `${lamp.label} (lamp ${lamp.index})`,
    })), String(ctx.selection[0] ?? ""), () => {});
    const colour = h("input", { type: "color", value: "#00aaff", "aria-label": "Override colour" });
    card.appendChild(h("div", { class: "row" },
      lampSelect, colour,
      button("Add override for selected lamp", () => {
        const lamp = ctx.selection[0] ?? Number(lampSelect.value);
        if (Number.isNaN(lamp)) return toast("Pick a lamp first");
        const next = overrides.filter((o) => !(o.device === cap.name && o.lamp === lamp));
        next.push({ device: cap.name, lamp, mode: "static", color: colour.value });
        ctx.update("lamp_overrides", next, "lamp override");
        ctx.refresh();
      })));
  }
  return card;
}
