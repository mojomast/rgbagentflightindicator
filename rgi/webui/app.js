// rgbafi web UI bootstrap: auth, store, router, chrome, draft lifecycle.
//
// The draft/Apply model is the heart of the safety story: every control edits
// a browser draft; hardware is only touched by Apply (which PUTs a validated
// revision) or by an explicit, time-boxed test overlay.

import { h, clear } from "./lib/dom.js";
import { createStore } from "./lib/store.js";
import { api, clearToken, getToken, setToken } from "./lib/api.js";
import { createEventStream } from "./lib/sse.js";
import { createHistory, deepClone, getPath, setPath } from "./lib/commands.js";
import { renderDiagram, sessionsBySlot, stateForSlot } from "./components/diagram.js";
import { toast, dialog, confirmDialog, button, field, textInput, banner } from "./components/ui.js";
import { layoutFor } from "./lib/layout.js";
import * as dashboard from "./views/dashboard.js";
import * as devices from "./views/devices.js";
import * as layoutView from "./views/layout.js";
import * as appearance from "./views/appearance.js";
import * as mapping from "./views/mapping.js";
import * as wizard from "./views/wizard.js";
import * as agents from "./views/agents.js";
import * as settings from "./views/settings.js";
import * as logs from "./views/logs.js";
import * as help from "./views/help.js";

const ROUTES = {
  "": { view: dashboard, title: "Dashboard" },
  "devices": { view: devices, title: "Devices" },
  "layout": { view: layoutView, title: "Layout" },
  "appearance": { view: appearance, title: "Appearance" },
  "mapping": { view: mapping, title: "Mapping" },
  "wizard": { view: wizard, title: "Mapping wizard" },
  "agents": { view: agents, title: "Agents" },
  "settings": { view: settings, title: "Settings" },
  "logs": { view: logs, title: "Logs" },
  "help": { view: help, title: "Help" },
};

const RAIL = [
  ["Live", [["dashboard", "#/", "Dashboard"]]],
  ["Configure", [
    ["devices", "#/devices", "Devices"],
    ["layout", "#/layout", "Layout"],
    ["appearance", "#/appearance", "Appearance"],
    ["mapping", "#/mapping", "Mapping"],
  ]],
  ["Find keys", [["wizard", "#/wizard", "Mapping wizard"]]],
  ["Agents", [["agents", "#/agents", "Agents"]]],
  ["System", [
    ["settings", "#/settings", "Settings"],
    ["logs", "#/logs", "Logs"],
    ["help", "#/help", "Help"],
  ]],
];

const store = createStore({
  status: null,
  config: null,
  draft: null,
  capabilities: [],
  revision: 0,
  dirty: false,
  connection: "connecting",
  route: "",
  restart: [],
  warnings: [],
  health: null,
  device: null,
  selection: [],
});
let history = createHistory();
let previewQueued = false;

