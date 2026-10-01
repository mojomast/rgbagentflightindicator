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
const VERSION_LABEL = "rbgafi v0.4"

// Details are hidden by default so the block stays one line per lane. Alt+L or
// /lanes toggles them; RGI_EXPAND=1 starts expanded.
const START_EXPANDED = process.env.RGI_EXPAND === "1"

function compact(value: unknown): string {
  const n = typeof value === "number" ? value : Number(value)
  if (!isFinite(n) || n === 0) return "-"
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${Math.round(n / 1_000)}k`
  return String(Math.round(n))
}

function duration(seconds: unknown): string {
  const s = typeof seconds === "number" ? seconds : Number(seconds)
  if (!isFinite(s) || s < 0) return "-"
  if (s < 60) return `${Math.round(s)}s`
  if (s < 3600) return `${Math.floor(s / 60)}m${String(Math.round(s % 60)).padStart(2, "0")}`
  return `${Math.floor(s / 3600)}h${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}`
}

function money(value: unknown): string {
  const n = typeof value === "number" ? value : Number(value)
  return isFinite(n) && n > 0 ? `$${n.toFixed(2)}` : ""
}

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
  ident?: string
  state?: string
  in_flight_s?: number
  idle_s?: number
  info?: {
    repo?: string
    branch?: string
    directory?: string
    tokens?: Record<string, number>
    context?: Record<string, number>
    blocked_on?: { action?: string; resources?: string[]; message?: string }
    children?: Array<{ id: string; label?: string; state?: string; tokens?: number }>
    [key: string]: unknown
  }
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
    // Detail is per lane and toggled by clicking it; alt+l (or /lanes) does all.
    const [openLanes, setOpenLanes] = createSignal<string[]>([])
    const [allOpen, setAllOpen] = createSignal(START_EXPANDED)

    const isOpen = (lane: Lane) => allOpen() || openLanes().includes(lane.id)
    const repaint = () => {
      try {
        context.renderer?.requestRender?.()
      } catch {
        /* ignore */
      }
    }
    const toggleLane = (id: string) => {
      const open = openLanes()
      setOpenLanes(open.includes(id) ? open.filter((x) => x !== id) : [...open, id])
      repaint()
    }
    const toggleDetails = () => {
      setAllOpen(!allOpen())
      repaint()
    }

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

    const name = (l: Lane) => trim(sessionTitle(l.id) ?? l.label ?? l.agent ?? l.id, 24)
    const HOST_W = 12

    // What an uncollapsed lane shows: who and where it is, then repository,
    // timings, tokens, context, what it is blocked on, and its subagents.
    const detailLines = (l: Lane, prefix: string): string[] => {
      const lines: string[] = []
      const info = l.info ?? {}

      const who = [
        l.ident ? `id ${trim(l.ident, 18)}` : "",
        l.host ? `host ${trim(l.host, HOST_W)}` : "",
        l.agent ? `agent ${trim(l.agent, 10)}` : "",
      ].filter(Boolean)
      if (who.length) lines.push(`${prefix}${who.join(" · ")}`)

      const where = [info.repo, info.branch].filter(Boolean).join(" @ ")
      if (where) lines.push(`${prefix}repo ${trim(where, 30)}`)
      else if (info.directory) lines.push(`${prefix}dir ${trim(String(info.directory), 30)}`)

      // "in flight" is the current action and nothing else: a lane that has landed
      // has no in-flight time, so this line only appears while something is
      // actually running or waiting. Idle is how long since it last said anything.
      if (l.in_flight_s !== null && l.in_flight_s !== undefined) {
        lines.push(`${prefix}in flight ${duration(l.in_flight_s)} · idle ${duration(l.idle_s)}`)
      } else {
        lines.push(`${prefix}idle ${duration(l.idle_s)}`)
      }

      const tokens = info.tokens ?? {}
      const spend = money(tokens.cost)
      lines.push(
        `${prefix}tokens ${compact(tokens.input)} in / ${compact(tokens.output)} out` +
          (spend ? ` · ${spend}` : ""),
      )
      if (tokens.cache_read) lines.push(`${prefix}cache ${compact(tokens.cache_read)} read`)
      if (tokens.reasoning) lines.push(`${prefix}reasoning ${compact(tokens.reasoning)}`)

      const context = info.context ?? {}
      if (context.percent !== undefined && context.limit) {
        lines.push(`${prefix}context ${context.percent}% of ${compact(context.limit)}`)
      } else if (context.entries) {
        lines.push(`${prefix}context ${context.entries} entries · ${context.compactions ?? 0} compactions`)
      } else if (context.compactions) {
        lines.push(`${prefix}context ${context.compactions} compactions`)
      }

      const blocked = info.blocked_on
      if (blocked) {
        const what = blocked.resources?.length
          ? blocked.resources.join(" ")
          : blocked.message || blocked.action || "unknown"
        lines.push(`${prefix}WAITING ON ${trim(String(what), 26)}`)
      }

      // a running terminal command or other tool: the difference between "busy"
      // and "busy running the thing that is stuck"
      const tools = (info as any).running ?? []
      for (const tool of tools.slice(0, 3)) {
        lines.push(`${prefix}> ${tool.tool} ${trim(String(tool.detail ?? ""), 26)}`)
      }

      const children = (info.children ?? []).filter((c) => c.state === "working")
      if (children.length) {
        lines.push(`${prefix}${children.length} subagent${children.length > 1 ? "s" : ""} in flight:`)
        for (const child of children.slice(0, 6)) {
          const mark = MARKS[child.state ?? "idle"] ?? "?"
          const seen = child.tokens ? ` ${compact(child.tokens)}` : ""
          lines.push(`${prefix}  ${mark} ${trim(child.label ?? child.id, 22)}${seen}`)
        }
        if (children.length > 6) lines.push(`${prefix}  …${children.length - 6} more`)
      }
      return lines
    }

    // The lamp number carries the eye in this block, so it gets its own colour:
    // a theme accent where one exists, and a fixed cyan otherwise so it is always
    // distinguishable from the text beside it.
    const digitFg = (): string => {
      try {
        const t = context.theme
        return (
          t?.text?.accent ??
          t?.accent?.base ??
          t?.border?.active ??
          t?.border?.focus ??
          "#33b1ff"
        )
      } catch {
        return "#33b1ff"
      }
    }

    // One clickable row. OpenTUI delivers mouse events to the topmost cell and
    // bubbles them, so a box containing the text is the clickable unit; the text
    // itself is made non-selectable or it swallows the click as a selection.
    // `getParts` returns strings and {text, fg} spans, so one segment can be
    // coloured differently from the rest of the line.
    const row = (
      getParts: () => Array<string | { text: string; fg?: string }>,
      onClick?: () => void,
    ) => {
      if (!jsxFn) return null
      const props: Record<string, unknown> = {
        flexDirection: "row",
        height: 1,
        get children() {
          return jsxFn("text", {
            fg: themeFg(),
            selectable: false,
            get children() {
              return getParts().map((part) =>
                typeof part === "string"
                  ? part
                  : jsxFn("span", {
                      fg: part.fg ?? themeFg(),
                      selectable: false,
                      get children() {
                        return part.text
                      },
                    }),
              )
            },
          })
        },
      }
      if (onClick) {
        props.onMouseUp = (event: any) => {
          if (event?.button !== 0) return          // left click only
          event?.stopPropagation?.()
          onClick()
        }
      }
      return jsxFn("box", props)
    }

    const textRow = (get: () => string, onClick?: () => void) => row(() => [get()], onClick)

    const sidebar = (sessionID?: string) => {
      if (!jsxFn) return null
      const build = () => {
        const rows: unknown[] = []
        rows.push(textRow(() => (online() ? `\u2328 ${VERSION_LABEL}` : `\u2328 ${VERSION_LABEL} \u00b7 offline`)))
        const all = lanes()
        if (!all.length) {
          rows.push(textRow(() => "  no lanes claimed"))
        }
        for (const l of all) {
          const here = !!sessionID && l.id === sessionID
          const mark = MARKS[l.state ?? "idle"] ?? "?"
          const lamp = String(l.key ?? "?").replace(/^led(?=\d)/, "")
          // who the agent says it is, in order of specificity: its own identifier,
          // then the machine it is on, then its agent kind as a last resort
          const who = l.ident || l.host || l.agent || ""
          const task = trim(sessionTitle(l.id) ?? l.label ?? "", 22)
          const rest = `${here ? "\u25B8" : " "}| ${who}(${mark})${task}`
          rows.push(
            row(() => [{ text: lamp, fg: digitFg() }, rest], () => toggleLane(l.id)),
          )
          if (isOpen(l)) {
            for (const detail of detailLines(l, "    ")) {
              rows.push(textRow(() => detail))
            }
          }
        }
        rows.push(textRow(() => (allOpen() ? "    (click a lane, alt+l or /lanes to collapse)"
                                           : "    (click a lane, alt+l or /lanes for detail)")))
        return rows
      }
      // built inside the getter so every signal read stays tracked
      return jsxFn("box", {
        flexDirection: "column",
        get children() {
          return build()
        },
      })
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

    place("sidebar.content", ({ sessionID }: any) => sidebar(sessionID))
    place("home.footer.status", () => line(footerText))

    // The keymap can only be claimed from inside a rendered component: setup()
    // runs in an async microtask with no Solid owner, so calling layer() there
    // throws "Keymap.Provider is missing" and the shortcut silently never exists.
    // The host's own plugins register from a slot render for exactly this reason.
    let keymapClaimed = false
    const claimKeymap = () => {
      if (keymapClaimed) return
      keymapClaimed = true
      try {
        context.keymap.layer(() => ({
          mode: "global",
          priority: 5,
          commands: [
            {
              id: "rgi.details",
              title: "rbgafi: lane details",
              group: "rbgafi",
              bind: "alt+l",
              palette: true,
              slash: { name: "lanes", aliases: ["rbgafi"] },
              run: () => toggleDetails(),
            },
          ],
          // no `bindings`: a named command's own `bind` is activated for it
        }))
        const shortcuts = context.keymap.shortcuts?.("rgi.details") ?? []
        log(`keymap layer registered (alt+l, /lanes) shortcuts=${JSON.stringify(shortcuts)}`)
      } catch (err: any) {
        log(`keymap layer failed: ${err?.message ?? err}`)
      }
    }
    place("app", () => {
      claimKeymap()
      return null
    })

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
