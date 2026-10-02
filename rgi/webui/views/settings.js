// Settings: daemon knobs, import/export, and honest reset actions.

import { h } from "../lib/dom.js";
import { api } from "../lib/api.js";
import { banner, button, checkbox, confirmDialog, field, numberInput, textInput, toast } from "../components/ui.js";
import { deepClone } from "../lib/commands.js";

export function render(ctx) {
  const draft = ctx.draft;
  const root = h("div", { class: "stack" });
  root.appendChild(h("h1", {}, "Settings"));
  root.appendChild(h("p", { class: "muted" },
    "Quiet mode and appearance apply live. Host, port and lane count are startup flags; changes are saved but need a restart."));

  if ((ctx.restart || []).length) {
    root.appendChild(banner("warn", `Restart required for: ${ctx.restart.join(", ")}`));
  }

  const settings = draft.settings || {};
  root.appendChild(h("div", { class: "card grid" },
    field("Bind host", textInput(settings.host || "127.0.0.1", (value) => {
      ctx.update("settings.host", value, "host");
    }, { mono: true }), { hint: "restart required" }),
    field("Port", numberInput(settings.port || 8730, (value) => {
      ctx.update("settings.port", value, "port");
    }, { min: 1, max: 65535 }), { hint: "restart required" }),
    field("Lane count", numberInput(settings.count || 12, (value) => {
      ctx.update("settings.count", value, "count");
    }, { min: 1, max: 512 }), { hint: "restart required" }),
    field("Hold writes while typing", checkbox("", settings.quiet !== false, (checked) => {
      ctx.update("settings.quiet", checked, "quiet");
    }), { hint: "firmware can drop keypresses while repainting" }),
    field("Quiet window (ms)", numberInput(settings.quiet_ms ?? 1500, (value) => {
      ctx.update("settings.quiet_ms", value, "quiet_ms");
    }, { min: 0, max: 60000, step: 100 })),
    field("Reduce motion in this UI", checkbox("", settings.reduce_motion === true, (checked) => {
      ctx.update("settings.reduce_motion", checked, "reduce motion");
    }))));

  root.appendChild(h("h2", {}, "Import / export"));
  root.appendChild(h("div", { class: "toolbar" },
    button("Export config JSON", () => {
      const blob = new Blob([JSON.stringify(ctx.draft, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const link = h("a", { href: url, download: "rgi-config.json" });
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    }),
    importButton(ctx)));

  root.appendChild(h("h2", {}, "Reset"));
  root.appendChild(h("div", { class: "toolbar" },
    button("Reset appearance to defaults", async () => {
      const defaults = await fetchDefaults();
      ctx.replaceDraft((d) => { d.appearance = deepClone(defaults.appearance); return d; }, "reset appearance");
      toast("Appearance reset in the draft");
      ctx.refresh();
    }),
    button("Reset everything to defaults", async () => {
      if (!await confirmDialog("Reset all settings?",
        "This replaces every value — layout, mapping, agents — with the built-in defaults. Nothing touches the panel until you Apply, and the old file is backed up.")) return;
      const defaults = await fetchDefaults();
      ctx.replaceDraft(() => deepClone(defaults), "factory reset");
      toast("Defaults loaded into the draft; review, then Apply");
      ctx.refresh();
    }, { variant: "danger" })));

  root.appendChild(h("h2", {}, "About this config"));
  root.appendChild(h("div", { class: "callout" },
    h("div", {}, `Schema version ${draft.schema_version}, revision ${ctx.revision}`),
    h("div", {}, `Config file: ~/.config/rgi/config.json (backup chain and history snapshots kept beside it)`),
    h("div", {}, `Panel: ${location.origin}`)));
  return root;
}

function importButton(ctx) {
  const input = h("input", {
    type: "file", accept: ".json,application/json", style: { display: "none" },
    onchange: async (event) => {
      const file = event.target.files?.[0];
      if (!file) return;
      try {
        const parsed = JSON.parse(await file.text());
        const check = await api("/ui/api/validate", { method: "POST", body: parsed });
        if (check.errors?.length) {
          return toast(`Import has ${check.errors.length} problem(s); fix them first`, "bad");
        }
        ctx.replaceDraft(() => parsed, "import config");
        toast("Imported into the draft; review, then Apply");
        ctx.refresh();
      } catch (err) {
        toast(`Import failed: ${err.message}`, "bad");
      } finally {
        event.target.value = "";
      }
    },
  });
  return h("span", null, input, button("Import config JSON", () => input.click(), { variant: "ghost" }));
}

async function fetchDefaults() {
  const result = await api("/ui/api/defaults");
  return result.config;
}
