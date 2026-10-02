// Mapping wizard. Two honest paths to lamp -> key:
//
//   manual: light one lamp, press the lit key; event.code names the key itself.
//   webcam: locate where each lamp appears (differential frames), then still
//           name the keys by pressing them — a camera can see light, not names.
//
// One writer only: every probe goes through /ui/api/paint, consumed by the
// render loop, and a cancel/finish always clears the overlay.

import { h, clear } from "../lib/dom.js";
import { autoLayout, codeLabel, defaultLanePool, keysByLamp, layoutFor, download, toCsv } from "../lib/layout.js";
import { WebcamMapper } from "../lib/webcam.js";
import { banner, button, captureKey, field, selectInput, toast } from "../components/ui.js";

const SCOPES = [
  { value: "pool", label: "Lane pool only (fast)" },
  { value: "unnamed", label: "Lamps without a key name yet" },
  { value: "selected", label: "Selected lamps (Layout page)" },
  { value: "all", label: "All lamps (slow but thorough)" },
];

export function render(ctx) {
  const { draft, capabilities } = ctx;
  const root = h("div", { class: "stack" });
  root.appendChild(h("h1", {}, "Mapping wizard"));
  root.appendChild(h("p", { class: "muted" },
    "Find out which physical key each lamp is. Start with the manual walk — it needs no camera and names keys by pressing them."));

  if (!capabilities.length) {
    root.appendChild(h("div", { class: "empty" }, "No device detected."));
    return root;
  }

  const cap = ctx.capability(ctx.deviceName) || capabilities[0];
  const layout = layoutFor(draft, cap);
  const byLamp = keysByLamp(layout);
  const pool = (draft.devices?.[cap.name]?.lane_pool) || defaultLanePool(layout, ctx.count());

  const scope = h("select", { class: "select", "aria-label": "Which lamps to map" },
    ...SCOPES.map((option) => h("option", { value: option.value }, option.label)));
  const mode = h("select", { class: "select", "aria-label": "Mapping mode" }, 
    h("option", { value: "manual" }, "Manual — press the lit key"),
    h("option", { value: "webcam" }, "Webcam assist — locate lights, then name them"));

  root.appendChild(h("div", { class: "card" },
    h("div", { class: "row" },
      field("Device", h("span", { class: "mono" }, cap.label)),
      field("Scope", scope),
      field("Mode", mode))));

  const work = h("div", { class: "stack" });
  root.appendChild(work);

  const state = {
    running: false,
    lamp: null,
    results: new Map(),
    mapped: new Map(),
  };

  const start = () => {
    const chosen = pickLamps(ctx, cap, layout, pool, scope.value);
    if (!chosen.length) return toast("Nothing to map with this scope");
    state.results.clear();
    state.order = chosen;
    state.at = 0;
    if (mode.value === "manual") startManual(ctx, cap, layout, work, state);
    else startWebcam(ctx, cap, layout, work, state, byLamp);
  };

  root.appendChild(h("div", { class: "toolbar" },
    button("Start", start, { variant: "primary" }),
    button("Cancel / stop", () => {
      state.cancelled = true;
      ctx.stopTest();
      toast("Stopped; the board is back on lane duty");
    }, { variant: "ghost" }),
    button("Save to draft", () => saveResults(ctx, cap, layout, state)),
    button("Export CSV", () => download(`${cap.name}-mapped.csv`, toCsv(resultsToKeys(layout, state.results)), "text/csv"))));

  return root;
}

function pickLamps(ctx, cap, layout, pool, scope) {
  const byLamp = keysByLamp(layout);
  if (scope === "pool") return pool.filter((lamp) => byLamp.has(lamp) || true);
  if (scope === "unnamed") {
    return cap.lamps.map((l) => l.index).filter((lamp) => !byLamp.get(lamp)?.code);
  }
  if (scope === "selected") return [...ctx.selection];
  return cap.lamps.map((l) => l.index);
}

