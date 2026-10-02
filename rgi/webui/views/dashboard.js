// Dashboard: the fleet at a glance, the live lane table, and quick actions.

import { h } from "../lib/dom.js";
import { button, chip, table, banner, confirmDialog, toast } from "../components/ui.js";
import { renderDiagram, sessionsBySlot, stateForSlot } from "../components/diagram.js";
import { layoutFor } from "../lib/layout.js";

const ORDER = ["working", "blocked", "error", "done", "idle"];

export function render(ctx) {
  const { status, draft, capabilities } = ctx;
  const cap = ctx.capability(ctx.deviceName) || capabilities[0];
  const root = h("div", { class: "stack" });

  root.appendChild(h("div", null,
    h("h1", {}, "Dashboard"),
    h("p", { class: "muted" }, "One lane per agent session, painted as it changes.")));

  if (ctx.connection === "offline") {
    root.appendChild(banner("warn", "The panel is unreachable — retrying. Your draft is kept, and Apply is disabled until it is back."));
  }

  const sessions = Object.values(status?.sessions || {});
  const counts = {};
  for (const session of sessions) counts[session.state || "idle"] = (counts[session.state || "idle"] || 0) + 1;
  root.appendChild(h("div", { class: "row" },
    ORDER.map((state) => chip(state, `${state} · ${counts[state] || 0}`))));

  root.appendChild(h("div", { class: "toolbar" },
    button("Test this device (15 s)", () => ctx.hardwareTest(), { variant: "primary" }),
    button("Stop hardware test", () => ctx.stopTest(), { variant: "ghost" }),
    button("Clear all lanes", async () => {
      if (await confirmDialog("Clear every lane?", "Sessions will re-report on their next update.")) {
        await ctx.api("/clear", { method: "POST", body: {} });
        ctx.refreshStatus();
      }
    }, { variant: "ghost" })));

  const rows = sessions
    .sort((a, b) => a.slot - b.slot)
    .map((session) => h("tr", null,
      h("td", { class: "mono" }, session.key || session.slot),
      h("td", null, chip(session.state, session.state)),
      h("td", null, session.ident || session.agent || "—"),
      h("td", { class: "muted" }, session.label || "—"),
      h("td", { class: "num" }, session.in_flight_s != null ? `${session.in_flight_s}s` : "—"),
      h("td", { class: "num" }, session.idle_s != null ? `${session.idle_s}s` : "—"),
      h("td", null, button("End", async () => {
        await ctx.api("/session/end", { method: "POST", body: { sessionID: session.sessionID } });
        toast(`Lane ${session.slot} released`);
        ctx.refreshStatus();
      }, { variant: "ghost" }))));
  root.appendChild(rows.length
    ? table(["Key", "State", "Agent", "Task", "In flight", "Idle", ""], rows,
            `Lanes (${sessions.length}/${status?.lanes ?? 0})`)
    : h("div", { class: "empty" }, "No sessions yet. Install an integration, or press the OpenCode sidebar's copy button."));

  if (cap) {
    const layout = layoutFor(draft, cap);
    const bySlot = sessionsBySlot(status);
    const overrides = new Map();
    for (const override of draft.lamp_overrides || []) {
      if (override.device === cap.name) overrides.set(override.lamp, override.color);
    }
    const lanePool = new Set((draft.devices?.[cap.name]?.lane_pool) || []);
    root.appendChild(h("h2", {}, `${cap.label} — ${layout.keys.length} lamps`));
    root.appendChild(h("div", { class: "card keyboard-wrap" }, renderDiagram({
      layout,
      stateFor: (lamp) => {
        const base = stateForSlot(bySlot.get((cap.pool || []).indexOf(lamp)));
        const colour = overrides.get(lamp);
        return { state: base.state, blink: base.blink, override: colour ? colour : null };
      },
      laneSet: lanePool,
      blinkPeriod: draft.appearance?.blink?.period_ms || 560,
      onSelect: (key) => ctx.go(`#/layout?lamp=${key.lamp}`),
    })));
  } else {
    root.appendChild(h("div", { class: "empty" }, "No devices detected."));
  }
  return root;
}
