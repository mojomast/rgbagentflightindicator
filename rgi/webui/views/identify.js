// Identify: teach the map which lamp is which key.
//
// Manual mode is ground truth: light a lamp, press the key, read event.code.
// Webcam assist adds evidence: references, a four-corner homography, a
// sequential differential pass, and classification into key / perimeter /
// logo / unknown — then the same human review. Nothing is saved until Review,
// and every probe is a time-boxed overlay on the single render loop.

import { h, clear } from "../lib/dom.js";
import {
  banner, button, captureKey, field, selectInput, toast,
} from "../components/ui.js";
import { createMapPane } from "../components/mappane.js";
import { autoLayout, codeLabel, resolveLayout } from "../lib/layout.js";
import { classifyEvidence, confidenceFor, WebcamMapper } from "../lib/webcam.js";
import { blobStats, deviation, solveHomography, invert3, applyH, detectLattice } from "../lib/cv.js";
import {
  caseOf, geometryOf, keysById, lampsOf, projectOnPath,
} from "../lib/topology.js";

const MANUAL_SECONDS = 3.5;
const WEBCAM_SECONDS = 0.8;

export function render(ctx) {
  const capability = ctx.device;
  const root = h("div", { class: "stack" });
  if (!capability) {
    root.appendChild(h("div", { class: "empty" }, "No device detected."));
    return root;
  }
  const state = {
    phase: "setup",
    mode: "manual",
    scope: "unnamed",
    order: [],
    at: 0,
    results: new Map(),
    camera: null,
    references: null,
    homography: null,
    running: false,
    cancelled: false,
    panel: h("div", { class: "identify-panel" }),
  };
  root.appendChild(h("div", { class: "view-head" },
    h("h1", {}, "Identify"),
    h("p", { class: "muted" },
      "Light one lamp at a time and tell the map what it is. Manual mode works everywhere; " +
      "webcam assist locates lamps and suggests their kind for review.")));
  const paneHost = h("div", { class: "map-host" });
  root.append(paneHost, state.panel);
  state.pane = createMapPane(ctx, {
    mode: "identify",
    capability,
    activeLamp: () => state.activeLamp,
    identifyOverlay: () => state.overlay,
    describe: (lamp) => describeResult(ctx, lamp, state),
    hud: () => [],
  });
  paneHost.appendChild(state.pane.el);
  state.overlay = new Map();

  window.addEventListener("beforeunload", () => cleanup(state, ctx), { once: true });
  renderSetup(ctx, capability, state);
  return root;
}

// ---------------------------------------------------------------------------
// setup
// ---------------------------------------------------------------------------
function renderSetup(ctx, capability, state) {
  const host = state.panel;
  clear(host);
  const layout = resolveLayout(ctx.draft, capability);
  const options = scopeOptions(ctx, capability, layout);
  const scope = selectInput(options.map((entry) => ({
    value: entry.id, label: `${entry.label} (${entry.count})` })),
    state.scope, (value) => { state.scope = value; renderSetup(ctx, capability, state); });
  const mode = selectInput([
    { value: "manual", label: "Manual — press the lit key" },
    { value: "webcam", label: "Webcam assist — locate, then name" },
  ], state.mode, (value) => { state.mode = value; renderSetup(ctx, capability, state); });
  const chosen = options.find((entry) => entry.id === state.scope) || options[0];
  const seconds = chosen.count * (state.mode === "manual" ? MANUAL_SECONDS : WEBCAM_SECONDS);
  const card = h("div", { class: "card" });
  card.appendChild(h("h3", {}, "Preflight"));
  card.appendChild(h("div", { class: "row" },
    field("Scope", scope), field("Mode", mode)));
  card.appendChild(h("p", { class: "muted" },
    `${chosen.count} lamps · estimated ${formatEstimate(seconds)} · ` +
    (state.mode === "manual"
      ? "press the lit key when prompted"
      : "needs a camera on this machine (localhost)")));
  card.appendChild(banner("warn",
    "While identifying, the board stops showing agent status. It returns automatically when you finish or cancel."));
  if (state.mode === "webcam" && !WebcamMapper.supported) {
    card.appendChild(banner("bad", WebcamMapper.reason));
  }
  card.appendChild(h("div", { class: "row" },
    button("Start", () => start(ctx, capability, state, chosen),
      { variant: "primary", disabled: !chosen.count }),
    button("Cancel", () => cleanup(state, ctx), { variant: "ghost" })));
  host.appendChild(card);
}

