// Draft history for undo/redo. Snapshots of the (small) config object, not a
// command pattern: the whole file is a few KB and correctness beats elegance.
// Rapid same-field edits coalesce so one undo reverses one gesture.

export function deepClone(value) {
  if (typeof structuredClone === "function") return structuredClone(value);
  return JSON.parse(JSON.stringify(value));
}

export function getPath(obj, path) {
  return path.split(".").reduce((node, key) => (node == null ? undefined : node[key]), obj);
}

export function setPath(obj, path, value) {
  const keys = path.split(".");
  const last = keys.pop();
  let node = obj;
  for (const key of keys) {
    if (typeof node[key] !== "object" || node[key] === null) node[key] = {};
    node = node[key];
  }
  node[last] = value;
  return obj;
}

export function createHistory(limit = 60) {
  const past = [];
  const future = [];
  let lastKey = null;
  let lastAt = 0;

  return {
    push(snapshot, label, key = null) {
      const now = Date.now();
      if (key && key === lastKey && now - lastAt < 700 && past.length) {
        // coalesce a gesture: keep the snapshot from before it started,
        // only the label moves
        past[past.length - 1].label = label;
      } else {
        past.push({ snapshot, label });
        if (past.length > limit) past.shift();
      }
      future.length = 0;
      lastKey = key;
      lastAt = now;
    },
    undo(current) {
      const entry = past.pop();
      if (!entry) return null;
      future.push({ snapshot: current, label: entry.label });
      return entry.snapshot;
    },
    redo(current) {
      const entry = future.pop();
      if (!entry) return null;
      past.push({ snapshot: current, label: entry.label });
      return entry.snapshot;
    },
    canUndo() { return past.length > 0; },
    canRedo() { return future.length > 0; },
  };
}
