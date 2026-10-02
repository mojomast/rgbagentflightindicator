// rgbafi web UI bootstrap: auth, store, router, draft lifecycle, palette.
//
// The draft/Apply model is unchanged: every control edits a browser draft;
// hardware is only touched by Apply or an explicit time-boxed overlay. What is
// new in 0.7 is that the draft is persisted, the dirty count is real, and the
// map is the centerpiece every live route is built around.

import { h, clear } from "./lib/dom.js";
import { createStore } from "./lib/store.js";
import { api, clearToken, getToken, setToken } from "./lib/api.js";
import { createEventStream } from "./lib/sse.js";
import { createHistory, deepClone, getPath, setPath } from "./lib/commands.js";
import { diffCount, diffSections } from "./lib/diff.js";
import { toast, dialog, confirmDialog, button, field, textInput, banner } from "./components/ui.js";
import { openPalette } from "./components/palette.js";
import { formatDuration } from "./lib/effective.js";
import * as live from "./views/live.js";
import * as layoutView from "./views/layoutmode.js";
import * as paint from "./views/paint.js";
import * as identify from "./views/identify.js";
import * as mapping from "./views/mapping.js";
import * as appearance from "./views/appearance.js";
import * as devices from "./views/devices.js";
import * as agents from "./views/agents.js";
import * as settings from "./views/settings.js";
import * as logs from "./views/logs.js";
import * as help from "./views/help.js";

const ROUTES = {
  "": { view: live, title: "Live" },
  "layout": { view: layoutView, title: "Layout" },
  "paint": { view: paint, title: "Paint" },
  "identify": { view: identify, title: "Identify" },
  "mapping": { view: mapping, title: "Mapping" },
  "appearance": { view: appearance, title: "Appearance" },
  "devices": { view: devices, title: "Devices" },
  "agents": { view: agents, title: "Agents" },
  "settings": { view: settings, title: "Settings" },
  "logs": { view: logs, title: "Logs" },
  "help": { view: help, title: "Help" },
};

const RAIL = [
  ["Live", [["", "#/", "Live"]]],
  ["Configure", [
    ["layout", "#/layout", "Layout"],
    ["paint", "#/paint", "Paint"],
    ["mapping", "#/mapping", "Mapping"],
  ]],
  ["Find keys", [["identify", "#/identify", "Identify"]]],
  ["Look", [["appearance", "#/appearance", "States"]]],
  ["System", [
    ["devices", "#/devices", "Devices"],
    ["agents", "#/agents", "Agents"],
    ["settings", "#/settings", "Settings"],
    ["logs", "#/logs", "Logs"],
    ["help", "#/help", "Help"],
  ]],
];

const DRAFT_KEY = "rgi.draft";

const store = createStore({
  status: null,
  config: null,
  draft: null,
  capabilities: [],
  profiles: [],
  revision: 0,
  dirty: 0,
  connection: "connecting",
  route: "",
  params: new URLSearchParams(),
  restart: [],
  warnings: [],
  health: null,
  device: null,
  selection: [],
  activeLamp: null,
  tests: [],
});
let history = createHistory();
let routeSubs = new Set();
let tickingTimer = null;

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
    const [status, config, health, profiles] = await Promise.all([
      api("/ui/api/status", { timeout: 5000 }),
      api("/ui/api/config"),
      api("/ui/api/health"),
      api("/ui/api/profiles").catch(() => ({ profiles: [] })),
    ]);
    const current = store.get();
    const stored = replaceDraft ? null : readStoredDraft(config.revision);
    const draft = stored ? stored.draft
      : (!replaceDraft && current.draft && current.dirty ? current.draft
        : deepClone(config.config));
    store.set({
      status, config: config.config, capabilities: config.capabilities,
      profiles: profiles.profiles || [], revision: config.revision,
      health, draft,
      dirty: stored ? diffCount(config.config, stored.draft)
        : (!replaceDraft && current.dirty ? current.dirty : 0),
      connection: "live",
      device: current.device || pickDevice(config.capabilities),
    });
    applyTheme();
    if (stored && diffCount(config.config, stored.draft) > 0) {
      offerStoredDraft(stored);
    }
  } catch (err) {
    if (err.status === 401) { tokenDialog(); return; }
    store.set({ connection: "offline" });
    toast(err.message, "bad");
  }
}

function pickDevice(capabilities) {
  for (const cap of capabilities || []) {
    if (cap.name !== "dummy" && cap.lamps && cap.lamps.length) return cap.name;
  }
  return (capabilities && capabilities[0] && capabilities[0].name) || null;
}

