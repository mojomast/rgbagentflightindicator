// Layout topology: keys, lamps, zones and the geometry maths that connects
// them. Pure functions only, so node --test can exercise every rule without a
// browser. Coordinates are keyboard units (1u = one key width); the case and
// perimeter paths are allowed outside the key grid, including negative values.

export const KEY_GROUP_DEFAULT = "unmapped";

export function geometryOf(entity) {
  if (!entity || typeof entity !== "object") return null;
  if (entity.geometry && typeof entity.geometry === "object") return entity.geometry;
  if (typeof entity.x === "number" && typeof entity.y === "number") {
    return { type: "rect", x: entity.x, y: entity.y,
             w: entity.w ?? 1, h: entity.h ?? 1 };
  }
  if (typeof entity.at === "object") {
    return { type: "point", x: entity.at[0], y: entity.at[1],
             size: entity.radius ?? 0.12 };
  }
  return null;
}

function shapeBounds(geometry, out) {
  if (!geometry || typeof geometry !== "object") return;
  if (geometry.type === "rect") {
    const w = geometry.w ?? 1, h = geometry.h ?? 1;
    out.minX = Math.min(out.minX, geometry.x ?? 0);
    out.minY = Math.min(out.minY, geometry.y ?? 0);
    out.maxX = Math.max(out.maxX, (geometry.x ?? 0) + w);
    out.maxY = Math.max(out.maxY, (geometry.y ?? 0) + h);
  } else if (geometry.type === "point") {
    const size = geometry.size ?? 0.12;
    out.minX = Math.min(out.minX, (geometry.x ?? 0) - size / 2);
    out.minY = Math.min(out.minY, (geometry.y ?? 0) - size / 2);
    out.maxX = Math.max(out.maxX, (geometry.x ?? 0) + size / 2);
    out.maxY = Math.max(out.maxY, (geometry.y ?? 0) + size / 2);
  } else if (geometry.type === "path") {
    const width = geometry.width ?? 0.18;
    for (const [x, y] of geometry.points || []) {
      out.minX = Math.min(out.minX, x - width / 2);
      out.minY = Math.min(out.minY, y - width / 2);
      out.maxX = Math.max(out.maxX, x + width / 2);
      out.maxY = Math.max(out.maxY, y + width / 2);
    }
  } else if (geometry.type === "union") {
    for (const shape of geometry.shapes || []) shapeBounds(shape, out);
  }
}

export function boundsOf(layout, { includeCase = true } = {}) {
  const out = { minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity };
  for (const key of (layout && layout.keys) || []) shapeBounds(geometryOf(key), out);
  for (const lamp of (layout && layout.lamps) || []) shapeBounds(geometryOf(lamp), out);
  for (const zone of (layout && layout.zones) || []) shapeBounds(geometryOf(zone), out);
  const box = caseOf(layout);
  if (includeCase && box) {
    out.minX = Math.min(out.minX, box.x);
    out.minY = Math.min(out.minY, box.y);
    out.maxX = Math.max(out.maxX, box.x + box.w);
    out.maxY = Math.max(out.maxY, box.y + box.h);
  }
  if (!Number.isFinite(out.minX)) return { minX: 0, minY: 0, maxX: 1, maxY: 1 };
  return out;
}

/** The case outline: explicit, or the key bounds grown by the canvas margin. */
export function caseOf(layout) {
  if (layout && layout.case && typeof layout.case === "object") {
    const box = layout.case;
    if ([box.x, box.y, box.w, box.h].every((v) => typeof v === "number")) {
      return { x: box.x, y: box.y, w: box.w, h: box.h, rx: box.rx ?? 0.35 };
    }
  }
  const keyBounds = { minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity };
  for (const key of (layout && layout.keys) || []) shapeBounds(geometryOf(key), keyBounds);
  if (!Number.isFinite(keyBounds.minX)) return null;
  const margin = (layout && layout.canvas && layout.canvas.margin) ?? 0.45;
  return {
    x: keyBounds.minX - margin,
    y: keyBounds.minY - margin,
    w: (keyBounds.maxX - keyBounds.minX) + margin * 2,
    h: (keyBounds.maxY - keyBounds.minY) + margin * 2,
    rx: 0.35,
  };
}

