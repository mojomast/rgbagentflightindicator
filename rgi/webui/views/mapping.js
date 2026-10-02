// Mapping: which agent lands on which lane, and what each lane is on each device.

import { h } from "../lib/dom.js";
import { button, checkbox, field, selectInput, table, textInput, toast, confirmDialog } from "../components/ui.js";
import { laneKeys } from "../lib/layout.js";

export function render(ctx) {
  const { draft, capabilities, status } = ctx;
  const root = h("div", { class: "stack" });
  root.appendChild(h("h1", {}, "Mapping"));
  root.appendChild(h("p", { class: "muted" },
    "A preferred lane is advice, not a fence: if it is taken, the session still gets a free one."));

  // -- live sessions ----------------------------------------------------
  const sessions = Object.values(status?.sessions || {}).sort((a, b) => a.slot - b.slot);
  const sessionRows = sessions.map((session) => h("tr", null,
    h("td", { class: "mono" }, session.slot),
    h("td", { class: "mono" }, session.key || "—"),
    h("td", null, session.ident || session.agent || "—"),
    h("td", null, session.label || "—"),
    h("td", null, session.state),
    h("td", null, button("Pin to this lane", () => {
      const name = session.ident || session.agent;
      if (!name) return toast("This session has no ident or agent to pin");
      addOverride(ctx, { ident: session.ident || "", agent: session.ident ? "" : (session.agent || ""), lane: session.slot });
    }, { variant: "ghost" }))));
  root.appendChild(sessionRows.length
    ? table(["Lane", "Key", "Agent", "Task", "State", ""], sessionRows, "Live sessions")
    : h("div", { class: "empty" }, "No sessions are holding lanes right now."));

  // -- lane preferences -------------------------------------------------
  const overrides = draft.lanes?.overrides || [];
  const rows = overrides.map((rule, index) => h("tr", null,
    h("td", null, textInput(rule.id || "", (value) => {
      ctx.update(`lanes.overrides.${index}.id`, value, "override id");
    })),
    h("td", null, textInput(rule.match?.ident || "", (value) => {
      ctx.update(`lanes.overrides.${index}.match.ident`, value || null, "override ident");
    }, { mono: true, placeholder: "machine name" })),
    h("td", null, textInput(rule.match?.agent || "", (value) => {
      ctx.update(`lanes.overrides.${index}.match.agent`, value || null, "override agent");
    }, { mono: true, placeholder: "harness name" })),
    h("td", null, selectInput(
      Array.from({ length: ctx.count() }, (_, lane) => ({ value: String(lane), label: `lane ${lane}` })),
      String(rule.lane ?? 0),
      (value) => ctx.update(`lanes.overrides.${index}.lane`, Number(value), "override lane"))),
    h("td", null, checkbox("", rule.enabled !== false,
      (checked) => ctx.update(`lanes.overrides.${index}.enabled`, checked, "override enabled"))),
    h("td", null, button("Remove", () => {
      ctx.update("lanes.overrides", overrides.filter((_, i) => i !== index), "remove override");
      ctx.refresh();
    }, { variant: "ghost" }))));
  root.appendChild(h("h2", {}, "Preferred lanes"));
  root.appendChild(rows.length
    ? table(["Id", "Ident", "Agent", "Lane", "On", ""], rows)
    : h("div", { class: "empty" }, "No preferences. Agents take the first free lane."));
  root.appendChild(h("div", { class: "toolbar" },
    button("Add preference", () => {
      const next = [...overrides, {
        id: `rule-${overrides.length + 1}`,
        match: { ident: "", agent: "" },
        lane: 0, enabled: true,
      }];
      ctx.update("lanes.overrides", next, "add override");
      ctx.refresh();
    })));

  // -- lane pool matrix -------------------------------------------------
  root.appendChild(h("h2", {}, "Lane pools"));
  root.appendChild(h("p", { class: "muted" },
    "Which lamp each lane is on, per device. Edit the pool in Devices."));
  if (capabilities.length) {
    const laneCount = Math.min(ctx.count(), 12);
    const head = h("tr", null, h("th", {}, "Lane"), ...capabilities.map((cap) => h("th", {}, cap.label)));
    const body = [];
    for (let lane = 0; lane < laneCount; lane += 1) {
      const cells = capabilities.map((cap) => {
        const keys = laneKeys(cap, draft, ctx.count());
        const entry = keys[lane];
        return h("td", { class: "mono" }, entry ? `${entry.label}` : "—");
      });
      body.push(h("tr", null, h("td", { class: "mono" }, lane), ...cells));
    }
    root.appendChild(h("table", { class: "table" },
      h("thead", null, head), h("tbody", null, body)));
  }
  return root;
}

function addOverride(ctx, { ident, agent, lane }) {
  const overrides = ctx.draft.lanes?.overrides || [];
  const next = [...overrides, {
    id: ident || agent || `rule-${overrides.length + 1}`,
    match: { ident: ident || "", agent: agent || "" },
    lane, enabled: true,
  }];
  ctx.update("lanes.overrides", next, "add override");
  toast(`Pinned ${ident || agent} to lane ${lane}`);
  ctx.refresh();
}