// ---------------------------------------------------------------------------
// manual walk
// ---------------------------------------------------------------------------
function startManual(ctx, cap, layout, host, state) {
  clear(host);
  state.running = true;
  const byLamp = keysByLamp(layout);

  const progressBar = h("span", { style: { width: "0%" } });
  const counter = h("strong", {}, `0 / ${state.order.length}`);
  const lampLine = h("div", { class: "stack" },
    h("div", { class: "mono", style: { fontSize: "var(--text-xl)" } }, "—"));
  const codeLine = h("div", { class: "muted" }, "Press the key that is lit. Esc cancels the capture.");

  const paint = (lamp) => {
    ctx.test({
      device: cap.name,
      lamps: [{ index: lamp, rgb: [255, 255, 255] }],
      duration_ms: 120000,
      label: "wizard",
    });
  };

  const advance = (delta = 1) => {
    state.at += delta;
    if (state.at < 0) state.at = 0;
    if (state.at >= state.order.length) {
      state.running = false;
      ctx.stopTest();
      counter.textContent = `${state.results.size} mapped`;
      lampLine.firstChild.textContent = "Done";
      codeLine.textContent = "Save to draft, then Apply.";
      return;
    }
    const lamp = state.order[state.at];
    lampLine.firstChild.textContent = `Lamp ${lamp}${byLamp.get(lamp)?.label ? ` · ${byLamp.get(lamp).label}` : ""}`;
    progressBar.style.width = `${Math.round((state.at / state.order.length) * 100)}%`;
    counter.textContent = `${state.at + 1} / ${state.order.length}`;
    paint(lamp);
  };

  const assign = (code) => {
    const lamp = state.order[state.at];
    state.results.set(lamp, {
      lamp, code, label: codeLabel(code), source: "press", confidence: 1,
    });
    toast(`Lamp ${lamp} → ${code}`);
    advance(1);
  };

  const capture = () => captureKey(assign);

  const controls = h("div", { class: "row" },
    button("Press the lit key", capture, { variant: "primary" }),
    button("Light it again", () => paint(state.order[state.at])),
    button("Skip", () => advance(1)),
    button("Back", () => advance(-1)),
    button("Stop", () => { state.running = false; ctx.stopTest(); }));

  host.appendChild(h("div", { class: "card stack" },
    h("div", { class: "row" }, counter, h("div", { class: "progress", style: { flex: "1" } }, progressBar)),
    lampLine, codeLine, controls));

  advance(0);
}

// ---------------------------------------------------------------------------
// webcam assist
// ---------------------------------------------------------------------------
async function startWebcam(ctx, cap, layout, host, state, byLamp) {
  clear(host);
  if (!WebcamMapper.supported) {
    host.appendChild(banner("warn", WebcamMapper.reason));
    host.appendChild(button("Use manual mode instead", () => {
      state.at = 0;
      startManual(ctx, cap, layout, host, state);
    }, { variant: "primary" }));
    return;
  }

  const video = h("video", { playsinline: true, muted: true, style: { width: "100%", borderRadius: "8px", background: "#000" } });
  const marker = h("canvas", {
    style: { position: "absolute", inset: "0", width: "100%", height: "100%", pointerEvents: "none" },
  });
  const videoWrap = h("div", { style: { position: "relative" } }, video, marker);
  const status = h("p", { class: "muted" }, "Allow the camera, then start the scan.");
  const exposure = h("p", { class: "muted small" }, "");
  const camera = new WebcamMapper(video);
  state.camera = camera;

  const startCamera = async () => {
    try {
      const lock = await camera.start();
      exposure.textContent = `exposure: ${lock}`;
      marker.width = camera.width;
      marker.height = camera.height;
      status.textContent = "Camera ready. Dim the room if you can.";
    } catch (err) {
      status.textContent = `Camera error: ${err.message}. Manual mode still works.`;
    }
  };

  const scan = async () => {
    if (!camera.stream) { await startCamera(); if (!camera.stream) return; }
    state.cancelled = false;
    status.textContent = "Capturing a dark baseline — all lamps off.";
    ctx.stopTest();
    await wait(250);
    await camera.captureDark(5);
    const results = state.results;
    results.clear();
    const ctx2d = marker.getContext("2d");
    ctx2d.clearRect(0, 0, marker.width, marker.height);
    for (let i = 0; i < state.order.length && !state.cancelled; i += 1) {
      const lamp = state.order[i];
      status.textContent = `Lamp ${lamp} (${i + 1}/${state.order.length})…`;
      await ctx.api("/ui/api/paint", {
        method: "POST",
        body: { device: cap.name, lamps: [{ index: lamp, rgb: [255, 255, 255] }], duration_ms: 700, label: "webcam" },
      });
      await wait(260);
      const blob = await camera.detect();
      if (blob && blob.confidence > 0.15) {
        results.set(lamp, {
          lamp, px: blob.nx, py: blob.ny, confidence: blob.confidence, source: "webcam",
        });
        ctx2d.fillStyle = "rgba(91,157,255,0.9)";
        ctx2d.beginPath();
        ctx2d.arc(blob.nx * marker.width, blob.ny * marker.height, 5, 0, Math.PI * 2);
        ctx2d.fill();
      }
    }
    ctx.stopTest();
    status.textContent = `Located ${results.size} of ${state.order.length} lamps. Name them now.`;
    await namePhase(ctx, cap, layout, host, state, byLamp);
  };

  const buttons = h("div", { class: "row" },
    button("Start camera", startCamera),
    button("Scan lamps", scan, { variant: "primary" }),
    button("Stop / all off", () => { state.cancelled = true; ctx.stopTest(); camera.stop(); }),
    button("Manual mode instead", () => { camera.stop(); state.at = 0; startManual(ctx, cap, layout, host, state); }, { variant: "ghost" }));

  host.appendChild(h("div", { class: "card stack" }, videoWrap, exposure, status, buttons));
  host.appendChild(banner("info", "Camera frames never leave this machine; the page is same-origin and offline."));
}

