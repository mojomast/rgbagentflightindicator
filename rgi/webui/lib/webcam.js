// Webcam assist for mapping: find where a lamp appears in the camera image.
//
// The camera can only say *where* light appeared; naming the key is a human
// job (press-to-label). The pipeline is deliberately small: lock exposure if
// the camera allows it, capture a dark baseline, then for each lamp capture a
// lit frame and find the brightest positive blob. No CV library, no network.
//
// Pure functions are exported so node --test can exercise the maths without a
// browser.

export function lumaAt(rgba, offset) {
  return 0.2126 * rgba[offset] + 0.7152 * rgba[offset + 1] + 0.0722 * rgba[offset + 2];
}

/** Positive-only difference of two luma arrays; negative values become 0. */
export function frameDiff(lit, dark) {
  const out = new Float32Array(lit.length);
  for (let i = 0; i < lit.length; i += 1) {
    const delta = lit[i] - dark[i];
    out[i] = delta > 0 ? delta : 0;
  }
  return out;
}

export function median(values) {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

/**
 * Find the strongest blob in a difference image.
 * Returns null when nothing rises meaningfully above the noise floor.
 */
export function findBlob(diff, width, height, { minPeak = 8, snr = 4 } = {}) {
  let peak = 0;
  let peakIndex = -1;
  for (let i = 0; i < diff.length; i += 1) {
    if (diff[i] > peak) { peak = diff[i]; peakIndex = i; }
  }
  if (peakIndex < 0 || peak < minPeak) return null;

  // Noise floor: the median of the non-positive half is unhelpful here (all
  // values are clamped to 0), so use the same image's low quantile away from
  // the peak instead.
  const sample = [];
  const stride = Math.max(1, Math.floor(diff.length / 4000));
  for (let i = 0; i < diff.length; i += stride) {
    if (Math.abs(i - peakIndex) > width * 2) sample.push(diff[i]);
  }
  const floor = median(sample);
  const threshold = Math.max(minPeak, peak * 0.5, floor * snr);

  let sum = 0, sx = 0, sy = 0, count = 0;
  let minX = width, minY = height, maxX = 0, maxY = 0;
  for (let i = 0; i < diff.length; i += 1) {
    if (diff[i] < threshold) continue;
    const x = i % width;
    const y = Math.floor(i / width);
    // stay near the peak so a neighbouring key cannot drag the centroid away
    if (Math.abs(x - (peakIndex % width)) > width * 0.25) continue;
    sum += diff[i];
    sx += diff[i] * x;
    sy += diff[i] * y;
    count += 1;
    if (x < minX) minX = x;
    if (y < minY) minY = y;
    if (x > maxX) maxX = x;
    if (y > maxY) maxY = y;
  }
  if (!count || sum <= 0) return null;
  const confidence = Math.min(1, (peak / Math.max(1, floor * snr)) / 4);
  return {
    x: sx / sum,
    y: sy / sum,
    nx: (sx / sum) / width,
    ny: (sy / sum) / height,
    peak,
    floor,
    pixels: count,
    width: maxX - minX + 1,
    height: maxY - minY + 1,
    confidence,
  };
}

export class WebcamMapper {
  constructor(video) {
    this.video = video;
    this.stream = null;
    this.canvas = document.createElement("canvas");
    this.ctx = this.canvas.getContext("2d", { willReadFrequently: true });
    this.width = 320;
    this.height = 240;
    this.dark = null;
    this.exposure = "unknown";
  }

  static get supported() {
    return Boolean(navigator.mediaDevices?.getUserMedia) && window.isSecureContext;
  }

  static get reason() {
    if (!window.isSecureContext) {
      return "camera access needs a secure context: open the page at http://localhost:8730/ui/ (a LAN address will not work)";
    }
    return "this browser has no getUserMedia";
  }

  async listCameras() {
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      return devices.filter((d) => d.kind === "videoinput");
    } catch {
      return [];
    }
  }

  async start(deviceId) {
    const constraints = {
      video: {
        width: { ideal: 1280 },
        height: { ideal: 720 },
        ...(deviceId ? { deviceId: { exact: deviceId } } : {}),
      },
    };
    this.stream = await navigator.mediaDevices.getUserMedia(constraints);
    this.video.srcObject = this.stream;
    await this.video.play();
    await new Promise((resolve) => {
      if (this.video.videoWidth) resolve();
      else this.video.addEventListener("loadedmetadata", resolve, { once: true });
    });
    this.width = Math.min(360, this.video.videoWidth || 320);
    this.height = Math.max(1, Math.round(this.width * (this.video.videoHeight || 240) / (this.video.videoWidth || 320)));
    this.canvas.width = this.width;
    this.canvas.height = this.height;
    this.exposure = await this.lockExposure();
    return this.exposure;
  }

  async lockExposure() {
    const track = this.stream?.getVideoTracks?.()[0];
    if (!track) return "no track";
    const caps = track.getCapabilities ? track.getCapabilities() : {};
    if (!caps.exposureMode || !caps.exposureMode.includes("manual")) {
      return "auto (camera does not offer manual exposure)";
    }
    try {
      await track.applyConstraints({ advanced: [{ exposureMode: "manual" }] });
      if (caps.exposureTime) {
        const target = Math.min(Math.max(100, caps.exposureTime.min ?? 100), caps.exposureTime.max ?? 1000);
        await track.applyConstraints({ advanced: [{ exposureTime: target }] });
      }
      if (caps.whiteBalanceMode && caps.whiteBalanceMode.includes("manual")) {
        await track.applyConstraints({ advanced: [{ whiteBalanceMode: "manual" }] });
      }
      return "locked";
    } catch (err) {
      return `auto (lock failed: ${err.message})`;
    }
  }

  stop() {
    this.stream?.getTracks?.().forEach((track) => track.stop());
    this.stream = null;
    this.video.srcObject = null;
  }

  captureLuma() {
    this.ctx.drawImage(this.video, 0, 0, this.width, this.height);
    const { data } = this.ctx.getImageData(0, 0, this.width, this.height);
    const out = new Float32Array(this.width * this.height);
    for (let i = 0; i < out.length; i += 1) out[i] = lumaAt(data, i * 4);
    return out;
  }

  async medianLuma(frames = 3) {
    const captured = [];
    for (let i = 0; i < frames; i += 1) {
      captured.push(this.captureLuma());
      // one video frame between captures; enough for 10-15 fps pipelines
      await new Promise((resolve) => setTimeout(resolve, 45));
    }
    const out = new Float32Array(captured[0].length);
    for (let i = 0; i < out.length; i += 1) out[i] = median(captured.map((frame) => frame[i]));
    return out;
  }

  async captureDark(frames = 5) {
    this.dark = await this.medianLuma(frames);
    return this.dark;
  }

  async detect() {
    const lit = await this.medianLuma(3);
    if (!this.dark) return null;
    return findBlob(frameDiff(lit, this.dark), this.width, this.height);
  }
}
