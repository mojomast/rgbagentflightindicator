// Colour math for the editor: parse, contrast, and colour-blind simulation.
// The same maths runs in the warnings the user sees, so the advice matches
// what a viewer will actually experience.

export function hexToRgb(hex) {
  if (typeof hex !== "string") return [0, 0, 0];
  let text = hex.trim().replace(/^#/, "");
  if (text.length === 3) text = text.split("").map((c) => c + c).join("");
  if (!/^[0-9a-fA-F]{6}$/.test(text)) return [0, 0, 0];
  const n = parseInt(text, 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

export function rgbToHex(rgb) {
  const part = (c) => Math.max(0, Math.min(255, Math.round(c))).toString(16).padStart(2, "0");
  return `#${part(rgb[0])}${part(rgb[1])}${part(rgb[2])}`;
}

export function luminance([r, g, b]) {
  const channel = (value) => {
    const c = value / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

export function contrast(a, b) {
  const la = luminance(a), lb = luminance(b);
  const [hi, lo] = la >= lb ? [la, lb] : [lb, la];
  return (hi + 0.05) / (lo + 0.05);
}

export function readableOn(rgb) {
  return luminance(rgb) > 0.35 ? "#000000" : "#ffffff";
}

// Approximate dichromacy transforms (Viénot/Brettel style matrices). Advisory
// only: the icons and labels are the required non-colour channel.
const SIMULATIONS = {
  protanopia: [[0.152, 1.053, -0.205], [0.115, 0.786, 0.099], [-0.004, -0.048, 1.052]],
  deuteranopia: [[0.367, 0.861, -0.228], [0.280, 0.673, 0.047], [-0.012, 0.043, 0.969]],
  tritanopia: [[1.256, -0.077, -0.179], [-0.078, 0.931, 0.148], [0.005, -0.015, 1.010]],
};

export function simulate(rgb, kind) {
  const matrix = SIMULATIONS[kind];
  if (!matrix) return rgb.slice();
  const [r, g, b] = rgb;
  const out = matrix.map((row) => row[0] * r + row[1] * g + row[2] * b);
  return out.map((c) => Math.max(0, Math.min(255, Math.round(c))));
}

export function differsEnough(a, b, threshold = 30) {
  const distance = Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);
  return distance >= threshold;
}