function scopeOptions(ctx, capability, layout) {
  const lamps = lampsOf(layout);
  const keys = keysById(layout);
  const pool = (ctx.draft.devices?.[capability.name]?.lane_pool || []);
  const unnamed = lamps.filter((lamp) => {
    const key = lamp.key ? keys.get(lamp.key) : null;
    return !key || !key.code;
  });
  return [
    { id: "pool", label: "Lane pool only", count: pool.length,
      lamps: pool.filter((lamp) => lamps.some((entry) => entry.index === lamp)) },
    { id: "unnamed", label: "Lamps without a key name", count: unnamed.length,
      lamps: unnamed.map((lamp) => lamp.index) },
    { id: "selected", label: "Selected on the map", count: ctx.selection.length,
      lamps: [...ctx.selection] },
    { id: "all", label: "All lamps", count: lamps.length,
      lamps: lamps.map((lamp) => lamp.index) },
  ];
}

async function start(ctx, capability, state, chosen) {
  state.order = chosen.lamps;
  state.at = 0;
  state.results.clear();
  state.overlay.clear();
  state.running = true;
  state.cancelled = false;
  if (!state.order.length) return toast("Nothing to map with this scope");
  if (state.mode === "manual") {
    state.phase = "manual";
    renderManual(ctx, capability, state);
  } else {
    state.phase = "webcam-setup";
    state.camera = new WebcamMapper(h("video", { playsinline: true, muted: true }));
    renderWebcamSetup(ctx, capability, state);
  }
}

// ---------------------------------------------------------------------------
// manual walk
// ---------------------------------------------------------------------------
function renderManual(ctx, capability, state) {
  const host = state.panel;
  clear(host);
  const card = h("div", { class: "card" });
  const bar = h("span", { style: { width: "0%" } });
  const counter = h("strong");
  const prompt = h("div", { class: "identify-prompt" });
  const detail = h("p", { class: "muted" });
  card.append(
    h("div", { class: "row" }, counter, h("div", { class: "progress", style: { flex: "1" } }, bar)),
    prompt, detail,
    h("div", { class: "row" },
      button("Press the lit key", () => armCapture(ctx, capability, state), { variant: "primary" }),
      button("Light again", () => paintLamp(ctx, capability, state, state.order[state.at])),
      button("Skip", () => advance(ctx, capability, state, 1)),
      button("Back", () => advance(ctx, capability, state, -1)),
      button("Stop", () => cleanup(state, ctx), { variant: "ghost" })));
  host.appendChild(card);

  const step = (delta) => {
    state.at = delta === 0 ? state.at : state.at + delta;
    if (state.at < 0) state.at = 0;
    if (state.at >= state.order.length) {
      state.phase = "review";
      state.activeLamp = null;
      ctx.setActiveLamp(null);
      ctx.stopTest();
      renderReview(ctx, capability, state);
      return;
    }
    const lamp = state.order[state.at];
    state.activeLamp = lamp;
    ctx.setActiveLamp(lamp);
    counter.textContent = `Lamp ${lamp} of ${state.order.length}`;
    bar.style.width = `${Math.round((state.at / state.order.length) * 100)}%`;
    prompt.textContent = "Press the key that is lit";
    detail.textContent = `${state.results.size} identified · ` +
      `${state.order.length - state.at - 1} to go`;
    state.pane.refresh();
    paintLamp(ctx, capability, state, lamp);
    armCapture(ctx, capability, state);
  };
  state.step = step;
  step(0);
}