async function refreshStatus() {
  try {
    const status = await api("/ui/api/status", { timeout: 4000 });
    store.set({ status: withAnchors(status), connection: "live" });
  } catch { /* the connection chip reports it */ }
}

async function refreshConfig({ replaceDraft = false } = {}) {
  try {
    const config = await api("/ui/api/config");
    const current = store.get();
    store.set({
      config: config.config, capabilities: config.capabilities,
      revision: config.revision,
      device: current.device || pickDevice(config.capabilities),
      draft: replaceDraft || !current.dirty ? deepClone(config.config) : current.draft,
      dirty: replaceDraft ? 0 : current.dirty,
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
      status: withAnchors(snapshot),
      revision: typeof snapshot.revision === "number" ? snapshot.revision : s.revision,
      tests: Array.isArray(snapshot.tests) ? snapshot.tests : [],
      connection: "live",
    }));
  },
  onState: (connection) => store.set({ connection }),
});

/** Wall-clock anchors for ticking timers; the server sends changed_at/idle_at. */
function withAnchors(status) {
  if (!status || !status.sessions) return status;
  for (const session of Object.values(status.sessions)) {
    session.changedMs = session.changed_at ? session.changed_at * 1000 : null;
    session.idleMs = session.idle_at ? session.idle_at * 1000 : null;
  }
  return status;
}

// one shared second ticker: timers are computed from anchors, never frozen
tickingTimer = setInterval(() => {
  if (store.get().status) store.set({});
}, 1000);
void tickingTimer;

// ---------------------------------------------------------------------------
// draft lifecycle
// ---------------------------------------------------------------------------
function commitDraft(next, label, key) {
  history.push(deepClone(store.get().draft), label, key);
  store.set({ draft: next, dirty: diffCount(store.get().config, next) });
  scheduleDraftSave();
}

function undo() {
  const snapshot = history.undo(deepClone(store.get().draft));
  if (!snapshot) return toast("Nothing to undo");
  store.set({ draft: snapshot, dirty: diffCount(store.get().config, snapshot) });
  scheduleDraftSave();
  renderRoute();
}

function redo() {
  const snapshot = history.redo(deepClone(store.get().draft));
  if (!snapshot) return toast("Nothing to redo");
  store.set({ draft: snapshot, dirty: diffCount(store.get().config, snapshot) });
  scheduleDraftSave();
  renderRoute();
}

async function discardDraft() {
  if (!await confirmDialog("Discard changes?",
      "The draft goes back to the last applied config.")) return;
  history = createHistory();
  store.set({ draft: deepClone(store.get().config), dirty: 0 });
  clearStoredDraft();
  renderRoute();
}

async function putConfig(draft, ifMatch) {
  return api("/ui/api/config", {
    method: "PUT", body: draft, timeout: 15000,
    headers: ifMatch ? { "If-Match": String(ifMatch) } : {},
  });
}

