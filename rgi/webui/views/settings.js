// Settings: daemon knobs, import/export, and honest reset actions.

import { h } from "../lib/dom.js";
import { api } from "../lib/api.js";
import { banner, button, checkbox, confirmDialog, dialog, field, numberInput, textInput, toast } from "../components/ui.js";
import { deepClone } from "../lib/commands.js";

const REACH_BLOCKS = [
  { id: "notify", label: "Notifications",
    note: "Runs a command when a lane enters an attention state (blocked, error, …).",
    fields: ["enabled", "states", "min_duration_s", "cooldown_s", "repeat_s",
             "quiet_hours", "dry_run", "command"] },
  { id: "mqtt", label: "MQTT",
    note: "Publishes lane state and Home Assistant discovery.",
    fields: ["enabled", "host", "port", "username", "password", "base_topic",
             "discovery", "tls"] },
  { id: "otlp", label: "OTLP", note: "Exports traces/metrics to a collector.",
    fields: ["enabled"] },
  { id: "history", label: "History",
    note: "The digest ring that feeds this page's \"While you were away\" card.",
    fields: ["enabled", "max_bytes"] },
];

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

  root.appendChild(h("h2", {}, "Reach"));
  root.appendChild(h("p", { class: "muted" },
    "Read-only: the notify, MQTT, OTLP and history blocks come from the active config. " +
    "Editing them here will come in a later version; for now change the file (and restart if the daemon caches it)."));
  const source = ctx.config && typeof ctx.config === "object" ? ctx.config : (draft || {});
  const blocks = REACH_BLOCKS.filter((block) =>
    source[block.id] && typeof source[block.id] === "object");
  if (!blocks.length) {
    root.appendChild(h("p", { class: "muted" },
      "This config has no notify, mqtt, otlp or history blocks — the daemon may predate them."));
  } else {
    root.appendChild(h("div", { class: "grid wide" },
      blocks.map((block) => reachCard(block, source[block.id]))));
  }
  return root;
}

function reachCard(block, values) {
  const card = h("section", { class: "card reach-card" });
  card.appendChild(h("h3", null, block.label,
    values.enabled === false ? h("span", { class: "muted small" }, " · off") : null));
  card.appendChild(h("p", { class: "muted small" }, block.note));
  const list = h("dl", { class: "reach-list" });
  for (const key of block.fields) {
    if (!(key in values)) continue;
    list.appendChild(h("dt", { class: "mono" }, key));
    list.appendChild(h("dd", null, reachValue(key, values[key])));
  }
  if (block.id === "notify" && Array.isArray(values.command) && values.command.length) {
    card.appendChild(h("div", { class: "toolbar" }, testNotification(values.command)));
  }
  card.appendChild(list);
  return card;
}

function reachValue(key, value) {
  if (key === "password") {
    return h("span", { class: "muted" }, value ? "set (hidden)" : "not set");
  }
  if (value == null || value === "") return h("span", { class: "muted" }, "—");
  if (typeof value === "boolean") {
    return h("span", { class: `badge${value ? " ok" : ""}` }, value ? "on" : "off");
  }
  if (typeof value === "number") return h("span", { class: "mono" }, String(value));
  if (Array.isArray(value)) {
    if (!value.length) return h("span", { class: "muted" }, "—");
    if (key === "command") return h("code", { class: "mono" }, value.map(shellQuote).join(" "));
    return h("span", null, value.join(", "));
  }
  return h("span", null, String(value));
}

/** Quote one argv entry so the copied text can run in a POSIX shell too. */
function shellQuote(arg) {
  const text = String(arg ?? "");
  if (text === "") return "''";
  if (/^[A-Za-z0-9_@%+=:,./-]+$/.test(text)) return text;
  return `'${text.replaceAll("'", `'\\''`)}'`;
}

function testNotification(command) {
  const text = command.map(shellQuote).join(" ");
  return button("Test notification", () => {
    const copy = button("Copy command", async () => {
      try {
        await navigator.clipboard.writeText(text);
        toast("Command copied");
      } catch {
        toast("Copy failed — select the command text and copy it by hand", "bad");
      }
    }, { sm: true });
    dialog({
      title: "Test notification",
      body: h("div", { class: "stack" },
        h("p", {}, "Sending a test is not wired up yet — there is no endpoint for it, " +
          "so this cannot actually run the command. When a lane notification fires, it runs exactly:"),
        h("pre", { class: "mono reach-command" }, text),
        h("p", { class: "muted small" },
          "The daemon runs the argv directly, not through a shell; the event arrives as JSON " +
          "on stdin and in RGI_* environment variables. Copy it to a terminal to verify it works."),
        copy),
      actions: [{ label: "Close", value: null, primary: true }],
    });
  }, { variant: "ghost", sm: true, title: "Show the exact command a notification would run" });
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