function armCapture(ctx, capability, state) {
  captureKey((code) => {
    const lamp = state.order[state.at];
    if (lamp == null) return;
    state.results.set(lamp, { lamp, kind: "key", code, label: codeLabel(code),
                              source: "press", confidence: 1, verified: true });
    toast(`Lamp ${lamp} → ${code}`);
    state.step(1);
  });
}

function advance(ctx, capability, state, delta) {
  if (delta === -1) {
    if (state.at > 0) state.at -= 2;
  }
  state.step(delta);
}

async function paintLamp(ctx, capability, state, lamp) {
  if (lamp == null) return;
  try {
    await ctx.paint({ device: capability.name,
      lamps: [{ index: lamp, rgb: [255, 255, 255] }],
      duration_ms: 120000, label: "identify" });
  } catch (err) {
    toast(err.message, "bad");
  }
}

// ---------------------------------------------------------------------------
// webcam: references and calibration
// ---------------------------------------------------------------------------
function renderWebcamSetup(ctx, capability, state) {
  const host = state.panel;
  clear(host);
  const card = h("div", { class: "card" });
  card.appendChild(h("h3", {}, "Webcam setup"));
  const cameraSelect = selectInput([{ value: "", label: "default camera" }], "", () => {});
  const status = h("p", { class: "muted" }, "Allow the camera, then capture references.");
  state.camera.listCameras().then((cameras) => {
    clear(cameraSelect);
    for (const camera of cameras) {
      cameraSelect.appendChild(h("option", { value: camera.deviceId },
        camera.label || camera.deviceId.slice(0, 12)));
    }
  });
  const videoWrap = h("div", { class: "video-wrap" });
  state.canvas = h("canvas", { class: "video-overlay" });
  videoWrap.append(state.camera.video, state.canvas);
  card.append(
    field("Camera", cameraSelect),
    videoWrap, status,
    h("div", { class: "row" },
      button("Start camera", async () => {
        try {
          const info = await state.camera.start(cameraSelect.value || undefined);
          state.canvas.width = info.width;
          state.canvas.height = info.height;
          status.textContent = `camera ready · exposure ${info.exposure}`;
        } catch (err) {
          status.textContent = `camera error: ${err.message}`;
        }
      }),
      button("Capture references", async () => {
        if (!state.camera.stream) return toast("Start the camera first");
        await captureReferences(ctx, capability, state, status);
      }, { variant: "primary" }),
      button("Cancel", () => cleanup(state, ctx), { variant: "ghost" })));
  host.appendChild(card);
}

async function captureReferences(ctx, capability, state, status) {
  const camera = state.camera;
  const layout = resolveLayout(ctx.draft, capability);
  await ctx.stopTest();
  status.textContent = "dark reference: all lamps off…";
  await wait(260);
  const dark = await camera.medianGrab(5);
  status.textContent = "all-on reference…";
  const every = lampsOf(layout).map((lamp) => ({ index: lamp.index, rgb: [140, 140, 140] }));
  await ctx.paint({ device: capability.name, lamps: every, duration_ms: 2500, label: "identify" });
  await wait(300);
  const allOn = await camera.medianGrab(3);
  await ctx.stopTest();
  state.references = { dark, allOn, layout };
  const probe = new Float32Array(dark.luma.length);
  for (let i = 0; i < probe.length; i += 1) {
    probe[i] = Math.max(0, allOn.luma[i] - dark.luma[i]);
  }
  state.probe = probe;
  state.noise = Math.max(0.8, deviation(dark.luma));
  const lattice = detectLattice(allOn.luma, allOn.width, allOn.height);
  status.textContent = lattice
    ? `keycap grid detected: ${lattice.xs.length} columns × ${lattice.ys.length} rows`
    : "no regular keycap grid detected — corners can still be clicked by hand";
  state.lattice = lattice;
  state.corners = [];
  state.phase = "calibrate";
  renderCalibrate(ctx, capability, state, status);
}