export function lampsOf(layout) {
  const lamps = [];
  const seen = new Set();
  for (const lamp of (layout && layout.lamps) || []) {
    if (!lamp || typeof lamp.index !== "number" || seen.has(lamp.index)) continue;
    seen.add(lamp.index);
    lamps.push(lamp);
  }
  // legacy layouts: a key with a lamp but no lamp record
  for (const key of (layout && layout.keys) || []) {
    if (!key || typeof key.lamp !== "number" || seen.has(key.lamp)) continue;
    seen.add(key.lamp);
    lamps.push({ index: key.lamp, kind: "key", key: key.id,
                 source: key.source, confidence: key.confidence });
  }
  return lamps.sort((a, b) => a.index - b.index);
}

export function lampsByIndex(layout) {
  const map = new Map();
  for (const lamp of lampsOf(layout)) map.set(lamp.index, lamp);
  return map;
}

export function keysById(layout) {
  const map = new Map();
  for (const key of (layout && layout.keys) || []) {
    if (key && typeof key.id === "string") map.set(key.id, key);
  }
  return map;
}

export function zoneOfLamp(layout) {
  const out = new Map();
  for (const lamp of (layout && layout.lamps) || []) {
    if (!lamp || typeof lamp.index !== "number") continue;
    const ref = lamp.zone || (lamp.path && lamp.path.zone);
    if (ref) out.set(lamp.index, ref);
  }
  for (const zone of (layout && layout.zones) || []) {
    if (!zone || typeof zone.id !== "string") continue;
    for (const index of zone.lamps || []) out.set(index, zone.id);
  }
  return out;
}

export function zoneOf(layout, zoneId) {
  return ((layout && layout.zones) || []).find((zone) => zone && zone.id === zoneId) || null;
}

export function effectiveKind(lamp) {
  if (!lamp) return "unknown";
  if (typeof lamp.kind === "string") return lamp.kind;
  return lamp.key ? "key" : "unknown";
}

export function isUnverified(lamp) {
  if (!lamp) return true;
  if (lamp.verified) return false;
  const source = lamp.source || "";
  return source === "builtin-profile" || source === "auto" || source === "" ||
         (typeof lamp.confidence === "number" && lamp.confidence < 0.5);
}

// ---------------------------------------------------------------------------
// paths
// ---------------------------------------------------------------------------
export function polylineLength(points, closed = false) {
  let total = 0;
  for (let i = 1; i < points.length; i += 1) {
    total += Math.hypot(points[i][0] - points[i - 1][0], points[i][1] - points[i - 1][1]);
  }
  if (closed && points.length > 1) {
    total += Math.hypot(points[0][0] - points[points.length - 1][0],
                        points[0][1] - points[points.length - 1][1]);
  }
  return total;
}

export function pointAtPath(points, closed, t) {
  const n = points.length;
  if (n === 0) return { x: 0, y: 0, tx: 1, ty: 0 };
  if (n === 1) return { x: points[0][0], y: points[0][1], tx: 1, ty: 0 };
  const total = polylineLength(points, closed) || 1;
  let target = (((t % 1) + 1) % 1) * total;
  const count = closed ? n : n - 1;
  for (let i = 0; i < count; i += 1) {
    const a = points[i];
    const b = points[(i + 1) % n];
    const length = Math.hypot(b[0] - a[0], b[1] - a[1]);
    if (target <= length || i === count - 1) {
      const u = length ? Math.min(1, Math.max(0, target / length)) : 0;
      return {
        x: a[0] + (b[0] - a[0]) * u,
        y: a[1] + (b[1] - a[1]) * u,
        tx: (b[0] - a[0]) / (length || 1),
        ty: (b[1] - a[1]) / (length || 1),
      };
    }
    target -= length;
  }
  return { x: points[0][0], y: points[0][1], tx: 1, ty: 0 };
}

