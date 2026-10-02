// Live: the map, the fleet table, the onboarding checklist. This is the page
// that must never lie: timers tick, the map follows the stream, and Apply is
// disabled while the panel is unreachable.

import { h, clear } from "../lib/dom.js";
import { button, chip, toast, banner } from "../components/ui.js";
import { createMapPane } from "../components/mappane.js";
import { resolveLayout, defaultLanePool } from "../lib/layout.js";
import { effectiveLamp, formatDuration, sessionForLamp } from "../lib/effective.js";
import { lampsOf, zoneOfLamp } from "../lib/topology.js";
import {
  CONTEXT_BAD_PERCENT, CONTEXT_WARN_PERCENT, aggregate, compactTokens,
  contextLevel, formatContext, formatCost,
} from "../lib/cost.js";

const ORDER = ["working", "blocked", "error", "done", "idle"];
const LAST_SEEN_KEY = "rgi.lastSeen";

export function render(ctx) {
  const capability = ctx.device;
  const root = h("div", { class: "stack live" });
  const header = h("div", { class: "view-head" });
  const checklist = h("div", { class: "stack" });
  const digestHost = h("div", { class: "digest-host" });
  const paneHost = h("div", { class: "map-host" });
  const tableHost = h("div", { class: "lane-table-host" });
  root.append(header, checklist, digestHost, paneHost, tableHost);

  const pane = createMapPane(ctx, {
    mode: "live",
    capability,
    describe: (lamp) => describeLamp(ctx, capability, lamp),
    hud: (count) => hudActions(ctx, count),
    onActivate: (lamp) => {
      ctx.setSelection([lamp]);
      pane.refresh();
    },
  });
  paneHost.appendChild(pane.el);

  let lastSignature = "";
  const update = () => {
    const s = ctx.store.get();
    const signature = s.status
      ? `${Object.entries(s.status.sessions || {})
          .map(([id, session]) => [id, session.state, session.changedMs ?? "",
            session.acked ? "a" : "", session.stale ? "s" : "",
            session.info?.context?.percent ?? "",
            session.info?.tokens?.cost ?? ""].join(":"))
          .join("|")}|${s.dirty}`
      : "none";
    renderHeader(ctx, header);
    renderChecklist(ctx, checklist, capability);
    if (signature !== lastSignature) {
      lastSignature = signature;
      pane.refresh();
      renderTable(ctx, tableHost, update);
    } else {
      renderTimers(tableHost);
    }
  };
  update();
  ctx.subscribe(update);
  void renderDigest(ctx, digestHost);
  return root;
}

function renderHeader(ctx, host) {
  clear(host);
  const sessions = Object.values(ctx.status?.sessions || {});
  const counts = {};
  for (const session of sessions) counts[session.state || "idle"] = (counts[session.state || "idle"] || 0) + 1;
  host.appendChild(h("div", null,
    h("h1", {}, "Live"),
    h("p", { class: "muted" }, "The panel as the agents see it. Timers tick; the map follows the stream.")));
  host.appendChild(h("div", { class: "row" },
    ORDER.map((state) => chip(state, `${state} · ${counts[state] || 0}`)),
    h("span", { class: "spacer" }),
    button("Flash selection (5 s)", () => {
      if (!ctx.selection.length) return toast("Select lamps on the map first");
      ctx.hardwareTest({ device: ctx.deviceName,
        lamps: ctx.selection.map((lamp) => ({ index: lamp, rgb: [255, 255, 255] })),
        duration_ms: 5000 });
    }, { variant: "ghost" }),
    button("Stop test", () => ctx.stopTest(), { variant: "ghost" })));
  const tests = ctx.tests;
  if (tests.length) {
    host.appendChild(banner("info", `Hardware test: ${tests.map((test) =>
      `${test.device} ${test.label} (${test.expires_in}s)`).join(", ")}`));
  }
}