function renderCalibrate(ctx, capability, state, status) {
  const host = state.panel;
  clear(host);
  const card = h("div", { class: "card" });
  card.appendChild(h("h3", {}, "Calibrate: click the four corners of the key area"));
  card.appendChild(h("p", { class: "muted" },
    "Top-left first, then clockwise: top-right, bottom-right, bottom-left. " +
    "Aim for the outer corners of the keycaps (Esc / Backspace / right Shift / left Ctrl region)."));
  const videoWrap = h("div", { class: "video-wrap" });
  videoWrap.append(state.camera.video, state.canvas);
  state.canvas.classList.add("clickable");
  state.canvas.onclick = (event) => {
    if (!state.camera.stream) return;
    const rect = state.canvas.getBoundingClientRect();
    const x = ((event.clientX - rect.left) / rect.width) * state.canvas.width;
    const y = ((event.clientY - rect.top) / rect.height) * state.canvas.height;
    state.corners.push([x, y]);
    drawCalibration(state);
    if (state.corners.length === 4) fitHomography(state);
  };
  const info = h("p", { class: "muted" });
  card.append(videoWrap, info, status,
    h("div", { class: "row" },
      button("Redo corners", () => { state.corners = []; state.homography = null;
                                      drawCalibration(state); fitHomography(state); }),
      button("Scan lamps", () => {
        if (!fitHomography(state)) return toast("Click four corners first");
        state.phase = "scan";
        runScan(ctx, capability, state);
      }, { variant: "primary" }),
      button("Cancel", () => cleanup(state, ctx), { variant: "ghost" })));
  host.appendChild(card);
  state.calibrationInfo = info;
  drawCalibration(state);
}

function fitHomography(state) {
  const layout = state.references?.layout;
  if (!layout || state.corners.length < 4) return false;
  const bounds = keyAreaBounds(layout);
  const corners = [[bounds.minX, bounds.minY], [bounds.maxX, bounds.minY],
                   [bounds.maxX, bounds.maxY], [bounds.minX, bounds.maxY]];
  const pairs = corners.map((corner, index) =>
    [corner[0], corner[1], state.corners[index][0], state.corners[index][1]]);
  const H = solveHomography(pairs);
  state.homography = H;
  if (!H) {
    state.calibrationInfo.textContent = "The four corners are degenerate; try again.";
    return false;
  }
  const inverse = invert3(H);
  state.unitFromImage = inverse;
  drawCalibration(state);
  state.calibrationInfo.textContent =
    "Overlay shows where the template expects keys. If it is wildly off, redo the corners.";
  return true;
}

function drawCalibration(state) {
  const canvas = state.canvas;
  const context = canvas.getContext("2d");
  context.clearRect(0, 0, canvas.width, canvas.height);
  context.fillStyle = "rgba(91, 157, 255, 0.95)";
  for (const [x, y] of state.corners || []) {
    context.beginPath();
    context.arc(x, y, 5, 0, Math.PI * 2);
    context.fill();
  }
  if (state.homography && state.references?.layout) {
    context.strokeStyle = "rgba(0, 255, 170, 0.9)";
    context.lineWidth = 1;
    for (const key of state.references.layout.keys) {
      const geometry = geometryOf(key);
      if (!geometry) continue;
      const centre = applyH(state.homography,
        (geometry.x ?? 0) + (geometry.w ?? 1) / 2,
        (geometry.y ?? 0) + (geometry.h ?? 1) / 2);
      context.beginPath();
      context.arc(centre.x, centre.y, 2.2, 0, Math.PI * 2);
      context.stroke();
    }
  }
  for (const result of state.results.values()) {
    if (!result.px) continue;
    context.fillStyle = result.kind === "perimeter" ? "#ff66ff"
      : result.kind === "key" ? "#66ff99" : "#ffcc66";
    context.beginPath();
    context.arc(result.px[0], result.px[1], 3, 0, Math.PI * 2);
    context.fill();
  }
}