export function projectOnPath(points, closed, x, y) {
  const n = points.length;
  if (n < 2) return { t: 0, dist: Infinity, x, y };
  const total = polylineLength(points, closed) || 1;
  let best = { t: 0, dist: Infinity, x, y };
  let run = 0;
  const count = closed ? n : n - 1;
  for (let i = 0; i < count; i += 1) {
    const a = points[i];
    const b = points[(i + 1) % n];
    const dx = b[0] - a[0], dy = b[1] - a[1];
    const length = Math.hypot(dx, dy) || 1;
    const u = Math.min(1, Math.max(0, ((x - a[0]) * dx + (y - a[1]) * dy) / (length * length)));
    const px = a[0] + dx * u, py = a[1] + dy * u;
    const dist = Math.hypot(x - px, y - py);
    if (dist < best.dist) {
      best = { t: (run + u * length) / total, dist, x: px, y: py };
    }
    run += length;
  }
  return best;
}

export function pathD(points, closed = false) {
  if (!points || points.length < 2) return "";
  const d = points.map((p, i) => `${i ? "L" : "M"}${p[0]},${p[1]}`).join(" ");
  return closed ? `${d} Z` : d;
}

/** Sub-path between two arc-length fractions, as a ready-to-use SVG `d`. */
export function subPathD(points, closed, t0, t1) {
  const n = points.length;
  if (n < 2) return "";
  const start = pointAtPath(points, closed, t0);
  const end = pointAtPath(points, closed, t1);
  const forward = (((t1 - t0) % 1) + 1) % 1;
  const total = polylineLength(points, closed) || 1;
  const target = forward * total;
  // walk vertices between t0 and t1
  const vertices = [];
  const count = closed ? n : n - 1;
  let run = 0;
  const lengths = [];
  for (let i = 0; i < count; i += 1) {
    const a = points[i], b = points[(i + 1) % n];
    lengths.push(Math.hypot(b[0] - a[0], b[1] - a[1]));
  }
  let travelled = (t0 % 1) * total;
  let remaining = target;
  for (let i = 0; i < count && remaining > 1e-9; i += 1) {
    const segmentStart = run;
    const segmentEnd = run + lengths[i];
    run = segmentEnd;
    if (segmentEnd <= travelled + 1e-9) continue;
    const from = Math.max(travelled, segmentStart);
    const to = Math.min(travelled + remaining, segmentEnd);
    if (from <= segmentStart + 1e-9) vertices.push(points[(i + 1) % n]);
    remaining -= (to - from);
    travelled = to;
  }
  const inner = vertices.filter((v, i) => i === 0 || v !== vertices[i - 1]);
  const d = [`M${start.x},${start.y}`,
             ...inner.map((p) => `L${p[0]},${p[1]}`),
             `L${end.x},${end.y}`];
  return d.join(" ");
}

/** Ordered lamps of a path zone: stored t first, then the zone's list order. */
export function orderedPathLamps(layout, zone, lamps) {
  if (!zone) return [];
  const byIndex = new Map((lamps || lampsOf(layout)).map((l) => [l.index, l]));
  const indexes = (zone.lamps || []).filter((i) => byIndex.has(i));
  const decorated = indexes.map((index, position) => {
    const record = byIndex.get(index) || {};
    const t = record.path && typeof record.path.t === "number" ? record.path.t : null;
    return { index, t, position };
  });
  const anyT = decorated.some((entry) => entry.t !== null);
  decorated.sort((a, b) => {
    if (anyT && a.t !== null && b.t !== null && a.t !== b.t) return a.t - b.t;
    if (a.t !== null && b.t === null) return -1;
    if (a.t === null && b.t !== null) return 1;
    return a.position - b.position;
  });
  return decorated;
}