async function namePhase(ctx, cap, layout, host, state, byLamp) {
  const detected = [...state.results.keys()];
  if (!detected.length) {
    toast("Nothing located — use manual mode", "bad");
    return;
  }
  let at = 0;
  const progressBar = h("span", { style: { width: "0%" } });
  const label = h("strong", {}, "");
  const detail = h("span", { class: "muted" }, "Press the lit key; webcam confidence shown as a chip.");
  const paint = (lamp) => {
    ctx.test({ device: cap.name, lamps: [{ index: lamp, rgb: [255, 255, 255] }], duration_ms: 120000, label: "wizard" });
  };
  const step = (delta = 1) => {
    at += delta;
    if (at < 0) at = 0;
    if (at >= detected.length) {
      ctx.stopTest();
      toast("Naming pass complete");
      host.querySelector(".namephase")?.remove();
      return;
    }
    const lamp = detected[at];
    label.textContent = `Lamp ${lamp} (${at + 1}/${detected.length}) · ${byLamp.get(lamp)?.label || ""}`;
    progressBar.style.width = `${Math.round((at / detected.length) * 100)}%`;
    paint(lamp);
  };
  const capture = () => captureKey((code) => {
    const lamp = detected[at];
    const entry = state.results.get(lamp) || { lamp };
    entry.code = code;
    entry.label = codeLabel(code);
    state.results.set(lamp, entry);
    step(1);
  });
  const panel = h("div", { class: "card stack namephase" },
    h("div", { class: "row" }, label, h("div", { class: "progress", style: { flex: "1" } }, progressBar)),
    detail,
    h("div", { class: "row" },
      button("Press the lit key", capture, { variant: "primary" }),
      button("Light it again", () => paint(detected[at])),
      button("Skip", () => step(1)),
      button("Back", () => step(-1))));
  host.appendChild(panel);
  step(0);
}

// ---------------------------------------------------------------------------
// saving
// ---------------------------------------------------------------------------
function resultsToKeys(layout, results) {
  const byLamp = keysByLamp(layout);
  const keys = [];
  for (const [lamp, found] of results) {
    const existing = byLamp.get(lamp) || { lamp, x: 0, y: lamp, w: 1, h: 1 };
    keys.push({ ...existing, ...found });
  }
  return keys;
}

function saveResults(ctx, cap, layout, state) {
  if (!state.results.size) return toast("Nothing to save yet");
  const id = ensureLayout(ctx, cap);
  const found = resultsToKeys(layout, state.results);
  ctx.replaceDraft((draft) => {
    const target = draft.layouts[id];
    for (const key of found) {
      const index = target.keys.findIndex((k) => k.lamp === key.lamp);
      if (index >= 0) target.keys[index] = { ...target.keys[index], ...key };
      else target.keys.push(key);
    }
    const entry = draft.devices[cap.name] || {};
    if (!entry.lane_pool || !entry.lane_pool.length) {
      draft.devices[cap.name] = { ...entry, lane_pool: defaultLanePool(target, ctx.count()) };
    }
    return draft;
  }, "mapping wizard result");
  toast(`Saved ${found.length} key mappings to the draft`);
  ctx.refresh();
}

function ensureLayout(ctx, cap) {
  let id = (ctx.draft.devices?.[cap.name] || {}).layout;
  if (id && ctx.draft.layouts?.[id]) return id;
  id = `${cap.name}-default`;
  ctx.replaceDraft((draft) => {
    draft.layouts = draft.layouts || {};
    draft.layouts[id] = autoLayout(cap.lamps);
    draft.devices = draft.devices || {};
    draft.devices[cap.name] = { ...(draft.devices[cap.name] || {}), layout: id };
    return draft;
  }, "create layout");
  return id;
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