function renderChecklist(ctx, host, capability) {
  clear(host);
  if (!capability || !ctx.draft) return;
  const layout = resolveLayout(ctx.draft, capability);
  const lamps = lampsOf(layout);
  const identified = lamps.filter((lamp) => lamp.kind === "key" && lamp.source &&
    lamp.source !== "auto" && lamp.source !== "builtin-profile").length;
  const pool = (ctx.draft.devices?.[capability.name]?.lane_pool || defaultLanePool(layout, ctx.count()));
  const sessions = Object.values(ctx.status?.sessions || {});
  const items = [
    { done: true, label: `Device detected — ${capability.name}, ${lamps.length} lamps` },
    { done: identified > 0, label: `Identify keys (${identified}/${lamps.length})`,
      action: ["Identify", () => ctx.go("#/identify")] },
    { done: pool.length > 0, label: `Choose lane lamps (${pool.length})`,
      action: ["Paint", () => ctx.go("#/paint")] },
    { done: sessions.length > 0, label: "Install an integration so agents report",
      action: ["Copy prompt", async () => {
        try {
          const response = await fetch("/files/AGENT_PROMPT.md");
          await navigator.clipboard.writeText(await response.text());
          toast("Agent prompt copied — hand it to any agent");
        } catch {
          ctx.go("#/help");
        }
      }] },
    { done: false, label: "Flash a test on the board",
      action: ["Flash 5 s", () => ctx.hardwareTest({ device: capability.name,
        lamp: pool[0] ?? 0, color: "#ffffff", duration_ms: 5000 })] },
  ];
  const pending = items.filter((item) => !item.done);
  if (pending.length <= 1) return;   // setup is essentially done; get out of the way
  const card = h("div", { class: "card checklist" });
  card.appendChild(h("h3", {}, "Set this board up"));
  for (const item of items) {
    card.appendChild(h("div", { class: `check-item${item.done ? " done" : ""}` },
      h("span", { class: "check-mark", "aria-hidden": "true" }, item.done ? "✓" : "○"),
      h("span", {}, item.label),
      item.action && !item.done
        ? button(item.action[0], item.action[1], { variant: "ghost" }) : null));
  }
  host.appendChild(card);
}

function renderTable(ctx, host, refresh) {
  clear(host);
  const sessions = Object.entries(ctx.status?.sessions || {})
    .map(([id, session]) => ({ id, session }))
    .sort((a, b) => (a.session.slot ?? 0) - (b.session.slot ?? 0));
  const card = h("div", { class: "card" });
  card.appendChild(h("h3", {}, `Lanes (${sessions.length}/${ctx.status?.lanes ?? 0})`));
  if (!sessions.length) {
    card.appendChild(h("div", { class: "empty" },
      h("p", {}, "No agents talking yet."),
      h("div", { class: "row", style: { justifyContent: "center" } },
        button("Open the integration guide", () => ctx.go("#/help")))));
    host.appendChild(card);
    return;
  }
  const table = h("table", { class: "table lane-table" },
    h("thead", null, h("tr", null,
      h("th", { scope: "col" }, "Key"), h("th", { scope: "col" }, "State"),
      h("th", { scope: "col" }, "Agent"), h("th", { scope: "col" }, "Task"),
      h("th", { scope: "col",
        title: `Model context window in use. Amber at ${CONTEXT_WARN_PERCENT}%, red at ${CONTEXT_BAD_PERCENT}%.` },
        "Context"),
      h("th", { scope: "col", title: "Spend reported by the session" }, "Cost"),
      h("th", { scope: "col" }, "For"), h("th", { scope: "col" }, "Actions"))));
  const body = h("tbody");
  for (const { id, session } of sessions) {
    const sessionID = session.sessionID || id;
    const state = session.state || "idle";
    const level = contextLevel(session.info?.context?.percent);
    const row = h("tr", {
      "data-session": sessionID,
      "data-acked": session.acked ? "true" : null,
      class: session.stale ? "stale" : null,
    });
    row.append(
      h("td", { class: "mono" }, session.key || session.slot),
      h("td", { class: "state-cell" }, chip(state, state),
        staleBadge(session), ackedBadge(session)),
      h("td", null, session.ident || session.agent || "—"),
      h("td", { class: "muted" }, session.label || "—"),
      h("td", { class: `num ctx${level ? ` level-${level}` : ""}`,
        title: contextTitle(session.info) }, formatContext(session.info)),
      h("td", { class: "num cost" }, formatCost(session.info)),
      h("td", { class: "num timer", "data-anchor": session.changedMs || "" }, "—"),
      h("td", { class: "row-actions" },
        ackButton(ctx, sessionID, session),
        button("End", async () => {
          await ctx.api("/session/end", { method: "POST",
            body: { sessionID } });
          toast(`Lane ${session.slot} released`);
          ctx.refreshStatus();
        }, { variant: "ghost", title: "Release this lane now" })));
    body.appendChild(row);
  }
  table.appendChild(body);
  card.appendChild(table);
  host.appendChild(card);
  renderTimers(host);
  void refresh;
}