// ---------------------------------------------------------------------------
// token bootstrap
// ---------------------------------------------------------------------------
(function bootstrapToken() {
  const match = location.hash.match(/^#token=([^&/]+)(?:&(.*))?$/);
  if (!match) return;
  setToken(decodeURIComponent(match[1]));
  const rest = (match[2] || "").replace(/^\/?/, "/");
  window.history.replaceState(null, "", `/ui/#${rest}`);
})();

// ---------------------------------------------------------------------------
// data
// ---------------------------------------------------------------------------
async function loadAll({ replaceDraft = false } = {}) {
  try {
    const [status, config, health] = await Promise.all([
      api("/ui/api/status", { timeout: 5000 }),
      api("/ui/api/config"),
      api("/ui/api/health"),
    ]);
    const current = store.get();
    const draft = !replaceDraft && current.draft && current.dirty
      ? current.draft
      : deepClone(config.config);
    store.set({
      status, config: config.config, capabilities: config.capabilities,
      revision: config.revision, health, draft,
      dirty: !replaceDraft && current.dirty,
      connection: "live",
      device: current.device || config.capabilities[0]?.name || null,
    });
    applyTheme();
  } catch (err) {
    if (err.status === 401) { tokenDialog(); return; }
    store.set({ connection: "offline" });
    toast(err.message, "bad");
  }
}

async function refreshStatus() {
  try {
    store.set({ status: await api("/ui/api/status", { timeout: 4000 }), connection: "live" });
  } catch { /* the connection chip reports it */ }
}

async function refreshConfig({ replaceDraft = false } = {}) {
  try {
    const config = await api("/ui/api/config");
    const current = store.get();
    store.set({
      config: config.config, capabilities: config.capabilities, revision: config.revision,
      device: current.device || config.capabilities[0]?.name || null,
      draft: replaceDraft || !current.dirty ? deepClone(config.config) : current.draft,
      dirty: replaceDraft ? false : current.dirty,
    });
    applyTheme();
  } catch (err) {
    if (err.status === 401) tokenDialog();
  }
}

createEventStream({
  onSnapshot: (snapshot) => {
    store.set((s) => ({
      ...s,
      status: snapshot,
      revision: typeof snapshot.revision === "number" ? snapshot.revision : s.revision,
      connection: "live",
    }));
  },
  onState: (connection) => store.set({ connection }),
});

// ---------------------------------------------------------------------------
// draft lifecycle
// ---------------------------------------------------------------------------
function commitDraft(next, label, key) {
  // history stores the state *before* the edit, so undo is just a pop
  history.push(deepClone(store.get().draft), label, key);
  store.set({ draft: next, dirty: true });
}

function recomputeDirty(draft) {
  const config = store.get().config;
  return JSON.stringify(draft) !== JSON.stringify(config);
}

function undo() {
  const s = store.get();
  const snapshot = history.undo(deepClone(s.draft));
  if (!snapshot) return toast("Nothing to undo");
  store.set({ draft: snapshot, dirty: recomputeDirty(snapshot) });
  renderRoute();
}

function redo() {
  const s = store.get();
  const snapshot = history.redo(deepClone(s.draft));
  if (!snapshot) return toast("Nothing to redo");
  store.set({ draft: snapshot, dirty: recomputeDirty(snapshot) });
  renderRoute();
}

async function discardDraft() {
  if (!await confirmDialog("Discard changes?", "The draft goes back to the last applied config.")) return;
  history = createHistory();
  store.set({ draft: deepClone(store.get().config), dirty: false });
  renderRoute();
}

async function putConfig(draft, ifMatch) {
  return api("/ui/api/config", {
    method: "PUT",
    body: draft,
    headers: ifMatch ? { "If-Match": String(ifMatch) } : {},
    timeout: 15000,
  });
}

async function applyDraft(overwrite = false) {
  const s = store.get();
  if (!s.draft) return;
  try {
    const result = await putConfig(s.draft, overwrite ? null : s.revision);
    history = createHistory();
    store.set({
      config: deepClone(s.draft),
      revision: result.revision,
      dirty: false,
      restart: result.restart_required || [],
      warnings: result.warnings || [],
    });
    toast(`Applied revision ${result.revision}` +
      ((result.restart_required || []).length
        ? ` — restart for: ${result.restart_required.join(", ")}` : ""));
    renderRoute();
  } catch (err) {
    if (err.code === "revision_conflict") {
      const choice = await dialog({
        title: "Another tab changed the config",
        body: h("p", {}, err.message),
        actions: [
          { label: "Reload from panel", value: "reload" },
          { label: "Overwrite", value: "overwrite" },
          { label: "Cancel", value: null },
        ],
      });
      if (choice === "reload") await refreshConfig({ replaceDraft: true });
      if (choice === "overwrite") await applyDraft(true);
      return;
    }
    if (err.status === 401) { tokenDialog(); return; }
    if (err.details?.length) {
      await dialog({
        title: `${err.details.length} field(s) need attention`,
        body: h("ul", null, err.details.slice(0, 12).map((item) =>
          h("li", null, h("span", { class: "mono" }, item.path || ""), ` — ${item.message}`))),
      });
      return;
    }
    toast(err.message, "bad");
  }
}

// ---------------------------------------------------------------------------
// hardware tests
// ---------------------------------------------------------------------------
function currentCapability() {
  const s = store.get();
  return s.capabilities.find((cap) => cap.name === s.device) || s.capabilities[0] || null;
}

async function hardwareTest(payload) {
  const cap = currentCapability();
  if (!cap) { toast("No device detected", "bad"); return; }
  try {
    const result = await api("/ui/api/test", {
      method: "POST",
      body: payload || {
        device: cap.name,
        lamp: (cap.pool && cap.pool[0]) ?? 0,
        color: "#ffffff",
        duration_ms: 15000,
      },
    });
    toast(`Testing ${result.device} for ${Math.round(result.duration_s)} s`);
  } catch (err) {
    toast(err.message, "bad");
  }
}

async function stopTest() {
  try { await api("/ui/api/test", { method: "DELETE" }); }
  catch { /* already off */ }
}

// ---------------------------------------------------------------------------
// router and chrome
// ---------------------------------------------------------------------------
function currentRoute() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [pathPart, query] = raw.split("?");
  const params = new URLSearchParams(query || "");
  const route = pathPart.split("/")[0] || "";
  return ROUTES[route] ? { route, params } : { route: "", params };
}

