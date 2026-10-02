// Structural diff between the applied config and the draft: the honest number
// the dirty bar shows, and the sections the Apply review lists.

const SECTION_LABELS = {
  settings: "Settings",
  appearance: "Appearance",
  devices: "Devices",
  layouts: "Layouts",
  profiles: "Profiles",
  lanes: "Lane preferences",
  lamp_overrides: "Lamp overrides",
  zone_overrides: "Zone overrides",
  agents: "Agents",
  endpoints: "Endpoints",
  schema_version: "Schema",
  revision: "Revision",
  updated_at: "Updated",
};

export function diffCount(a, b) {
  return leafChanges(a, b, "");
}

function leafChanges(a, b, path) {
  if (a === b) return 0;
  const typeA = a === null ? "null" : Array.isArray(a) ? "array" : typeof a;
  const typeB = b === null ? "null" : Array.isArray(b) ? "array" : typeof b;
  if (typeA !== typeB) return 1;
  if (typeA === "object") {
    const keys = new Set([...Object.keys(a || {}), ...Object.keys(b || {})]);
    let count = 0;
    for (const key of keys) count += leafChanges(a?.[key], b?.[key], path ? `${path}.${key}` : key);
    return count;
  }
  if (typeB === "array") {
    const length = Math.max(a.length, b.length);
    let count = 0;
    for (let i = 0; i < length; i += 1) {
      count += leafChanges(a[i], b[i], `${path}[${i}]`);
    }
    return count;
  }
  return 1;
}

/** Top-level sections that differ, with a count each, for the review dialog. */
export function diffSections(a, b) {
  const out = [];
  for (const key of new Set([...Object.keys(a || {}), ...Object.keys(b || {})])) {
    const left = (a || {})[key];
    const right = (b || {})[key];
    if (JSON.stringify(left) === JSON.stringify(right)) continue;
    out.push({ key, label: SECTION_LABELS[key] || key,
               count: leafChanges(left, right, key) });
  }
  return out.sort((x, y) => y.count - x.count);
}