function staleBadge(session) {
  if (!session.stale) return null;
  const seconds = Number(session.stale_s);
  const title = session.stale_s != null && Number.isFinite(seconds)
    ? `No heartbeat for ${formatDuration(seconds)} — the reporter may have died; the state is not changed.`
    : "No heartbeat from the reporter — it may have died; the state is not changed.";
  return h("span", { class: "badge warn stale-badge", title }, "stale");
}

function ackedBadge(session) {
  if (!session.acked) return null;
  return h("span", {
    class: "badge",
    title: "Acknowledged; notifications are suppressed until the next state change.",
  }, "acked");
}

function ackButton(ctx, sessionID, session) {
  const el = button("Ack", async () => {
    el.disabled = true;
    try {
      await ctx.api("/session/ack", {
        method: "POST", body: { sessionID }, timeout: 6000,
      });
      toast(`Lane ${session.slot ?? sessionID} acknowledged`);
      await ctx.refreshStatus();
    } catch (err) {
      toast(err.message, "bad");
    } finally {
      // The status refresh usually rebuilds this row; if it did not, let the
      // button come back after a moment instead of wedging disabled forever.
      setTimeout(() => { if (el.isConnected) el.disabled = false; }, 1200);
    }
  }, {
    variant: "ghost", sm: true,
    title: session.acked
      ? "Already acknowledged; a new state change makes it actionable again"
      : "Mark this lane as seen: done lanes become idle, blocked lanes keep their colour but stop notifying until the next state change",
  });
  if (session.acked) el.disabled = true;
  return el;
}

function contextTitle(info) {
  const context = info?.context || {};
  const parts = [];
  const percent = Number(context.percent);
  if (context.percent != null && context.percent !== "" && Number.isFinite(percent)) {
    parts.push(`${Math.round(percent)}% of the model context window`);
  }
  if (Number.isFinite(Number(context.entries)) && context.entries != null) {
    parts.push(`${Number(context.entries)} entries`);
  }
  if (Number.isFinite(Number(context.compactions)) && context.compactions != null) {
    parts.push(`${Number(context.compactions)} compactions`);
  }
  if (context.limit != null) {
    const limit = compactTokens(context.limit);
    if (limit != null) parts.push(`${limit} token window`);
  }
  if (!parts.length) return "Context use is not reported for this session";
  return `Context: ${parts.join(" · ")}. Amber at ${CONTEXT_WARN_PERCENT}%, red at ${CONTEXT_BAD_PERCENT}%.`;
}

// -- "while you were away" digest -------------------------------------------

function readLastSeen() {
  let raw;
  try { raw = localStorage.getItem(LAST_SEEN_KEY); }
  catch { return null; }
  if (!raw) return null;
  const value = Number(raw);
  if (!Number.isFinite(value) || value <= 0) return null;
  // Tolerate tabs that stored milliseconds: seconds is the wire format.
  return value > 1e12 ? Math.floor(value / 1000) : Math.floor(value);
}

function writeLastSeen(seconds) {
  try { localStorage.setItem(LAST_SEEN_KEY, String(seconds)); }
  catch { /* private mode: the digest is a courtesy */ }
}