function go(hash) { location.hash = hash.startsWith("#") ? hash : `#/${hash}`; }

function renderRoute() {
  const { route } = currentRoute();
  store.set({ route });
  const main = document.getElementById("main");
  clear(main);
  if (!store.get().draft) {
    main.appendChild(h("div", { class: "empty" }, "Connecting to the panel…"));
    renderChrome();
    return;
  }
  try {
    main.appendChild(ROUTES[route].view.render(ctx()));
  } catch (err) {
    main.appendChild(h("div", { class: "stack" },
      h("h1", {}, "This page hit an error"),
      banner("bad", err.message || String(err)),
      h("pre", { class: "mono", style: { whiteSpace: "pre-wrap" } }, err.stack || "")));
  }
  renderChrome();
  window.scrollTo(0, 0);
}

function renderChrome() {
  const s = store.get();
  const { route } = currentRoute();

  const rail = document.getElementById("rail");
  clear(rail);
  for (const [group, items] of RAIL) {
    rail.appendChild(h("div", { class: "rail-head" }, group));
    for (const [id, href, label] of items) {
      rail.appendChild(h("a", {
        href,
        "aria-current": id === route ? "page" : null,
      }, label));
    }
  }

  const crumbs = document.getElementById("crumbs");
  clear(crumbs);
  crumbs.appendChild(h("a", { href: "#/" }, "rgbafi"));
  if (ROUTES[route].title !== "Dashboard") {
    crumbs.appendChild(document.createTextNode(` / ${ROUTES[route].title}`));
  }
  if (s.capabilities.length > 1) {
    crumbs.appendChild(document.createTextNode(" · "));
    const picker = h("select", {
      class: "select", "aria-label": "Device",
      style: { width: "auto", display: "inline-block", minHeight: "24px", padding: "0 4px" },
      onchange: (event) => { store.set({ device: event.target.value, selection: [] }); renderRoute(); },
    });
    for (const cap of s.capabilities) {
      picker.appendChild(h("option", { value: cap.name, selected: cap.name === s.device }, cap.label));
    }
    crumbs.appendChild(picker);
  }
}

// ---------------------------------------------------------------------------
// preview strip
// ---------------------------------------------------------------------------
function applyTheme() {
  const draft = store.get().draft;
  const root = document.documentElement;
  if (!draft) return;
  for (const [name, spec] of Object.entries(draft.appearance?.states || {})) {
    if (spec.color) root.style.setProperty(`--state-${name}`, spec.color);
  }
  const reduce = draft.settings?.reduce_motion === true;
  root.dataset.reduceMotion = reduce ? "true" : "";
}

function renderPreview() {
  const host = document.getElementById("preview");
  clear(host);
  const s = store.get();
  const cap = currentCapability();
  host.appendChild(h("h3", {}, "Live preview"));
  if (!cap || !s.draft) {
    host.appendChild(h("p", { class: "muted" }, "No device."));
    return;
  }
  const layout = layoutFor(s.draft, cap);
  const bySlot = sessionsBySlot(s.status);
  const overrides = new Map();
  for (const override of s.draft.lamp_overrides || []) {
    if (override.device === cap.name) overrides.set(override.lamp, override.color);
  }
  host.appendChild(renderDiagram({
    layout,
    stateFor: (lamp) => {
      const base = stateForSlot(bySlot.get((cap.pool || []).indexOf(lamp)));
      const colour = overrides.get(lamp);
      if (colour) return { state: "override", override: colour };
      return base;
    },
    laneSet: new Set(s.draft.devices?.[cap.name]?.lane_pool || []),
    quiet: s.draft.settings?.quiet !== false,
    blinkPeriod: s.draft.appearance?.blink?.period_ms || 560,
  }));
  const legend = h("div", { class: "row", style: { marginTop: "8px" } });
  for (const state of ["working", "done", "blocked", "idle"]) {
    const spec = s.draft.appearance?.states?.[state] || {};
    legend.appendChild(h("span", { class: "chip" },
      h("span", { class: "dot", "aria-hidden": "true", style: { background: spec.color || "#333" } }),
      state));
  }
  host.appendChild(legend);
  const tests = (s.status?.tests || []).map((test) => `${test.device}: ${test.label} (${test.expires_in}s)`);
  if (tests.length) host.appendChild(banner("info", `Hardware test running — ${tests.join(", ")}`));
  host.appendChild(button("Stop test", stopTest, { variant: "ghost" }));
}