async function applyDraft(overwrite = false) {
  const s = store.get();
  if (!s.draft || !s.dirty) return;
  const sections = diffSections(s.config, s.draft);
  const proceed = await dialog({
    title: `Apply ${s.dirty} change${s.dirty === 1 ? "" : "s"}?`,
    body: h("div", null,
      h("p", { class: "muted" }, "The panel repaints on the next tick. " +
        "The previous file stays in the backup chain."),
      h("ul", null, sections.map((section) => h("li", null,
        `${section.label}: ${section.count} field${section.count === 1 ? "" : "s"}`)))),
    actions: [
      { label: "Cancel", value: null },
      { label: "Apply to board", value: "apply", primary: true },
    ],
  });
  if (proceed !== "apply") return;
  try {
    const result = await putConfig(s.draft, overwrite ? null : s.revision);
    history = createHistory();
    clearStoredDraft();
    store.set({
      config: deepClone(s.draft), revision: result.revision, dirty: 0,
      restart: result.restart_required || [], warnings: result.warnings || [],
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

// -- draft persistence ------------------------------------------------------
let draftSaveTimer = null;
function scheduleDraftSave() {
  clearTimeout(draftSaveTimer);
  draftSaveTimer = setTimeout(saveDraftNow, 400);
}
function saveDraftNow() {
  const s = store.get();
  if (!s.draft || !s.dirty) return;
  try {
    sessionStorage.setItem(DRAFT_KEY, JSON.stringify({
      revision: s.revision, at: Date.now(), dirty: s.dirty, draft: s.draft,
    }));
  } catch { /* storage full or private mode: drafts are a courtesy */ }
}
function readStoredDraft(revision) {
  try {
    const raw = sessionStorage.getItem(DRAFT_KEY);
    if (!raw) return null;
    const stored = JSON.parse(raw);
    if (!stored || stored.revision !== revision || !stored.draft) return null;
    return stored;
  } catch {
    return null;
  }
}
function clearStoredDraft() {
  try { sessionStorage.removeItem(DRAFT_KEY); } catch { /* ignore */ }
}
function offerStoredDraft(stored) {
  const when = new Date(stored.at).toLocaleTimeString();
  const el = banner("info",
    `Restored an unsaved draft from ${when} (${stored.dirty} change${stored.dirty === 1 ? "" : "s"}).`,
    []);
  el.querySelector("div").appendChild(h("div", { class: "row", style: { marginTop: "6px" } },
    button("Keep it", () => el.remove(), { variant: "primary" }),
    button("Discard it", async () => {
      clearStoredDraft();
      store.set({ draft: deepClone(store.get().config), dirty: 0 });
      el.remove();
      renderRoute();
    })));
  document.getElementById("main")?.prepend(el);
}
window.addEventListener("beforeunload", () => {
  if (store.get().dirty) saveDraftNow();
});

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
      body: payload || { device: cap.name, lamp: cap.pool?.[0] ?? 0,
                         color: "#ffffff", duration_ms: 5000 },
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

async function paintFrame(payload) {
  return api("/ui/api/paint", { method: "POST", body: payload });
}

// ---------------------------------------------------------------------------
// profiles
// ---------------------------------------------------------------------------
async function useProfile(profileId, capability) {
  try {
    const profile = await api(`/ui/api/profiles/${encodeURIComponent(profileId)}`);
    const layout = profile.layout;
    if (!layout || !layout.keys) { toast("That profile has no layout", "bad"); return; }
    const id = `${capability.name}-${profileId}`;
    ctx.replaceDraft((draft) => {
      draft.layouts = draft.layouts || {};
      draft.layouts[id] = deepClone(layout);
      draft.devices = draft.devices || {};
      draft.devices[capability.name] = {
        ...(draft.devices[capability.name] || {}),
        layout: id,
        profile: profileId,
      };
      draft.profiles = draft.profiles || {};
      draft.profiles[profileId] = { ...(draft.profiles[profileId] || {}), enabled: true };
      return draft;
    }, `use profile ${profileId}`);
    toast(`Loaded ${profile.label || profileId} geometry into the draft`);
    renderRoute();
  } catch (err) {
    toast(err.message, "bad");
  }
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

function go(hash) {
  location.hash = hash.startsWith("#") ? hash : `#/${hash.replace(/^\//, "")}`;
}

function renderRoute() {
  const { route, params } = currentRoute();
  routeSubs = new Set();
  const main = document.getElementById("main");
  clear(main);
  store.set({ route, params: params,
              selection: parseSelection(params.get("sel")) });
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
  renderLaneStrip();
}

function parseSelection(value) {
  if (!value) return store.get().selection;
  return value.split(",").map(Number).filter((n) => Number.isFinite(n));
}

function renderChrome() {
  const s = store.get();
  const { route } = currentRoute();

  const rail = document.getElementById("rail");
  clear(rail);
  for (const [group, items] of RAIL) {
    rail.appendChild(h("div", { class: "rail-head" }, group));
    for (const [id, href, label] of items) {
      rail.appendChild(h("a", { href, "aria-current": id === route ? "page" : null },
        label));
    }
  }

  const crumbs = document.getElementById("crumbs");
  clear(crumbs);
  crumbs.appendChild(h("a", { href: "#/" }, "rgbafi"));
  if (ROUTES[route].title !== "Live") {
    crumbs.appendChild(document.createTextNode(` / ${ROUTES[route].title}`));
  }
  if (s.capabilities.length > 1) {
    crumbs.appendChild(document.createTextNode(" · "));
    const picker = h("select", {
      class: "select device-picker", "aria-label": "Device",
      onchange: (event) => { store.set({ device: event.target.value, selection: [] }); renderRoute(); },
    });
    for (const cap of s.capabilities) {
      picker.appendChild(h("option", { value: cap.name, selected: cap.name === s.device },
        cap.label || cap.name));
    }
    crumbs.appendChild(picker);
  }
  updateChrome();
}

function updateChrome() {
  const s = store.get();
  const dirtybar = document.getElementById("dirtybar");
  dirtybar.hidden = !s.dirty;
  if (s.dirty) {
    document.getElementById("dirty-count").textContent =
      `${s.dirty} unsaved change${s.dirty === 1 ? "" : "s"}`;
  }
  document.getElementById("btn-apply").disabled = !s.dirty;
  document.getElementById("btn-discard").disabled = !s.dirty;
  document.getElementById("btn-undo").disabled = !history.canUndo();
  document.getElementById("btn-redo").disabled = !history.canRedo();
  const conn = document.getElementById("conn");
  const labels = { live: "live", polling: "live (polling)", offline: "offline",
                   connecting: "connecting", reconnecting: "reconnecting" };
  conn.dataset.state = s.connection;
  conn.textContent = labels[s.connection] || s.connection;
  conn.title = s.connection === "polling"
    ? "stream unavailable; polling every 2 s" : "";
}

function renderLaneStrip() {
  const host = document.getElementById("lanestrip");
  if (!host) return;
  const s = store.get();
  const sessions = Object.values(s.status?.sessions || {}).sort((a, b) => a.slot - b.slot);
  clear(host);
  if (!sessions.length) {
    host.appendChild(h("span", { class: "muted small" }, "No agents talking yet."));
    return;
  }
  const now = Date.now();
  for (const session of sessions) {
    const anchor = session.changedMs ?? (session.idleMs ?? now);
    const elapsed = anchor ? Math.max(0, (now - anchor) / 1000) : null;
    host.appendChild(h("button", {
      class: "lane-pill", "data-state": session.state || "idle", type: "button",
      title: `${session.label || ""} ${session.ident ? `· ${session.ident}` : ""}`,
      onclick: () => { go(`#/mapping?lane=${session.slot}`); },
    },
      h("span", { class: "dot", "aria-hidden": "true" }),
      h("span", { class: "mono" }, session.key || session.slot),
      h("span", { class: "lane-state" }, session.state || "idle"),
      elapsed != null ? h("span", { class: "lane-time" }, formatDuration(elapsed)) : null));
  }
}

store.subscribe(() => {
  updateChrome();
  renderLaneStrip();
  for (const fn of routeSubs) fn(store.get());
});

// ---------------------------------------------------------------------------
// token dialog
// ---------------------------------------------------------------------------
async function tokenDialog() {
  const input = textInput(getToken(), () => {}, { type: "password", ariaLabel: "Panel token" });
  const result = await dialog({
    title: "Panel token",
    body: h("div", null,
      h("p", {}, "Paste the token from ", h("code", {}, "~/.config/rgi/token"),
        ", or open this page with ", h("code", {}, "rgi ui"), "."),
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
  const cap = currentCapability();
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
    paint: paintFrame,
    useProfile,
    subscribe(fn) { routeSubs.add(fn); return () => routeSubs.delete(fn); },
    get status() { return store.get().status; },
    get config() { return store.get().config; },
    get draft() { return store.get().draft; },
    get capabilities() { return store.get().capabilities; },
    get profiles() { return store.get().profiles; },
    get tests() { return store.get().tests; },
    get revision() { return store.get().revision; },
    get restart() { return store.get().restart; },
    get health() { return store.get().health; },
    get connection() { return store.get().connection; },
    get params() { return store.get().params; },
    get route() { return store.get().route; },
    get deviceName() { return cap ? cap.name : null; },
    get device() { return cap; },
    get selection() { return store.get().selection; },
    get activeLamp() { return store.get().activeLamp; },
    setActiveLamp(lamp) { store.set({ activeLamp: lamp }); },
    setSelection(list) {
      const unique = [...new Set(list)];
      store.set({ selection: unique });
      const { route } = currentRoute();
      const params = store.get().params;
      if (unique.length) params.set("sel", unique.join(","));
      else params.delete("sel");
      window.history.replaceState(null, "", `#/${route}${params.toString() ? `?${params}` : ""}`);
    },
    capability(name) { return store.get().capabilities.find((c) => c.name === name) || null; },
    count() { return store.get().draft?.settings?.count || 12; },
    quiet() { return store.get().draft?.settings?.quiet !== false; },
    reduceMotion() {
      return store.get().draft?.settings?.reduce_motion === true ||
        window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    },
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
    openPalette: () => openPalette(ctx()),
  };
}

function applyTheme() {
  const draft = store.get().draft;
  if (!draft) return;
  const root = document.documentElement;
  for (const [name, spec] of Object.entries(draft.appearance?.states || {})) {
    if (spec.color) root.style.setProperty(`--state-${name}`, spec.color);
  }
  root.dataset.reduceMotion = ctx().reduceMotion() ? "true" : "";
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
  if (mod && event.key.toLowerCase() === "k") {
    event.preventDefault();
    ctx().openPalette();
  } else if (mod && event.key.toLowerCase() === "z") {
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
document.getElementById("btn-palette").addEventListener("click", () => ctx().openPalette());
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