async function renderDigest(ctx, host) {
  const now = Math.floor(Date.now() / 1000);
  const lastSeen = readLastSeen();
  if (lastSeen == null) {
    // First visit: set the baseline silently so the next visit has something
    // to compare against, and show nothing this time.
    writeLastSeen(now);
    return;
  }
  let result;
  try {
    result = await ctx.api(`/ui/api/history?since=${lastSeen}`, { timeout: 5000 });
  } catch {
    return;   // older daemon or offline: the digest is a bonus, not a demand
  }
  if (!host.isConnected) return;   // the route changed while we were fetching
  const entries = (Array.isArray(result?.entries) ? result.entries : [])
    .filter((entry) => entry && typeof entry === "object");
  if (!entries.length) return;     // empty history: stay out of the way

  const summary = aggregate(entries, { now });
  const counts = [
    ["working", summary.counts.working], ["done", summary.counts.done],
    ["blocked", summary.counts.blocked], ["error", summary.counts.error],
    ["idle", summary.counts.idle],
  ].filter(([, count]) => count > 0);
  if (!counts.length && summary.cost == null && summary.longest_wait_s == null) return;

  const card = h("section", { class: "card digest", "aria-labelledby": "digest-title" });
  card.appendChild(h("div", { class: "digest-head" },
    h("h3", { id: "digest-title" }, "While you were away"),
    h("span", { class: "muted small" },
      `since ${new Date(lastSeen * 1000).toLocaleString()} · ${summary.total} session${summary.total === 1 ? "" : "s"}`)));
  if (counts.length) {
    card.appendChild(h("div", { class: "row" },
      counts.map(([state, count]) => chip(state, `${state} · ${count}`))));
  }
  card.appendChild(h("p", { class: "muted small" },
    `Total spend ${summary.cost == null ? "—" : formatCost(summary.cost)} · ` +
    `Longest blocked wait ${summary.longest_wait_s == null ? "—" : formatDuration(summary.longest_wait_s)}`));
  card.appendChild(h("div", { class: "row" },
    button("Mark all seen", () => {
      writeLastSeen(Math.floor(Date.now() / 1000));
      clear(host);
      toast("Marked as seen");
    }, { variant: "primary", sm: true })));
  clear(host);
  host.appendChild(card);
}

function renderTimers(host) {
  const now = Date.now();
  for (const cell of host.querySelectorAll(".timer")) {
    const anchor = Number(cell.dataset.anchor);
    if (!anchor) { cell.textContent = "—"; continue; }
    cell.textContent = formatDuration(Math.max(0, (now - anchor) / 1000));
  }
}

function describeLamp(ctx, capability, lampIndex) {
  if (!capability) return null;
  const layout = resolveLayout(ctx.draft, capability);
  const lamps = lampsOf(layout);
  const record = lamps.find((lamp) => lamp.index === lampIndex);
  const zone = zoneOfLamp(layout).get(lampIndex) || "—";
  const look = effectiveLamp(ctx.draft, capability, lampIndex, ctx.status,
    Date.now(), { quiet: ctx.quiet(), zoneOfLamp: (i) => zoneOfLamp(layout).get(i) });
  const session = sessionForLamp(ctx.status, capability, lampIndex);
  const kind = record?.kind || "unknown";
  return h("div", { class: "stack tiny" },
    h("strong", {}, `Lamp ${lampIndex} · ${kind}${zone !== "—" ? ` · ${zone}` : ""}`),
    h("span", {}, `state: ${look.state || look.source}${session ? ` · lane ${session.slot}` : ""}`),
    h("span", {}, `label: ${record?.label || "unnamed"}`),
    h("span", {}, `source: ${record?.source || "unknown"}` +
      (typeof record?.confidence === "number"
        ? ` · confidence ${Math.round(record.confidence * 100)}%` : "")),
    session ? h("span", {}, `${session.ident || session.agent} · ${session.label || ""}`) : null);
}

function hudActions(ctx, count) {
  if (!count) return [];
  return [
    button(`Flash ${count}`, () => {
      ctx.hardwareTest({ device: ctx.deviceName,
        lamps: ctx.selection.map((lamp) => ({ index: lamp, rgb: [255, 255, 255] })),
        duration_ms: 5000 });
    }, { variant: "primary", sm: true }),
    button("Assign as lanes", () => {
      const entry = ctx.draft.devices?.[ctx.deviceName] || {};
      ctx.update(`devices.${ctx.deviceName}.lane_pool`,
        [...new Set([...(entry.lane_pool || []), ...ctx.selection])], "lane pool");
      toast("Added to the lane pool");
    }, { sm: true }),
    button("Identify", () => ctx.go("#/identify"), { sm: true }),
    button("Clear", () => { ctx.setSelection([]); ctx.refresh(); }, { variant: "ghost", sm: true }),
  ];
}