// ---------------------------------------------------------------------------
// webcam: sequential scan
// ---------------------------------------------------------------------------
async function runScan(ctx, capability, state) {
  state.running = true;
  state.overlay.clear();
  state.results.clear();
  state.phase = "scan";
  const host = state.panel;
  clear(host);
  const bar = h("span", { style: { width: "0%" } });
  const status = h("p", { class: "muted" }, "Scanning…");
  const card = h("div", { class: "card" },
    h("div", { class: "row" }, h("strong", {}, "Webcam scan"),
      h("div", { class: "progress", style: { flex: "1" } }, bar)),
    status,
    h("div", { class: "row" },
      button("Stop", () => { state.cancelled = true; }, { variant: "ghost" })));
  host.appendChild(card);

  const layout = state.references.layout;
  const lamps = lampsOf(layout);
  const keys = keysById(layout);
  const keyCentres = [];
  for (const key of layout.keys) {
    const geometry = geometryOf(key);
    if (!geometry) continue;
    keyCentres.push({ id: key.id, code: key.code, label: key.label,
                      x: (geometry.x ?? 0) + (geometry.w ?? 1) / 2,
                      y: (geometry.y ?? 0) + (geometry.h ?? 1) / 2 });
  }
  const box = caseOf(layout);
  const casePath = box ? [[box.x, box.y], [box.x + box.w, box.y],
                          [box.x + box.w, box.y + box.h], [box.x, box.y + box.h]] : null;

  for (let i = 0; i < state.order.length && !state.cancelled; i += 1) {
    const lamp = state.order[i];
    status.textContent = `Lamp ${lamp} (${i + 1}/${state.order.length})…`;
    bar.style.width = `${Math.round((i / state.order.length) * 100)}%`;
    state.activeLamp = lamp;
    ctx.setActiveLamp(lamp);
    try {
      await ctx.paint({ device: capability.name,
        lamps: [{ index: lamp, rgb: [255, 255, 255] }],
        duration_ms: 350, label: "identify" });
    } catch (err) {
      status.textContent = `paint failed: ${err.message}`;
      await wait(300);
      continue;
    }
    await wait(180);
    const lit = await state.camera.medianGrab(3, 30);
    const evidence = analyseLamp(state, lamp, lit, keyCentres, casePath);
    if (evidence) {
      state.results.set(lamp, evidence);
      if (evidence.unit) {
        state.overlay.set(lamp, { x: evidence.unit.x, y: evidence.unit.y,
                                  kind: evidence.kind,
                                  confidence: evidence.confidence });
      }
    }
    if (i % 4 === 0) {
      drawCalibration(state);
      state.pane.refresh();
    }
  }
  await ctx.stopTest();
  state.activeLamp = null;
  ctx.setActiveLamp(null);
  drawCalibration(state);
  state.pane.refresh();
  state.phase = "review";
  renderReview(ctx, capability, state);
}

function analyseLamp(state, lamp, lit, keyCentres, casePath) {
  const dark = state.references.dark;
  const diff = new Float32Array(lit.luma.length);
  for (let i = 0; i < diff.length; i += 1) {
    diff[i] = Math.max(0, lit.luma[i] - dark.luma[i]);
  }
  const stats = blobStats(diff, lit.width, lit.height,
                          { noise: state.noise, absoluteFloor: 4 });
  if (!stats.blobs.length) {
    return { lamp, kind: "unknown", confidence: 0.05, reason: "no response",
             px: null, unit: null };
  }
  const blob = stats.blobs[0];
  const snr = blob.peak / Math.max(0.5, state.noise);
  const unit = state.unitFromImage
    ? applyH(state.unitFromImage, blob.cx, blob.cy) : null;
  let dCell = null;
  let nearest = null;
  if (unit) {
    for (const centre of keyCentres) {
      const distance = Math.hypot(unit.x - centre.x, unit.y - centre.y);
      if (nearest == null || distance < dCell) { dCell = distance; nearest = centre; }
    }
  }
  const dPath = unit && casePath ? pathDistance(casePath, unit) : null;
  const compact = blob.elongation <= 2.5;
  const insideCase = Boolean(unit && casePath && pointInPolygon(casePath, unit.x, unit.y));
  const kind = classifyEvidence({ dCell, dPath, elongation: blob.elongation,
                                  compact, insideCase, hasCell: keyCentres.length > 0 });
  const confidence = confidenceFor({ snr, dCell: dCell ?? 1, compact });
  return {
    lamp, kind, confidence, snr,
    keyId: kind === "key" && nearest ? nearest.id : null,
    code: kind === "key" && nearest ? nearest.code : null,
    label: kind === "key" && nearest ? nearest.label : null,
    t: kind === "perimeter" ? perimeterT(casePath, unit) : null,
    px: [blob.cx, blob.cy], unit,
  };
}

