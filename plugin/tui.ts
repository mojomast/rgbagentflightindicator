// OpenCode TUI half: show the panel's lanes inside the session sidebar.
//
// Hard-won facts about the host (OpenCode v2.x) - each of these was a real
// failure before it worked:
//
//   1. The default export MUST be { id, setup }. The older v1 shape
//      { id, tui(...) } is rejected with "Invalid V2 TUI plugin module".
//      The loader validates structurally, calls setup(context) once, and treats
//      the returned function as cleanup.
//
//   2. The entrypoint is `tui.ts` and it is built WITHOUT a JSX transform, so
//      elements are made with jsx() imported from the runtime rather than with
//      JSX syntax. (JSX inside a .ts file fails with "N errors building tui.ts".)
//
//   3. Slots are claimed with exactly one placement key:
//      context.ui.slot({ append: "sidebar.content", render }).
//
//   4. OpenCode imports external plugins *before* installing OpenTUI's Solid
//      runtime support, so a plugin that renders JSX meets "No renderer found"
//      and the host then throws the plugin away. So install the support
//      ourselves, then import the JSX runtime dynamically.
//
//   5. Never return a value the host cannot mount. If there is no JSX runtime,
//      render null: a blank panel survives, a bad value gets the plugin disposed.

import { createSignal } from "solid-js"
import { appendFileSync, readFileSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"

const HOME = homedir()
const CONFIG = join(HOME, ".config", "rgi")
const PANEL = process.env.RGI_URL ?? process.env.LEDD_URL ?? "http://127.0.0.1:8730"
const POLL_MS = 1000
const LOG = join(CONFIG, "plugin.log")

// What the sidebar block calls itself. Keep in step with VERSION_LABEL in
// rgi/__init__.py and the version in pyproject.toml.
const VERSION_LABEL = "rbgafi v0.3"

function loadToken(): string {
  if (process.env.RGI_TOKEN) return process.env.RGI_TOKEN
  if (process.env.LEDD_TOKEN) return process.env.LEDD_TOKEN
  try {
    return readFileSync(join(CONFIG, "token"), "utf8").trim()
  } catch {
    return ""
  }
}

const TOKEN = loadToken()

function log(msg: string) {
  try {
    appendFileSync(LOG, `${new Date().toISOString()} ${msg}\n`)
  } catch {
    /* logging must never break the TUI */
  }
}

let jsxFailureLogged = false

let jsxFn: ((type: string, props?: Record<string, unknown>) => unknown) | undefined

async function prepareRuntime() {
  try {
    const configure: any = await import("@opentui/solid/runtime-plugin-support/configure")
    if (typeof configure.ensureRuntimePluginSupport === "function") {
      configure.ensureRuntimePluginSupport()
      log("OpenTUI runtime support installed")
    }
  } catch (err: any) {
    log(`could not install OpenTUI runtime support: ${err?.message ?? err}`)
  }
  try {
    const runtime: any = await import("@opentui/solid/jsx-runtime")
    jsxFn = runtime.jsx ?? runtime.jsxs
    log(jsxFn ? "jsx runtime ready" : "jsx-runtime module has no jsx export")
  } catch (err: any) {
    log(`jsx runtime unavailable: ${err?.message ?? err}`)
  }
}

// loaded with `await import()`, so top-level await is available
await prepareRuntime()

// Same vocabulary as the panel itself.
const MARKS: Record<string, string> = {
  working: "\u25B6",
  done: "\u2714",
  blocked: "!",
  error: "\u2716",
  idle: "\u00B7",
}

type Lane = {
  id: string
  slot?: number
  key?: string
  agent?: string
  label?: string
  host?: string
  state?: string
}

function trim(text: string, max: number): string {
  const clean = text.replace(/\s+/g, " ").trim()
  return clean.length > max ? clean.slice(0, max - 1) + "\u2026" : clean
}

export default {
  id: "rgi.panel",

  setup(context: any) {
    log(`setup() called (panel=${PANEL}, token=${TOKEN ? "yes" : "no"})`)

    const [lanes, setLanes] = createSignal<Lane[]>([])
    const [online, setOnline] = createSignal(false)

    const poll = async () => {
      try {
        const res = await fetch(`${PANEL}/status`, {
          headers: TOKEN ? { "X-LED-Token": TOKEN } : {},
        })
        const data: any = await res.json()
        const list: Lane[] = Object.entries(data.sessions ?? {}).map(
          ([id, v]: [string, any]) => ({ id, ...v }),
        )
        list.sort((a, b) => Number(a.slot ?? 99) - Number(b.slot ?? 99))
        setOnline(true)
        setLanes(list)
      } catch {
        setOnline(false)
      }
      try {
        context.renderer?.requestRender?.()
      } catch {
        /* ignore */
      }
    }

    void poll()
    const timer = setInterval(() => void poll(), POLL_MS)

    const sessionTitle = (id: string): string | undefined => {
      try {
        const title = context.data?.session?.get?.(id)?.title
        return typeof title === "string" && title.trim() ? title : undefined
      } catch {
        return undefined
      }
    }

    const name = (l: Lane) => trim(sessionTitle(l.id) ?? l.label ?? l.agent ?? l.id, 14)
    const HOST_W = 12
    const host = (l: Lane) =>
      l.host ? trim(l.host, HOST_W).padEnd(HOST_W) : " ".repeat(HOST_W)

    const sidebarText = (sessionID?: string) => {
      if (!online()) return `\u2328 ${VERSION_LABEL} \u00b7 offline`
      const all = lanes()
      if (!all.length) return `\u2328 ${VERSION_LABEL}\n  no lanes claimed`
      const lines = all.map((l) => {
        const here = sessionID && l.id === sessionID
        const mark = MARKS[l.state ?? "idle"] ?? "?"
        return `${here ? "\u25B8" : " "} ${String(l.key ?? "?").padStart(2)} ${mark} ${host(l)} ${name(l)}`
      })
      return `\u2328 ${VERSION_LABEL}\n` + lines.join("\n")
    }

    const footerText = () => {
      if (!online()) return `\u2328 ${VERSION_LABEL} offline`
      const all = lanes()
      if (!all.length) return `\u2328 ${VERSION_LABEL}`
      return "\u2328 " + all.map((l) => `${l.key ?? "?"}${MARKS[l.state ?? "idle"] ?? "?"}`).join(" ")
    }

    const themeFg = (): string => {
      try {
        const t = context.theme?.text
        return t?.base ?? t?.default ?? t?.normal ?? "#ffffff"
      } catch {
        return "#ffffff"
      }
    }

    // `get children()` is what the Solid compiler emits for <text>{expr}</text>,
    // and what makes the text update in place.
    const line = (get: () => string) => {
      if (!jsxFn) return null
      try {
        return jsxFn("text", {
          get children() {
            return get()
          },
          fg: themeFg(),
        })
      } catch (err: any) {
        if (!jsxFailureLogged) {
          jsxFailureLogged = true
          log(`jsx() failed: ${err?.message ?? err}`)
        }
        return null
      }
    }

    const disposers: Array<() => void> = []
    const place = (slot: string, render: (props: any) => unknown) => {
      try {
        const off = context.ui.slot({ append: slot, render })
        if (typeof off === "function") disposers.push(off)
        log(`slot registered: ${slot}`)
      } catch (err: any) {
        log(`slot FAILED: ${slot} -> ${err?.message ?? err}`)
      }
    }

    place("sidebar.content", ({ sessionID }: any) => line(() => sidebarText(sessionID)))
    place("home.footer.status", () => line(footerText))

    return () => {
      clearInterval(timer)
      for (const off of disposers) {
        try {
          off()
        } catch {
          /* ignore */
        }
      }
      log("disposed")
    }
  },
}
