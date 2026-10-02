// Mapping: preferred lanes, the live fleet, and the map with lane badges.
// "Preferred lane" is advice, not a fence; the UI says so everywhere.

import { h, clear } from "../lib/dom.js";
import { button, checkbox, selectInput, textInput, toast } from "../components/ui.js";
import { createMapPane } from "../components/mappane.js";

export function render(ctx) {
  const capability = ctx.device;
  const root = h("div", { class: "stack" });
  root.appendChild(h("div", { class: "view-head" },
    h("h1", {}, "Mapping"),
    h("p", { class: "muted" },
      "Which agent prefers which lane, and where every lane sits on the board.")));

  const paneHost = h("div", { class: "map-host" });
  const sessionsCard = h("div", { class: "card" });
  const rulesCard = h("div", { class: "card" });
  root.append(paneHost, sessionsCard, rulesCard);

  const pane = createMapPane(ctx, {
    mode: "mapping",
    capability,
    describe: (lamp) => describeLamp(ctx, capability, lamp),
    hud: (count) => count ? [button("Clear", () => { ctx.setSelection([]); ctx.refresh(); },
      { variant: "ghost", sm: true })] : [],
  });
  paneHost.appendChild(pane.el);

  const refresh = () => {
    renderSessions(ctx, sessionsCard);
    renderRules(ctx, rulesCard, refresh);
    pane.refresh();
  };
  refresh();
  ctx.subscribe((state) => {
    // keep timers moving without rebuilding the map on every tick
    if (state.status) renderSessions(ctx, sessionsCard);
  });
  return root;
}

function renderSessions(ctx, host) {
  clear(host);
  const sessions = Object.values(ctx.status?.sessions || {}).sort((a, b) => a.slot - b.slot);
  host.appendChild(h("h3", {}, "Live sessions"));
  if (!sessions.length) {
    host.appendChild(h("p", { class: "muted" },
      "No sessions right now. Agents take the first free lane unless a rule below prefers one."));
    return;
  }
  const table = h("table", { class: "table" },
    h("thead", null, h("tr", null,
      h("th", {}, "Lane"), h("th", {}, "Key"), h("th", {}, "Agent"),
      h("th", {}, "Task"), h("th", {}, "State"), h("th", {}, ""))));
  const body = h("tbody");
  for (const session of sessions) {
    body.appendChild(h("tr", null,
      h("td", { class: "mono" }, session.slot),
      h("td", { class: "mono" }, session.key || "—"),
      h("td", null, session.ident || session.agent || "—"),
      h("td", { class: "muted" }, session.label || "—"),
      h("td", null, session.state),
      h("td", null, button("Pin to this lane", () => {
        const name = session.ident || session.agent;
        if (!name) return toast("This session has no ident or agent to pin");
        addRule(ctx, { ident: session.ident || "", agent: session.ident ? "" : (session.agent || ""),
                       lane: session.slot });
      }, { variant: "ghost", sm: true, title: `Always prefer lane ${session.slot}` }))));
  }
  table.appendChild(body);
  host.appendChild(table);
}

function renderRules(ctx, host, refresh) {
  clear(host);
  const rules = ctx.draft.lanes?.overrides || [];
  host.appendChild(h("h3", {}, "Preferred lanes"));
  host.appendChild(h("p", { class: "muted small" },
    "Matched on ident first, then agent. If the lane is taken the session still gets a free one."));
  if (rules.length) {
    const table = h("table", { class: "table" },
      h("thead", null, h("tr", null,
        h("th", {}, "Id"), h("th", {}, "Ident"), h("th", {}, "Agent"),
        h("th", {}, "Lane"), h("th", {}, "On"), h("th", {}, ""))));
    const body = h("tbody");
    rules.forEach((rule, index) => {
      body.appendChild(h("tr", null,
        h("td", null, textInput(rule.id || "", (value) => {
          ctx.update(`lanes.overrides.${index}.id`, value, "rule id");
        })),
        h("td", null, textInput(rule.match?.ident || "", (value) => {
          ctx.update(`lanes.overrides.${index}.match.ident`, value || null, "rule ident");
        }, { mono: true, placeholder: "machine" })),
        h("td", null, textInput(rule.match?.agent || "", (value) => {
          ctx.update(`lanes.overrides.${index}.match.agent`, value || null, "rule agent");
        }, { mono: true, placeholder: "harness" })),
        h("td", null, selectInput(
          Array.from({ length: ctx.count() }, (_, lane) => ({ value: String(lane), label: `lane ${lane}` })),
          String(rule.lane ?? 0),
          (value) => ctx.update(`lanes.overrides.${index}.lane`, Number(value), "rule lane"))),
        h("td", null, checkbox("", rule.enabled !== false,
          (checked) => ctx.update(`lanes.overrides.${index}.enabled`, checked, "rule enabled"))),
        h("td", null, button("Remove", () => {
          ctx.update("lanes.overrides", rules.filter((_, i) => i !== index), "remove rule");
          refresh();
        }, { variant: "ghost", sm: true }))));
    });
    table.appendChild(body);
    host.appendChild(table);
  } else {
    host.appendChild(h("p", { class: "muted" },
      "No rules. Agents take the first free lane; add one to pin an agent to a key."));
  }
  host.appendChild(button("Add a rule", () => {
    const next = [...rules, {
      id: `rule-${rules.length + 1}`, match: { ident: "", agent: "" },
      lane: 0, enabled: true,
    }];
    ctx.update("lanes.overrides", next, "add rule");
    refresh();
  }));
}

function addRule(ctx, { ident, agent, lane }) {
  const rules = ctx.draft.lanes?.overrides || [];
  const next = [...rules, {
    id: ident || agent || `rule-${rules.length + 1}`,
    match: { ident: ident || "", agent: agent || "" },
    lane, enabled: true,
  }];
  ctx.update("lanes.overrides", next, "add rule");
  toast(`Preferred lane ${lane} for ${ident || agent}`);
  ctx.refresh();
}

function describeLamp(ctx, capability, lampIndex) {
  const slot = (capability?.pool || []).indexOf(lampIndex);
  const session = slot >= 0
    ? Object.values(ctx.status?.sessions || {}).find((entry) => entry.slot === slot)
    : null;
  return h("div", { class: "stack tiny" },
    h("strong", {}, slot >= 0 ? `Lane ${slot} · lamp ${lampIndex}` : `Lamp ${lampIndex}`),
    session ? h("span", {}, `${session.ident || session.agent} · ${session.state}`)
            : h("span", { class: "muted" }, "no session"));
}
