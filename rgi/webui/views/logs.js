// Logs: the daemon's in-memory ring, polled only while this page is open.

import { h, clear } from "../lib/dom.js";
import { api } from "../lib/api.js";
import { button, table } from "../components/ui.js";

export function render(ctx) {
  const root = h("div", { class: "stack" });
  root.appendChild(h("h1", {}, "Logs"));
  root.appendChild(h("p", { class: "muted" },
    "The daemon's last 500 events: writes that failed, reconnects, config applies, test overlays."));

  let since = 0;
  const body = h("tbody", null);
  const tableEl = h("table", { class: "table" },
    h("caption", null, "Newest last"),
    h("thead", null, h("tr", null,
      h("th", { scope: "col" }, "Time"),
      h("th", { scope: "col" }, "Level"),
      h("th", { scope: "col" }, "Source"),
      h("th", { scope: "col" }, "Message"))),
    body);
  let autoScroll = true;

  const refresh = async () => {
    if (!root.isConnected) { clearInterval(timer); return; }
    try {
      const result = await api(`/ui/api/logs?since=${since}`, { timeout: 4000 });
      for (const line of result.lines || []) {
        body.appendChild(h("tr", null,
          h("td", { class: "mono" }, new Date(line.time * 1000).toLocaleTimeString()),
          h("td", null, h("span", { class: `badge ${line.level === "error" ? "bad" : ""}` }, line.level)),
          h("td", { class: "mono" }, line.source),
          h("td", null, line.message)));
      }
      if (result.next != null) since = result.next;
      while (body.children.length > 500) body.removeChild(body.firstChild);
    } catch {
      /* the connection chip already shows offline */
    }
  };
  const timer = setInterval(refresh, 3000);
  refresh();

  root.appendChild(h("div", { class: "toolbar" },
    button("Refresh", refresh),
    button("Copy diagnostics", async () => {
      const report = {
        generated: new Date().toISOString(),
        url: location.origin,
        revision: ctx.revision,
        connection: ctx.connection,
        config: ctx.draft,
        capabilities: ctx.capabilities,
        logLines: [...body.children].map((row) => [...row.children].map((c) => c.textContent).join(" | ")),
      };
      await navigator.clipboard.writeText(JSON.stringify(report, null, 2));
      ctx.toast("Diagnostics copied");
    })));
  root.appendChild(tableEl);
  return root;
}
