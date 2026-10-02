// Pure-JS computer vision for the mapping wizard. No wasm, no OpenCV, no
// network; every function is deterministic and node --test exercises it.
//
// The pipeline it serves (see docs/webui.md):
//   frame -> profiles/periodicity -> grid seed -> homography -> evidence
//   -> blob stats -> classification; and a 1-D perimeter sampler that works in
//   arc length along the case outline so ordering falls out of the geometry.

// ---------------------------------------------------------------------------
// planes and filters
// ---------------------------------------------------------------------------
export function rgbPlanes(rgba, width, height) {
  const r = new Float32Array(width * height);
  const g = new Float32Array(width * height);
  const b = new Float32Array(width * height);
  for (let i = 0, p = 0; i < r.length; i += 1, p += 4) {
    r[i] = rgba[p];
    g[i] = rgba[p + 1];
    b[i] = rgba[p + 2];
  }
  return { r, g, b };
}

export function lumaPlane(rgba, width, height) {
  const out = new Float32Array(width * height);
  for (let i = 0, p = 0; i < out.length; i += 1, p += 4) {
    out[i] = 0.2126 * rgba[p] + 0.7152 * rgba[p + 1] + 0.0722 * rgba[p + 2];
  }
  return out;
}

export function subtractPlane(a, b) {
  const out = new Float32Array(a.length);
  for (let i = 0; i < a.length; i += 1) out[i] = a[i] - b[i];
  return out;
}

export function positivePart(plane) {
  const out = new Float32Array(plane.length);
  for (let i = 0; i < plane.length; i += 1) out[i] = plane[i] > 0 ? plane[i] : 0;
  return out;
}

export function medianPlanes(planes) {
  const out = new Float32Array(planes[0].length);
  const values = new Array(planes.length);
  for (let i = 0; i < out.length; i += 1) {
    for (let j = 0; j < planes.length; j += 1) values[j] = planes[j][i];
    values.sort((a, b) => a - b);
    const mid = values.length >> 1;
    out[i] = values.length % 2 ? values[mid] : (values[mid - 1] + values[mid]) / 2;
  }
  return out;
}

export function boxBlur(plane, width, height, radius = 1) {
  const horizontal = new Float32Array(plane.length);
  for (let y = 0; y < height; y += 1) {
    const row = y * width;
    let sum = 0;
    for (let x = -radius; x <= radius; x += 1) sum += plane[row + clamp(x, 0, width - 1)];
    for (let x = 0; x < width; x += 1) {
      horizontal[row + x] = sum / (radius * 2 + 1);
      sum -= plane[row + clamp(x - radius, 0, width - 1)];
      sum += plane[row + clamp(x + radius + 1, 0, width - 1)];
    }
  }
  const out = new Float32Array(plane.length);
  for (let x = 0; x < width; x += 1) {
    let sum = 0;
    for (let y = -radius; y <= radius; y += 1) sum += horizontal[clamp(y, 0, height - 1) * width + x];
    for (let y = 0; y < height; y += 1) {
      out[y * width + x] = sum / (radius * 2 + 1);
      sum -= horizontal[clamp(y - radius, 0, height - 1) * width + x];
      sum += horizontal[clamp(y + radius + 1, 0, height - 1) * width + x];
    }
  }
  return out;
}

function clamp(value, lo, hi) { return value < lo ? lo : value > hi ? hi : value; }

export function gradientX(plane, width, height) {
  const out = new Float32Array(plane.length);
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const left = plane[y * width + Math.max(0, x - 1)];
      const right = plane[y * width + Math.min(width - 1, x + 1)];
      out[y * width + x] = (right - left) / 2;
    }
  }
  return out;
}