function perimeterT(path, unit) {
  if (!path || !unit) return null;
  return projectOnPath(path, true, unit.x, unit.y).t;
}

function pathDistance(path, unit) {
  return projectOnPath(path, true, unit.x, unit.y).dist;
}

function pointInPolygon(points, x, y) {
  let inside = false;
  for (let i = 0, j = points.length - 1; i < points.length; j = i, i += 1) {
    const [xi, yi] = points[i];
    const [xj, yj] = points[j];
    const intersect = ((yi > y) !== (yj > y)) &&
      (x < ((xj - xi) * (y - yi)) / ((yj - yi) || 1e-9) + xi);
    if (intersect) inside = !inside;
  }
  return inside;
}

// ---------------------------------------------------------------------------
// review and save
// ---------------------------------------------------------------------------
function renderReview(ctx, capability, state) {
  const host = state.panel;
  clear(host);
  const results = [...state.results.values()];
  const keys = results.filter((result) => result.kind === "key");
  const perimeter = results.filter((result) => result.kind === "perimeter")
    .sort((a, b) => (a.t ?? 0) - (b.t ?? 0));
  const others = results.filter((result) => !["key", "perimeter"].includes(result.kind));
  const card = h("div", { class: "card" });
  card.appendChild(h("h3", {}, "Review"));
  card.appendChild(h("p", { class: "muted" },
    `${keys.length} key · ${perimeter.length} base/perimeter · ${others.length} other · ` +
    `${state.order.length - results.length} no response`));
  const table = h("table", { class: "table" });
  table.appendChild(h("thead", null, h("tr", null,
    h("th", {}, "Lamp"), h("th", {}, "Kind"), h("th", {}, "Key / t"),
    h("th", {}, "Confidence"), h("th", {}, "Source"), h("th", {}, ""))));
  const body = h("tbody");
  for (const result of results) {
    body.appendChild(h("tr", null,
      h("td", { class: "mono" }, result.lamp),
      h("td", null, chipFor(result.kind)),
      h("td", { class: "mono" },
        result.kind === "key"
          ? `${result.code || "?"} (${result.label || "?"})`
          : result.kind === "perimeter"
            ? `t ${(result.t ?? 0).toFixed(3)}`
            : "—"),
      h("td", { class: "num" }, `${Math.round((result.confidence ?? 0) * 100)}%`),
      h("td", { class: "muted" }, result.source || "webcam"),
      h("td", null,
        button("Name…", () => {
          captureKey((code) => {
            result.kind = "key";
            result.code = code;
            result.label = codeLabel(code);
            result.source = "press";
            result.confidence = 1;
            result.verified = true;
            renderReview(ctx, capability, state);
          });
        }, { variant: "ghost", sm: true }),
        button("Mark base", () => {
          result.kind = "perimeter";
          result.t = result.t ?? 0.5;
          renderReview(ctx, capability, state);
        }, { variant: "ghost", sm: true, title: "This lamp is part of the base/underglow" }),
        button("Flash", () => paintLamp(ctx, capability, state, result.lamp),
          { variant: "ghost", sm: true }))));
  }
  table.appendChild(body);
  card.appendChild(table);

  if (perimeter.length) {
    const preview = h("div", { class: "row" },
      button("Light neighbours of the first base lamp", () => {
        const first = perimeter[0];
        const previous = perimeter[perimeter.length - 1];
        const nextLamp = perimeter[1] || previous;
        state.overlay.clear();
        for (const entry of perimeter) {
          if (entry.unit) {
            state.overlay.set(entry.lamp, { x: entry.unit.x, y: entry.unit.y,
                                            kind: "perimeter",
                                            confidence: entry.confidence });
          }
        }
        state.pane.refresh();
        ctx.paint({ device: capability.name, lamps: [
          { index: previous.lamp, rgb: [255, 0, 0] },
          { index: first.lamp, rgb: [0, 255, 0] },
          { index: nextLamp.lamp, rgb: [0, 0, 255] },
        ], duration_ms: 2500, label: "identify" });
      }),
      button("Reverse order", () => {
        perimeter.reverse();
        perimeter.forEach((entry, index) => { entry.t = index / perimeter.length; });
        renderReview(ctx, capability, state);
      }, { variant: "ghost" }));
    card.appendChild(h("h4", {}, "Base/perimeter order"));
    card.appendChild(h("p", { class: "muted" },
      `Index order: ${perimeter.map((entry) => entry.lamp).join(" → ")}`));
    card.appendChild(preview);
  }

  card.appendChild(h("div", { class: "row", style: { marginTop: "12px" } },
    button(`Save ${results.length} mappings to draft`, () => {
      saveResults(ctx, capability, state, results, perimeter);
    }, { variant: "primary" }),
    button("Re-run webcam scan", () => {
      if (state.phase === "review" && state.camera?.stream) {
        state.results.clear();
        state.overlay.clear();
        runScan(ctx, capability, state);
      } else {
        toast("Start the webcam first");
      }
    }, { variant: "ghost" }),
    button("Start over", () => { cleanup(state, ctx); renderSetup(ctx, capability, state); },
      { variant: "ghost" }),
    button("Cancel", () => cleanup(state, ctx), { variant: "ghost" })));
  host.appendChild(card);
}