store.subscribe(() => {
  if (previewQueued) return;
  previewQueued = true;
  requestAnimationFrame(() => { previewQueued = false; renderPreview(); });
  const s = store.get();
  document.getElementById("dirtybar").hidden = !s.dirty;
  const conn = document.getElementById("conn");
  const labels = { live: "live", polling: "polling", offline: "offline", connecting: "connecting", reconnecting: "reconnecting" };
  conn.dataset.state = s.connection;
  conn.textContent = labels[s.connection] || s.connection;
});

// ---------------------------------------------------------------------------
// token dialog
// ---------------------------------------------------------------------------
async function tokenDialog() {
  const input = textInput(getToken(), () => {}, { type: "password", ariaLabel: "Panel token" });
  const result = await dialog({
    title: "Panel token",
    body: h("div", null,
      h("p", {}, "Paste the token from ", h("code", {}, "~/.config/rgi/token"), ", or open this page with ", h("code", {}, "rgi ui"), "."),
      field("X-LED-Token", input)),
    actions: [
      { label: "Forget token", value: "forget" },
      { label: "Use token", value: "save", primary: true },
    ],
  });
  if (result === "forget") { clearToken(); return; }
  if (result !== "save") return;
  setToken(input.value.trim());
  await loadAll({ replaceDraft: true });
  renderRoute();
}

// ---------------------------------------------------------------------------
// ctx handed to views
// ---------------------------------------------------------------------------
function ctx() {
  return {
    store,
    api,
    toast,
    dialog,
    confirm: confirmDialog,
    go,
    refresh: renderRoute,
    refreshStatus,
    refreshConfig,
    get status() { return store.get().status; },
    get config() { return store.get().config; },
    get draft() { return store.get().draft; },
    get capabilities() { return store.get().capabilities; },
    get revision() { return store.get().revision; },
    get restart() { return store.get().restart; },
    get health() { return store.get().health; },
    get connection() { return store.get().connection; },
    get deviceName() { return currentCapability()?.name || null; },
    get selection() { return store.get().selection; },
    setSelection(list) { store.set({ selection: [...new Set(list)] }); },
    capability(name) { return store.get().capabilities.find((cap) => cap.name === name) || null; },
    count() { return store.get().draft?.settings?.count || 12; },
    quiet() { return store.get().draft?.settings?.quiet !== false; },
    get(path) { return getPath(store.get().draft, path); },
    update(path, value, label, key) {
      const next = deepClone(store.get().draft);
      setPath(next, path, value);
      commitDraft(next, label || path, key);
    },
    replaceDraft(fn, label, key) {
      const next = deepClone(store.get().draft);
      const result = fn(next) || next;
      commitDraft(result, label || "edit", key);
    },
    apply: applyDraft,
    discard: discardDraft,
    undo,
    redo,
    hardwareTest,
    stopTest,
    test: hardwareTest,
  };
}

// ---------------------------------------------------------------------------
// global wiring
// ---------------------------------------------------------------------------
function isTyping(event) {
  const target = event.target;
  return target && (target.tagName === "INPUT" || target.tagName === "SELECT" ||
    target.tagName === "TEXTAREA" || target.isContentEditable);
}

document.addEventListener("keydown", (event) => {
  const mod = event.ctrlKey || event.metaKey;
  if (mod && event.key.toLowerCase() === "z") {
    event.preventDefault();
    if (event.shiftKey) redo(); else undo();
  } else if (mod && event.key.toLowerCase() === "s") {
    event.preventDefault();
    applyDraft();
  } else if (event.key === "?" && !isTyping(event)) {
    go("#/help");
  }
});
document.getElementById("btn-apply").addEventListener("click", () => applyDraft());
document.getElementById("btn-discard").addEventListener("click", discardDraft);
document.getElementById("btn-undo").addEventListener("click", undo);
document.getElementById("btn-redo").addEventListener("click", redo);
document.getElementById("btn-token").addEventListener("click", tokenDialog);
document.getElementById("app-version").textContent = "web";
window.addEventListener("hashchange", renderRoute);
window.addEventListener("error", (event) => {
  console.error(event.error || event.message);
  toast(`UI error: ${event.message}`, "bad");
});
window.addEventListener("unhandledrejection", (event) => {
  console.error(event.reason);
});

renderRoute();
loadAll({ replaceDraft: true }).then(() => {
  renderRoute();
  const health = store.get().health;
  if (health?.version) document.getElementById("app-version").textContent = `v${health.version}`;
  if (!getToken() && store.get().connection !== "live") tokenDialog();
});
