// Agents registry: configured identities, what is observed live, and test runs.

import { h } from "../lib/dom.js";
import { button, checkbox, field, table, textInput, toast, confirmDialog, banner } from "../components/ui.js";

export function render(ctx) {
  const { draft, status } = ctx;
  const agents = draft.agents || [];
  const root = h("div", { class: "stack" });
  root.appendChild(h("h1", {}, "Agents"));
  root.appendChild(h("p", { class: "muted" },
    "Names rgbafi knows how to recognise, with a one-click test that starts and lands a synthetic lane."));

  // -- observed ---------------------------------------------------------
  const sessions = Object.values(status?.sessions || {});
  const observed = new Map();
  for (const session of sessions) {
    const name = session.ident || session.agent || "unknown";
    const entry = observed.get(name) || { name, count: 0, slots: [] };
    entry.count += 1;
    entry.slots.push(session.slot);
    observed.set(name, entry);
  }
  root.appendChild(h("h2", {}, "Observed now"));
  if (observed.size) {
    root.appendChild(h("div", { class: "row" },
      [...observed.values()].map((entry) => h("span", { class: "chip" },
        h("span", { class: "dot", "aria-hidden": "true" }),
        `${entry.name} · lane ${entry.slots.join(", ")}`,
        button("Add to registry", () => {
          const id = entry.name.toLowerCase().replace(/[^a-z0-9._-]+/g, "-");
          if (agents.some((a) => a.id === id)) return toast("Already in the registry");
          ctx.update("agents", [...agents, {
            id, label: entry.name,
            match: { agent: "", ident: entry.name },
            enabled: true, notes: "",
          }], "add agent");
          ctx.refresh();
        }, { variant: "ghost" })))));
  } else {
    root.appendChild(h("p", { class: "muted" }, "No sessions reporting. Install an integration (see Help → Integrations)."));
  }

  // -- registry ---------------------------------------------------------
  root.appendChild(h("h2", {}, "Registry"));
  const rows = agents.map((agent, index) => h("tr", null,
    h("td", null, textInput(agent.id || "", (value) => {
      ctx.update(`agents.${index}.id`, value, "agent id");
    }, { mono: true })),
    h("td", null, textInput(agent.label || "", (value) => {
      ctx.update(`agents.${index}.label`, value, "agent label");
    })),
    h("td", null, textInput(agent.match?.agent || "", (value) => {
      ctx.update(`agents.${index}.match.agent`, value || null, "agent match");
    }, { mono: true, placeholder: "opencode" })),
    h("td", null, textInput(agent.match?.ident || "", (value) => {
      ctx.update(`agents.${index}.match.ident`, value || null, "ident match");
    }, { mono: true, placeholder: "machine" })),
    h("td", null, checkbox("", agent.enabled !== false,
      (checked) => ctx.update(`agents.${index}.enabled`, checked, "agent enabled"))),
    h("td", null, button("Test", async () => {
      try {
        const result = await ctx.api(`/ui/api/agents/${encodeURIComponent(agent.id)}/test`, {
          method: "POST", body: { seconds: 4 },
        });
        toast(`Test lane ${result.slot} (${result.key}) started; it lands in ${result.lands_in_s}s`);
      } catch (err) {
        toast(err.message, "bad");
      }
    }, { variant: "primary" })),
    h("td", null, button("Remove", () => {
      ctx.update("agents", agents.filter((_, i) => i !== index), "remove agent");
      ctx.refresh();
    }, { variant: "ghost" }))));
  root.appendChild(rows.length
    ? table(["Id", "Label", "Agent match", "Ident match", "On", "Test", ""], rows)
    : h("div", { class: "empty" }, "No agents configured yet."));
  root.appendChild(h("div", { class: "toolbar" },
    button("Add agent", () => {
      ctx.update("agents", [...agents, {
        id: `agent-${agents.length + 1}`, label: "New agent",
        match: { agent: "", ident: "" }, enabled: true, notes: "",
      }], "add agent");
      ctx.refresh();
    })));

  root.appendChild(banner("info", h("span", null,
    "Integration setup lives in the docs: ",
    h("a", { href: "/files/integrations.md", target: "_blank", rel: "noreferrer" }, "integrations index"),
    " · ",
    h("a", { href: "/files/AGENT_PROMPT.md", target: "_blank", rel: "noreferrer" }, "agent prompt"),
    " · ",
    h("a", { href: "/files", target: "_blank", rel: "noreferrer" }, "all published files"))));
  return root;
}