function chipFor(kind) {
  const colours = { key: "ok", perimeter: "warn", logo: "", unknown: "bad" };
  return h("span", { class: `badge ${colours[kind] || ""}` }, kind);
}

function saveResults(ctx, capability, state, results, perimeter) {
  const layout = resolveLayout(ctx.draft, capability);
  const id = ensureLayout(ctx, capability);
  const box = caseOf(layout);
  const casePath = box ? [[box.x, box.y], [box.x + box.w, box.y],
                          [box.x + box.w, box.y + box.h], [box.x, box.y + box.h]] : null;
  const zoneId = uniqueZoneId(layout, "perimeter");
  ctx.replaceDraft((draft) => {
    const node = draft.layouts[id];
    const keys = node.keys;
    for (const result of results) {
      let record = (node.lamps || []).find((entry) => entry.index === result.lamp);
      if (!record) { record = { index: result.lamp }; node.lamps.push(record); }
      record.source = result.source === "press" ? "press" : "webcam";
      record.confidence = result.confidence;
      record.verified = Boolean(result.verified);
      if (result.px) record.pixel = { points: [result.px], confidence: result.confidence };
      if (result.kind === "key") {
        record.kind = "key";
        let keyId = result.keyId || record.key;
        if (!keyId) {
          keyId = uniqueId(keys, `k${result.lamp}`);
          keys.push({ id: keyId, code: result.code || null,
                      label: result.label || `lamp ${result.lamp}`,
                      group: "unmapped",
                      geometry: { type: "rect", x: 0, y: 0, w: 1, h: 1 } });
        }
        record.key = keyId;
        delete record.zone;
        delete record.path;
        const key = keys.find((entry) => entry.id === keyId);
        if (key) {
          key.code = result.code || key.code;
          key.label = result.label || key.label;
        }
      } else if (result.kind === "perimeter") {
        record.kind = "perimeter";
        record.zone = zoneId;
        record.path = { zone: zoneId, t: result.t ?? 0 };
        delete record.key;
      } else {
        record.kind = result.kind || "unknown";
      }
    }
    if (perimeter.length && casePath) {
      let zone = (node.zones || []).find((entry) => entry.id === zoneId);
      if (!zone) {
        zone = { id: zoneId, label: "Base / underglow", kind: "perimeter",
                 geometry: { type: "path", closed: true, width: 0.22, points: casePath },
                 ordering: "cw", source: "webcam", lamps: [] };
        node.zones.push(zone);
      }
      zone.lamps = perimeter.map((entry) => entry.lamp);
      zone.confidence = perimeter.reduce((sum, entry) => sum + (entry.confidence || 0), 0) /
        perimeter.length;
    }
    return draft;
  }, "identify results");
  toast(`Saved ${results.length} mappings to the draft — review, then Apply`);
  cleanup(state, ctx);
  ctx.go("#/layout");
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------
function keyAreaBounds(layout) {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const key of layout.keys || []) {
    const geometry = geometryOf(key);
    if (!geometry) continue;
    minX = Math.min(minX, geometry.x ?? 0);
    minY = Math.min(minY, geometry.y ?? 0);
    maxX = Math.max(maxX, (geometry.x ?? 0) + (geometry.w ?? 1));
    maxY = Math.max(maxY, (geometry.y ?? 0) + (geometry.h ?? 1));
  }
  if (!Number.isFinite(minX)) return { minX: 0, minY: 0, maxX: 1, maxY: 1 };
  return { minX, minY, maxX, maxY };
}