export function gradientY(plane, width, height) {
  const out = new Float32Array(plane.length);
  for (let x = 0; x < width; x += 1) {
    for (let y = 0; y < height; y += 1) {
      const up = plane[Math.max(0, y - 1) * width + x];
      const down = plane[Math.min(height - 1, y + 1) * width + x];
      out[y * width + x] = (down - up) / 2;
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// projection profiles + periodicity (keycap lattice detection)
// ---------------------------------------------------------------------------
export function profiles(gray, width, height) {
  const gx = gradientX(gray, width, height);
  const gy = gradientY(gray, width, height);
  const v = new Float32Array(width);
  const h = new Float32Array(height);
  const column = new Float32Array(height);
  for (let x = 0; x < width; x += 1) {
    for (let y = 0; y < height; y += 1) column[y] = Math.abs(gx[y * width + x]);
    v[x] = medianOf(column);
  }
  const row = new Float32Array(width);
  for (let y = 0; y < height; y += 1) {
    row.set(gy.subarray(y * width, y * width + width));
    for (let i = 0; i < width; i += 1) row[i] = Math.abs(row[i]);
    h[y] = medianOf(row);
  }
  return { v, h };
}

export function medianOf(array) {
  const copy = Array.from(array).sort((a, b) => a - b);
  if (!copy.length) return 0;
  const mid = copy.length >> 1;
  return copy.length % 2 ? copy[mid] : (copy[mid - 1] + copy[mid]) / 2;
}

export function mean(array) {
  let sum = 0;
  for (const value of array) sum += value;
  return array.length ? sum / array.length : 0;
}

export function deviation(array) {
  const mu = mean(array);
  let sum = 0;
  for (const value of array) sum += (value - mu) ** 2;
  return Math.sqrt(sum / Math.max(1, array.length));
}

/** First strong autocorrelation peak in [lo, hi]; returns {period, score}. */
export function periodicity(profile, lo, hi) {
  const n = profile.length;
  const mu = mean(profile);
  const centred = new Float32Array(n);
  let variance = 0;
  for (let i = 0; i < n; i += 1) {
    centred[i] = profile[i] - mu;
    variance += centred[i] * centred[i];
  }
  variance = variance / n || 1;
  let best = { period: 0, score: 0 };
  const low = Math.max(2, Math.floor(lo));
  const high = Math.min(n - 2, Math.ceil(hi));
  for (let d = low; d <= high; d += 1) {
    let sum = 0;
    for (let i = 0; i + d < n; i += 1) sum += centred[i] * centred[i + d];
    const score = sum / (n - d) / variance;
    if (score > best.score) best = { period: d, score };
  }
  return best;
}

/** Local maxima with prominence and minimum separation, sub-pixel refined. */
export function findPeaks(profile, { minProminence = 0.2, minSeparation = 2 } = {}) {
  const peaks = [];
  const max = Math.max(...profile);
  const threshold = max * minProminence;
  for (let i = 1; i < profile.length - 1; i += 1) {
    if (profile[i] < threshold) continue;
    if (profile[i] >= profile[i - 1] && profile[i] >= profile[i + 1]) {
      const denominator = profile[i - 1] - 2 * profile[i] + profile[i + 1];
      const offset = denominator ? 0.5 * (profile[i - 1] - profile[i + 1]) / denominator : 0;
      peaks.push({ index: i + Math.max(-1, Math.min(1, offset)),
                   value: profile[i] });
    }
  }
  peaks.sort((a, b) => b.value - a.value);
  const kept = [];
  for (const peak of peaks) {
    if (kept.every((other) => Math.abs(other.index - peak.index) >= minSeparation)) {
      kept.push(peak);
    }
  }
  return kept.sort((a, b) => a.index - b.index);
}

/**
 * Propose a key lattice from an all-on frame. Returns null when the image has
 * no regular structure (opaque keycaps, no backlight, camera not looking).
 */
export function detectLattice(gray, width, height) {
  const { v, h } = profiles(boxBlur(gray, width, height, 1), width, height);
  const vp = periodicity(v, width * 0.03, width * 0.25);
  const hp = periodicity(h, height * 0.03, height * 0.25);
  // a flat or textureless image has no periodicity to lock onto
  if (vp.score < 0.08 || hp.score < 0.08) return null;
  const xs = findPeaks(v, { minProminence: 0.25, minSeparation: vp.period * 0.4 });
  const ys = findPeaks(h, { minProminence: 0.25, minSeparation: hp.period * 0.4 });
  if (xs.length < 8 || ys.length < 4) return null;
  const xPitch = deviation(xs.map((p, i) => i ? p.index - xs[i - 1].index : null).filter(Boolean)) /
    (vp.period || 1);
  const yPitch = deviation(ys.map((p, i) => i ? p.index - ys[i - 1].index : null).filter(Boolean)) /
    (hp.period || 1);
  if (xPitch > 0.08 || yPitch > 0.08) return null;
  return { xs: xs.map((p) => p.index), ys: ys.map((p) => p.index),
           xPitch: vp.period, yPitch: hp.period };
}

// ---------------------------------------------------------------------------
// homography
// ---------------------------------------------------------------------------
function normalizePoints(points) {
  let cx = 0, cy = 0;
  for (const [x, y] of points) { cx += x; cy += y; }
  cx /= points.length; cy /= points.length;
  let meanDistance = 0;
  for (const [x, y] of points) meanDistance += Math.hypot(x - cx, y - cy);
  meanDistance /= points.length || 1;
  const scale = meanDistance > 1e-9 ? Math.SQRT2 / meanDistance : 1;
  return { cx, cy, scale,
           matrix: [[scale, 0, -scale * cx], [0, scale, -scale * cy], [0, 0, 1]] };
}

export function invert3(m) {
  const [a, b, c] = m[0];
  const [d, e, f] = m[1];
  const [g, hh, i] = m[2];
  const A = e * i - f * hh;
  const B = -(d * i - f * g);
  const C = d * hh - e * g;
  const det = a * A + b * B + c * C;
  if (Math.abs(det) < 1e-12) return null;
  return [
    [A / det, -(b * i - c * hh) / det, (b * f - c * e) / det],
    [B / det, (a * i - c * g) / det, -(a * f - c * d) / det],
    [C / det, -(a * hh - b * g) / det, (a * e - b * d) / det],
  ];
}

export function multiply3(a, b) {
  const out = [[0, 0, 0], [0, 0, 0], [0, 0, 0]];
  for (let i = 0; i < 3; i += 1) {
    for (let j = 0; j < 3; j += 1) {
      out[i][j] = a[i][0] * b[0][j] + a[i][1] * b[1][j] + a[i][2] * b[2][j];
    }
  }
  return out;
}

export function applyH(H, x, y) {
  const w = H[2][0] * x + H[2][1] * y + H[2][2];
  return {
    x: (H[0][0] * x + H[0][1] * y + H[0][2]) / (w || 1e-12),
    y: (H[1][0] * x + H[1][1] * y + H[1][2]) / (w || 1e-12),
  };
}

export function solveLinear(A, b) {
  const n = A.length;
  const M = A.map((row, i) => [...row, b[i]]);
  for (let col = 0; col < n; col += 1) {
    let pivot = col;
    for (let row = col + 1; row < n; row += 1) {
      if (Math.abs(M[row][col]) > Math.abs(M[pivot][col])) pivot = row;
    }
    if (Math.abs(M[pivot][col]) < 1e-12) return null;
    [M[col], M[pivot]] = [M[pivot], M[col]];
    for (let row = col + 1; row < n; row += 1) {
      const factor = M[row][col] / M[col][col];
      if (!factor) continue;
      for (let k = col; k <= n; k += 1) M[row][k] -= factor * M[col][k];
    }
  }
  const out = new Array(n).fill(0);
  for (let row = n - 1; row >= 0; row -= 1) {
    let sum = M[row][n];
    for (let k = row + 1; k < n; k += 1) sum -= M[row][k] * out[k];
    out[row] = sum / M[row][row];
  }
  return out;
}

/**
 * Normalized DLT over [unitX, unitY, imageX, imageY] pairs.
 * Weighted least squares (weights default 1); returns 3x3 or null.
 */
export function solveHomography(pairs, weights = null, lambda = 1e-9) {
  if (pairs.length < 4) return null;
  const modelNorm = normalizePoints(pairs.map((p) => [p[0], p[1]]));
  const imageNorm = normalizePoints(pairs.map((p) => [p[2], p[3]]));
  const rows = [];
  const rhs = [];
  for (let i = 0; i < pairs.length; i += 1) {
    const X = modelNorm.matrix[0][0] * pairs[i][0] + modelNorm.matrix[0][2];
    const Y = modelNorm.matrix[1][1] * pairs[i][1] + modelNorm.matrix[1][2];
    const image = applyH(imageNorm.matrix, pairs[i][2], pairs[i][3]);
    const weight = weights ? Math.sqrt(weights[i]) : 1;
    if (!weight) continue;
    rows.push([X * weight, Y * weight, weight, 0, 0, 0,
               -image.x * X * weight, -image.x * Y * weight]);
    rhs.push(image.x * weight);
    rows.push([0, 0, 0, X * weight, Y * weight, weight,
               -image.y * X * weight, -image.y * Y * weight]);
    rhs.push(image.y * weight);
  }
  const A = Array.from({ length: 8 }, () => new Array(8).fill(0));
  const b = new Array(8).fill(0);
  for (let r = 0; r < rows.length; r += 1) {
    for (let i = 0; i < 8; i += 1) {
      b[i] += rows[r][i] * rhs[r];
      for (let j = 0; j < 8; j += 1) A[i][j] += rows[r][i] * rows[r][j];
    }
  }
  for (let i = 0; i < 8; i += 1) A[i][i] += lambda;
  const h = solveLinear(A, b);
  if (!h) return null;
  const normalized = [[h[0], h[1], h[2]], [h[3], h[4], h[5]], [h[6], h[7], 1]];
  const imageInv = invert3(imageNorm.matrix);
  if (!imageInv) return null;
  return multiply3(imageInv, multiply3(normalized, modelNorm.matrix));
}

export function reprojectionError(H, pair) {
  const projected = applyH(H, pair[0], pair[1]);
  return Math.hypot(projected.x - pair[2], projected.y - pair[3]);
}

/** RANSAC + Huber IRLS refinement; pairs are [unitX, unitY, imageX, imageY]. */
export function refineHomography(pairs, { thresholdPx = 8, iterations = 400,
                                           seed = 12345, minInliers = 6 } = {}) {
  if (pairs.length <= 4) return { H: solveHomography(pairs), inliers: pairs.length,
                                  residual: 0 };
  const random = mulberry32(seed);
  let best = null;
  for (let it = 0; it < iterations; it += 1) {
    const sample = [];
    const used = new Set();
    while (sample.length < 4) {
      const index = Math.floor(random() * pairs.length);
      if (!used.has(index)) { used.add(index); sample.push(pairs[index]); }
    }
    const H = solveHomography(sample);
    if (!H) continue;
    let count = 0;
    for (const pair of pairs) if (reprojectionError(H, pair) < thresholdPx) count += 1;
    if (!best || count > best.count) best = { H, count };
  }
  if (!best || best.count < minInliers) {
    return { H: solveHomography(pairs), inliers: pairs.length, residual: null };
  }
  const inliers = pairs.filter((pair) => reprojectionError(best.H, pair) < thresholdPx);
  let H = solveHomography(inliers);
  for (let it = 0; it < 6; it += 1) {
    const weights = inliers.map((pair) => {
      const residual = reprojectionError(H, pair);
      const scaled = residual / (thresholdPx / 2);
      return scaled <= 1 ? 1 : 1 / scaled;
    });
    H = solveHomography(inliers, weights) || H;
  }
  let sum = 0;
  for (const pair of inliers) sum += reprojectionError(H, pair) ** 2;
  return { H, inliers: inliers.length,
           residual: Math.sqrt(sum / Math.max(1, inliers.length)) };
}

function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a += 0x6D2B79F5;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// ---------------------------------------------------------------------------
// blobs
// ---------------------------------------------------------------------------
export function connectedComponents(diff, width, height, threshold, minArea = 4) {
  const visited = new Uint8Array(diff.length);
  const blobs = [];
  const stack = [];
  for (let start = 0; start < diff.length; start += 1) {
    if (visited[start] || diff[start] < threshold) continue;
    let area = 0, sx = 0, sy = 0, energy = 0, peak = 0;
    let sxx = 0, sxy = 0, syy = 0;
    stack.length = 0;
    stack.push(start);
    visited[start] = 1;
    while (stack.length) {
      const index = stack.pop();
      const x = index % width;
      const y = (index / width) | 0;
      const value = diff[index];
      area += 1;
      energy += value;
      sx += x; sy += y;
      sxx += x * x; sxy += x * y; syy += y * y;
      if (value > peak) peak = value;
      for (let dy = -1; dy <= 1; dy += 1) {
        for (let dx = -1; dx <= 1; dx += 1) {
          const nx = x + dx, ny = y + dy;
          if (nx < 0 || ny < 0 || nx >= width || ny >= height) continue;
          const next = ny * width + nx;
          if (!visited[next] && diff[next] >= threshold) {
            visited[next] = 1;
            stack.push(next);
          }
        }
      }
    }
    if (area >= minArea) {
      blobs.push({ area, energy, peak, cx: sx / area, cy: sy / area,
                   sxx: sxx / area, sxy: sxy / area, syy: syy / area });
    }
  }
  return blobs.map((blob) => {
    const covXX = blob.sxx - blob.cx * blob.cx;
    const covYY = blob.syy - blob.cy * blob.cy;
    const covXY = blob.sxy - blob.cx * blob.cy;
    const trace = covXX + covYY;
    const det = covXX * covYY - covXY * covXY;
    const root = Math.sqrt(Math.max(0, trace * trace / 4 - det));
    const l1 = trace / 2 + root;
    const l2 = Math.max(1e-6, trace / 2 - root);
    return {
      ...blob,
      radius: Math.sqrt(blob.area / Math.PI),
      elongation: Math.sqrt(Math.max(0, l1) / l2),
      concentration: blob.peak / Math.max(1e-9, blob.energy / blob.area),
      // sharp core vs diffuse halo; both measured in the same blob
      haloRatio: (blob.energy + 1e-6) /
        (blob.peak * Math.max(1, blob.area) + 1e-6),
    };
  });
}

export function blobStats(diff, width, height, { noise = 1, absoluteFloor = 4 } = {}) {
  const max = diff.reduce((best, value) => (value > best ? value : best), 0);
  const threshold = Math.max(absoluteFloor, 4 * noise, 0.25 * max);
  if (max < Math.max(absoluteFloor, 3 * noise)) return { blobs: [], peak: max, threshold };
  const blobs = connectedComponents(diff, width, height, threshold);
  let sum = 0;
  for (const blob of blobs) sum += blob.energy;
  for (const blob of blobs) blob.share = blob.energy / (sum || 1);
  return { blobs: blobs.sort((a, b) => b.energy - a.energy), peak: max, threshold };
}

/** Weighted sub-pixel centroid in a window around (cx, cy). */
export function subPixelCentroid(diff, width, height, cx, cy, radius = 3) {
  let sum = 0, sx = 0, sy = 0;
  const x0 = Math.max(0, Math.floor(cx - radius));
  const x1 = Math.min(width - 1, Math.ceil(cx + radius));
  const y0 = Math.max(0, Math.floor(cy - radius));
  const y1 = Math.min(height - 1, Math.ceil(cy + radius));
  let floor = Infinity;
  for (let y = y0; y <= y1; y += 1) {
    for (let x = x0; x <= x1; x += 1) {
      floor = Math.min(floor, diff[y * width + x]);
    }
  }
  for (let y = y0; y <= y1; y += 1) {
    for (let x = x0; x <= x1; x += 1) {
      const weight = Math.max(0, diff[y * width + x] - floor);
      sum += weight * weight;
      sx += weight * weight * x;
      sy += weight * weight * y;
    }
  }
  if (!sum) return { x: cx, y: cy };
  return { x: sx / sum, y: sy / sum };
}

// ---------------------------------------------------------------------------
// perimeter sampling along a path
// ---------------------------------------------------------------------------
export function samplePath(plane, width, height, points, closed, {
  halfWidth = 3, samples = 240,
} = {}) {
  const profile = new Float32Array(samples);
  for (let k = 0; k < samples; k += 1) {
    const t = k / samples;
    const point = pointAt(points, closed, t);
    const nx = -point.ty;
    const ny = point.tx;
    const values = [];
    for (let offset = -halfWidth; offset <= halfWidth; offset += 1) {
      const x = Math.round(point.x + nx * offset);
      const y = Math.round(point.y + ny * offset);
      if (x < 0 || y < 0 || x >= width || y >= height) continue;
      values.push(plane[y * width + x]);
    }
    values.sort((a, b) => a - b);
    // mean of the hottest third: a thin strip two pixels wide must not be
    // erased by a median across the sampling window, while a lone hot pixel
    // still cannot dominate.
    const keep = Math.max(1, Math.ceil(values.length / 3));
    let sum = 0;
    for (let i = values.length - keep; i < values.length; i += 1) sum += values[i];
    profile[k] = values.length ? sum / keep : 0;
  }
  return profile;
}

function pointAt(points, closed, t) {
  const n = points.length;
  if (n < 2) return { x: 0, y: 0, tx: 1, ty: 0 };
  let total = 0;
  const lengths = [];
  const count = closed ? n : n - 1;
  for (let i = 0; i < count; i += 1) {
    const a = points[i], b = points[(i + 1) % n];
    const length = Math.hypot(b[0] - a[0], b[1] - a[1]);
    lengths.push(length);
    total += length;
  }
  let target = (((t % 1) + 1) % 1) * (total || 1);
  for (let i = 0; i < count; i += 1) {
    const a = points[i], b = points[(i + 1) % n];
    const length = lengths[i] || 1e-9;
    if (target <= length || i === count - 1) {
      const u = Math.min(1, Math.max(0, target / length));
      return { x: a[0] + (b[0] - a[0]) * u, y: a[1] + (b[1] - a[1]) * u,
               tx: (b[0] - a[0]) / length, ty: (b[1] - a[1]) / length };
    }
    target -= length;
  }
  return { x: points[0][0], y: points[0][1], tx: 1, ty: 0 };
}

/** Peaks of a 1-D profile, returned as t fractions with widths. */
export function profilePeaks(profile, { minProminence = 0.3, minSeparation = 4 } = {}) {
  const max = Math.max(...profile);
  const min = Math.min(...profile);
  if (max - min < 1e-6) return [];
  const threshold = min + (max - min) * minProminence;
  const peaks = [];
  for (let i = 0; i < profile.length; i += 1) {
    const prev = profile[(i - 1 + profile.length) % profile.length];
    const next = profile[(i + 1) % profile.length];
    if (profile[i] >= threshold && profile[i] >= prev && profile[i] >= next) {
      peaks.push({ index: i, value: profile[i], t: i / profile.length });
    }
  }
  peaks.sort((a, b) => b.value - a.value);
  const kept = [];
  for (const peak of peaks) {
    if (kept.every((other) => circularDistance(other.index / profile.length, peak.t) *
                             profile.length >= minSeparation)) {
      kept.push(peak);
    }
  }
  for (const peak of kept) {
    let left = peak.index;
    let guard = 0;
    while (profile[(left - 1 + profile.length) % profile.length] > threshold && guard < profile.length) {
      left -= 1;
      guard += 1;
    }
    let right = peak.index;
    let guard2 = 0;
    while (profile[(right + 1) % profile.length] > threshold && guard2 < profile.length) {
      right += 1;
      guard2 += 1;
    }
    peak.width = (((right - left + profile.length) % profile.length) + 1) / profile.length;
  }
  return kept.sort((a, b) => a.t - b.t);
}

function circularDistance(a, b) {
  const d = Math.abs(a - b) % 1;
  return Math.min(d, 1 - d);
}

/**
 * Order detected perimeter points by their arc position on a hull, split into
 * strips at large gaps. This is the ordering half of "which base LED is
 * which": the pixel evidence supplies positions, the hull supplies the order.
 */
export function orderOnPath(points, path, closed = true, { gapFraction = 0.12 } = {}) {
  const keyed = points.map((point) => {
    const { x, y } = point;
    const projection = projectOnPath(path, closed, x, y);
    return { ...point, t: projection.t, dist: projection.dist };
  });
  keyed.sort((a, b) => a.t - b.t);
  const gaps = [];
  for (let i = 0; i < keyed.length; i += 1) {
    const next = keyed[(i + 1) % keyed.length];
    const gap = (((next.t - keyed[i].t) % 1) + 1) % 1;
    gaps.push({ index: i, gap });
  }
  const bigGaps = gaps.filter((entry) => entry.gap > gapFraction);
  if (!closed || !bigGaps.length) {
    return [{ points: keyed, closed }];
  }
  const start = bigGaps.reduce((best, entry) => (entry.gap > best.gap ? entry : best)).index;
  const rotated = [...keyed.slice(start + 1), ...keyed.slice(0, start + 1)];
  const strips = [];
  let current = [];
  for (let i = 0; i < rotated.length; i += 1) {
    current.push(rotated[i]);
    const next = rotated[(i + 1) % rotated.length];
    const gap = (((next.t - rotated[i].t) % 1) + 1) % 1;
    if (gap > gapFraction) { strips.push(current); current = []; }
  }
  if (current.length) strips.push(current);
  // normalise t within each open strip so the UI uses 0..1 per strip
  return strips.map((strip) => {
    const t0 = strip[0].t;
    let span = (((strip[strip.length - 1].t - t0) % 1) + 1) % 1;
    if (!span) span = 1;
    return {
      points: strip.map((point) => ({ ...point,
        t: ((((point.t - t0) % 1) + 1) % 1) / span })),
      closed: false,
    };
  });
}

function projectOnPath(points, closed, x, y) {
  const n = points.length;
  let total = 0;
  const lengths = [];
  const count = closed ? n : n - 1;
  for (let i = 0; i < count; i += 1) {
    const a = points[i], b = points[(i + 1) % n];
    const length = Math.hypot(b[0] - a[0], b[1] - a[1]);
    lengths.push(length);
    total += length;
  }
  let best = { t: 0, dist: Infinity };
  let run = 0;
  for (let i = 0; i < count; i += 1) {
    const a = points[i], b = points[(i + 1) % n];
    const dx = b[0] - a[0], dy = b[1] - a[1];
    const length = lengths[i] || 1e-9;
    const u = Math.min(1, Math.max(0, ((x - a[0]) * dx + (y - a[1]) * dy) /
                                      (length * length)));
    const px = a[0] + dx * u, py = a[1] + dy * u;
    const dist = Math.hypot(x - px, y - py);
    if (dist < best.dist) best = { t: (run + u * length) / (total || 1), dist };
    run += length;
  }
  return best;
}

/** Expand a rectangle outward for the case path. */
export function rectPath(x, y, w, h) {
  return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]];
}
