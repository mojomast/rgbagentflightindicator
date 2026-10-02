// A minimal observable store: immutable snapshots, synchronous subscribers.

export function createStore(initial) {
  let state = initial;
  const subscribers = new Set();
  return {
    get() { return state; },
    set(patch) {
      const next = typeof patch === "function" ? patch(state) : { ...state, ...patch };
      if (next === state) return;
      state = next;
      for (const fn of [...subscribers]) fn(state);
    },
    subscribe(fn) {
      subscribers.add(fn);
      return () => subscribers.delete(fn);
    },
  };
}