function identity() {
  return [[1, 0, 0], [0, 1, 0], [0, 0, 1]];
}

function ensureLayout(ctx, capability) {
  const entry = (ctx.draft.devices || {})[capability.name] || {};
  if (entry.layout && ctx.draft.layouts?.[entry.layout]) return entry.layout;
  const id = `${capability.name}-default`;
  if (!ctx.draft.layouts?.[id]) {
    ctx.replaceDraft((draft) => {
      draft.layouts = draft.layouts || {};
      draft.layouts[id] = autoLayout(capability.lamps);
      draft.devices = draft.devices || {};
      draft.devices[capability.name] = { ...(draft.devices[capability.name] || {}),
                                         layout: id };
      return draft;
    }, "create layout");
  }
  return id;
}

function uniqueId(keys, base) {
  let id = base;
  const used = new Set(keys.map((key) => key.id));
  while (used.has(id)) id += "_";
  return id;
}

function uniqueZoneId(layout, base) {
  let id = base;
  const used = new Set((layout.zones || []).map((zone) => zone.id));
  while (used.has(id)) id += "_";
  return id;
}

function cleanup(state, ctx) {
  state.running = false;
  state.cancelled = true;
  state.activeLamp = null;
  ctx.setActiveLamp(null);
  ctx.stopTest();
  state.camera?.stop?.();
}

function describeResult(ctx, lamp, state) {
  const result = state.results.get(lamp);
  if (!result) return h("span", { class: "muted" }, `Lamp ${lamp} · not scanned yet`);
  return h("div", { class: "stack tiny" },
    h("strong", {}, `Lamp ${lamp} · ${result.kind}`),
    h("span", {}, result.kind === "key"
      ? `key ${result.code || "?"} (${result.label || "?"})`
      : result.kind === "perimeter" ? `t ${(result.t ?? 0).toFixed(3)}` : "—"),
    h("span", {}, `confidence ${Math.round((result.confidence ?? 0) * 100)}%`),
    result.snr ? h("span", {}, `snr ${result.snr.toFixed(1)}`) : null);
}

function formatEstimate(seconds) {
  if (seconds < 60) return `${Math.round(seconds)} s`;
  return `${Math.floor(seconds / 60)} min ${Math.round(seconds % 60)} s`;
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
