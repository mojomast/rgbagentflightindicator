// Server half of the OpenCode plugin.
//
// Nothing needs to run on the server: the panel is driven by `rgi watch`, which
// polls OpenCode's own API and reports lanes to the daemon, and the TUI half
// (tui.ts) draws them. This module exists so the plugin directory has the
// documented layout and a valid V2 server shape: { id, setup }.

export default {
  id: "rgi.panel",
  setup() {
    // intentionally empty
  },
}
