// Webcam capture for the Identify wizard.
//
// Capture is deliberately simple and local: one hidden canvas at a working
// resolution (640 px long side), RGB planes, medians over a few frames. The
// maths lives in cv.js so it can be unit-tested without a camera. Frames never
// leave the machine; the stream is stopped the moment the wizard is left.

import { medianPlanes, rgbPlanes, lumaPlane } from "./cv.js";

export const WORKING_WIDTH = 640;

export class WebcamMapper {
  constructor(video) {
    this.video = video;
    this.stream = null;
    this.canvas = document.createElement("canvas");
    this.ctx = this.canvas.getContext("2d", { willReadFrequently: true });
    this.width = WORKING_WIDTH;
    this.height = 360;
    this.exposure = "unknown";
  }

  static get supported() {
    return Boolean(navigator.mediaDevices?.getUserMedia) && window.isSecureContext;
  }

  static get reason() {
    if (!window.isSecureContext) {
      return "camera access needs a secure context: open the page at " +
        "http://localhost:8730/ui/ (a LAN address will not work)";
    }
    return "this browser has no getUserMedia";
  }

  async listCameras() {
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      return devices.filter((device) => device.kind === "videoinput");
    } catch {
      return [];
    }
  }

  async start(deviceId) {
    this.stream = await navigator.mediaDevices.getUserMedia({
      video: {
        width: { ideal: 1280 },
        height: { ideal: 720 },
        frameRate: { ideal: 30 },
        ...(deviceId ? { deviceId: { exact: deviceId } } : {}),
      },
    });
    this.video.srcObject = this.stream;
    await this.video.play();
    await new Promise((resolve) => {
      if (this.video.videoWidth) resolve();
      else this.video.addEventListener("loadedmetadata", resolve, { once: true });
    });
    const videoWidth = this.video.videoWidth || 640;
    this.width = Math.min(WORKING_WIDTH, videoWidth);
    this.height = Math.max(1, Math.round(this.width * (this.video.videoHeight || 360) /
      videoWidth));
    this.canvas.width = this.width;
    this.canvas.height = this.height;
    this.exposure = await this.lockExposure();
    return { exposure: this.exposure, width: this.width, height: this.height };
  }

  async lockExposure() {
    const track = this.stream?.getVideoTracks?.()[0];
    if (!track) return "no track";
    const capabilities = track.getCapabilities ? track.getCapabilities() : {};
    if (!capabilities.exposureMode || !capabilities.exposureMode.includes("manual")) {
      return "auto (camera does not offer manual exposure)";
    }
    try {
      await track.applyConstraints({ advanced: [{ exposureMode: "manual" }] });
      if (capabilities.exposureTime) {
        const target = Math.min(Math.max(100, capabilities.exposureTime.min ?? 100),
                                capabilities.exposureTime.max ?? 1000);
        await track.applyConstraints({ advanced: [{ exposureTime: target }] });
      }
      if (capabilities.whiteBalanceMode?.includes("manual")) {
        await track.applyConstraints({ advanced: [{ whiteBalanceMode: "manual" }] });
      }
      if (capabilities.focusMode?.includes("manual")) {
        await track.applyConstraints({ advanced: [{ focusMode: "manual" }] });
      }
      return "locked";
    } catch (err) {
      return `auto (lock failed: ${err.message})`;
    }
  }

  stop() {
    this.stream?.getTracks?.().forEach((track) => track.stop());
    this.stream = null;
    if (this.video) this.video.srcObject = null;
  }

  /** One frame as channel planes at the working resolution. */
  grab() {
    this.ctx.drawImage(this.video, 0, 0, this.width, this.height);
    const image = this.ctx.getImageData(0, 0, this.width, this.height);
    return { planes: rgbPlanes(image.data, this.width, this.height),
             luma: lumaPlane(image.data, this.width, this.height),
             width: this.width, height: this.height };
  }

  async medianGrab(frames = 3, gapMs = 40) {
    const grabs = [];
    for (let i = 0; i < frames; i += 1) {
      grabs.push(this.grab());
      if (i + 1 < frames) await wait(gapMs);
    }
    return {
      planes: {
        r: medianPlanes(grabs.map((entry) => entry.planes.r)),
        g: medianPlanes(grabs.map((entry) => entry.planes.g)),
        b: medianPlanes(grabs.map((entry) => entry.planes.b)),
      },
      luma: medianPlanes(grabs.map((entry) => entry.luma)),
      width: this.width,
      height: this.height,
    };
  }
}

/** Pure classification of one lamp's evidence, shared with the tests. */
export function classifyEvidence({ dCell, dPath, elongation = 1, compact = true,
                                   insideCase = true, hasCell = true } = {}) {
  if (hasCell && dCell != null && dCell <= 0.45 && compact && elongation <= 2.5) {
    return "key";
  }
  if (dPath != null && dPath <= 0.4) return "perimeter";
  if (insideCase) return "logo";
  return "unknown";
}

export function confidenceFor({ snr = 0, dCell = 1, margin = 0, compact = true } = {}) {
  const score = 0.45 * Math.min(1, Math.max(0, (snr - 2) / 10)) +
    0.35 * Math.max(0, 1 - dCell / 0.5) +
    0.2 * (compact ? 1 : 0.4) +
    0.1 * Math.min(1, Math.max(0, margin / 0.25));
  return Math.max(0.05, Math.min(1, score));
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