export function laneWindows(slots) {
  const windows = [];
  for (let i = 0; i < slots; i += 1) {
    windows.push({ index: i, t0: i / slots, t1: (i + 1) / slots });
  }
  return windows;
}

export function windowForT(t, slots) {
  const wrapped = ((t % 1) + 1) % 1;
  return Math.min(slots - 1, Math.floor(wrapped * slots));
}

// ---------------------------------------------------------------------------
// generation and migration
// ---------------------------------------------------------------------------
function guessKind(lamp) {
  const group = lamp.group || "";
  if (["number-row", "function-row", "key", "alphas", "numpad", "nav"].includes(group)) {
    return "key";
  }
  if (/^zone/.test(group)) return "accent";
  return "unknown";
}

/** A sensible v2 starter: keys from lamp order, honest about unknown kinds. */
export function autoLayout(lamps, columns = 13) {
  const keys = [];
  const records = [];
  const keyed = [];
  [...(lamps || [])].sort((a, b) => a.index - b.index).forEach((lamp, i) => {
    const id = `k${i}`;
    const kind = guessKind(lamp);
    const label = lamp.label && !/^slot\d+$/.test(lamp.label)
      ? lamp.label : (lamp.label || String(lamp.index));
    keys.push({
      id,
      code: null,
      label,
      group: lamp.group || KEY_GROUP_DEFAULT,
      geometry: { type: "rect", x: i % columns, y: Math.floor(i / columns),
                  w: 1, h: 1 },
    });
    records.push({
      index: lamp.index,
      kind,
      key: kind === "key" ? id : undefined,
      source: "auto",
      confidence: null,
    });
    if (kind === "key") keyed.push(lamp.index);
  });
  return {
    format_version: 2,
    source: "auto",
    name: "Approximate grid",
    units: "u",
    canvas: { margin: 0.45 },
    keys,
    lamps: records.map((record) => {
      const clean = { ...record };
      if (clean.key === undefined) delete clean.key;
      return clean;
    }),
    zones: keyed.length ? [{ id: "keys", label: "Keys", kind: "keys",
                             lamps: keyed, source: "auto" }] : [],
    meta: {
      assumptions: ["key geometry and kind guessed from lamp order and group"],
    },
  };
}

/** Client-side v1 -> v2 migration, mirroring rgi/webconfig.py. */
export function migrateLayout(layout) {
  if (!layout || typeof layout !== "object") return layout;
  const version = layout.format_version;
  if (typeof version === "number" && version >= 2) return layout;
  const out = { ...layout, format_version: 2 };
  const lamps = Array.isArray(layout.lamps) ? [...layout.lamps] : [];
  const usedIds = new Set(lamps.map((l) => l && l.key).filter(Boolean));
  const keys = (layout.keys || []).map((key, i) => {
    if (!key || typeof key !== "object") return key;
    const next = { ...key };
    if (!next.id || typeof next.id !== "string") {
      let id = `k${i}`;
      while (usedIds.has(id)) id += "_";
      next.id = id;
    }
    usedIds.add(next.id);
    if (!next.geometry && typeof next.x === "number") {
      next.geometry = { type: "rect", x: next.x, y: next.y ?? 0,
                        w: next.w ?? 1, h: next.h ?? 1 };
    }
    if (typeof next.lamp === "number" && !lamps.some((l) => l && l.index === next.lamp)) {
      const record = { index: next.lamp, kind: "key", key: next.id };
      if (next.source) record.source = next.source;
      if (typeof next.confidence === "number") record.confidence = next.confidence;
      lamps.push(record);
    }
    return next;
  });
  out.keys = keys;
  out.lamps = lamps;
  out.zones = Array.isArray(layout.zones) ? layout.zones : [];
  out.canvas = layout.canvas || { margin: 0.45 };
  out.meta = layout.meta || {};
  return out;
}
