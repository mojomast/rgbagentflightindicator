// Mimic what opencode's V2 TUI plugin loader does, so we can find out whether
// the module is valid WITHOUT restarting the whole TUI.
//
//   loader: const mod = await import(entrypoint)
//           if (!isPlugin(mod.default)) throw new Error("Invalid V2 TUI plugin module: " + spec)
//           then: setup(context) once, return value = cleanup
//
// Run:  node check-tui-plugin.mjs

import { pathToFileURL } from "node:url"

// Point this at the plugin you want to check:
//   PLUGIN_ENTRY=~/.config/opencode/plugins/rgi-panel/tui.ts node check_tui_plugin.mjs
// There is no default: this used to carry an absolute path from one machine, and
// that is exactly the kind of thing that should not be committed.
const ENTRY = process.env.PLUGIN_ENTRY
if (!ENTRY) {
  console.log("set PLUGIN_ENTRY to the tui.ts you want to check, e.g.")
  console.log("  PLUGIN_ENTRY=~/.config/opencode/plugins/rgi-panel/tui.ts node check_tui_plugin.mjs")
  process.exit(2)
}

// What the beta loader accepts, per the opencode v2 docs and the
// oh-my-opencode-slim PR #1058 write-up.
function isPlugin(value) {
  return (
    !!value &&
    typeof value === "object" &&
    typeof value.id === "string" &&
    typeof value.setup === "function"
  )
}

console.log("importing", ENTRY)
let mod
try {
  mod = await import(pathToFileURL(ENTRY).href)
} catch (err) {
  console.log("IMPORT FAILED:", err?.code ?? "", err?.message ?? err)
  process.exit(1)
}

console.log("import ok; keys:", Object.keys(mod).join(", ") || "(none)")
const def = mod.default

if (!isPlugin(def)) {
  console.log("*** INVALID: default export is not { id: string, setup: function }")
  console.log("    got:", def === undefined ? "undefined" : JSON.stringify(Object.keys(def ?? {})))
  process.exit(1)
}

console.log(`shape OK -> { id: ${JSON.stringify(def.id)}, setup: function }`)

// Now drive setup() with a stub context, the way the host would.
const registered = []
const disposed = []

const stub = {
  app: { version: "test", channel: "test" },
  location: undefined,
  options: {},
  renderer: { requestRender: () => console.log("    [host] requestRender()") },
  theme: { text: { base: "#ffffff", default: "#ffffff" } },
  data: {
    session: {
      get: (id) => ({ id, title: "stub title for " + id }),
    },
  },
  ui: {
    slot: (claim) => {
      const placements = ["append", "prepend", "before", "after", "replace"].filter(
        (k) => k in claim,
      )
      if (placements.length !== 1) {
        throw new Error("Slot claim requires exactly one placement key")
      }
      console.log(`    [slot] ${placements[0]} ${claim[placements[0]]}`)
      registered.push(claim)
      return () => {
        disposed.push(claim[placements[0]])
        console.log(`    [slot] disposed ${claim[placements[0]]}`)
      }
    },
  },
}

let cleanup
try {
  cleanup = def.setup(stub)
  console.log("setup() returned:", typeof cleanup)
} catch (err) {
  console.log("*** setup() THREW:", err?.message ?? err)
  process.exit(1)
}

await new Promise((r) => setTimeout(r, 1500))

console.log("slots registered:", registered.length)
if (registered.length === 0) {
  console.log("*** WARNING: nothing was registered - the sidebar would stay empty")
}

// Exercise the render functions, since a throw inside one would blank the UI.
const SID = "ses_f13a567e7ffeFUdoSZ9cWtgI47"
for (const claim of registered) {
  const name = ["append", "prepend", "before", "after", "replace"].find((k) => k in claim)
  try {
    const el = claim.render({ sessionID: SID, name: "session.panel" })
    // jsx() returns a string when no renderer is installed, or an element whose
    // children getter yields the text - read either, so this shows what the
    // sidebar would actually say
    let text = null
    if (typeof el === "string") text = el
    else if (el && typeof el === "object") {
      try {
        const c = el.children
        text = typeof c === "function" ? c() : c
      } catch {
        /* leave it null */
      }
    }
    if (typeof text === "string") {
      console.log(`  render(${claim[name]}) ->`)
      for (const l of text.split("\n")) console.log("      | " + l)
    } else {
      console.log(`  render(${claim[name]}) -> element (no readable children)`)
    }
  } catch (err) {
    console.log(`  *** render(${claim[name]}) THREW:`, err?.message ?? err)
  }
}

if (typeof cleanup === "function") {
  try {
    cleanup()
    console.log("cleanup() ok; disposed:", disposed.length)
  } catch (err) {
    console.log("*** cleanup() THREW:", err?.message ?? err)
  }
}
console.log("DONE")
