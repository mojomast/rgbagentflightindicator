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
import { spawnSync } from "node:child_process"

const HOME = homedir()
const CONFIG = join(HOME, ".config", "rgi")
const POLL_MS = 1000
const LOG = join(CONFIG, "plugin.log")

// The panel address has to survive the environment. A running OpenCode cannot
// acquire a new variable, and editing this default locally is what the update
// prompt forbids - the next update overwrites it. So: the environment first, for
// an override, then ~/.config/rgi/url - the same file the rgi client reads - and
// only then localhost.
function resolvePanel(): { url: string; from: string } {
  const env = process.env.RGI_URL ?? process.env.LEDD_URL
  if (env) return { url: env.replace(/\/+$/, ""), from: "RGI_URL" }
  try {
    const file = readFileSync(join(CONFIG, "url"), "utf8").trim().replace(/\/+$/, "")
    if (file) return { url: file, from: "~/.config/rgi/url" }
  } catch {
    /* no file is the normal case when the panel is local */
  }
  return { url: "http://127.0.0.1:8730", from: "the default" }
}
const PANEL_INFO = resolvePanel()
const PANEL = PANEL_INFO.url

// What the sidebar block calls itself. Keep in step with VERSION_LABEL in
// rgi/__init__.py and the version in pyproject.toml.
const VERSION_LABEL = "rbgafi v0.4"

// A paste-ready handover: whichever prompt fits the machine, with the token, so
// the human does not have to assemble it under pressure.
function handoverText(): string {
  return [
    "Register with the rbgafi status panel.",
    "",
    "If this machine has no plugin yet:",
    `  Fetch and follow ${PANEL}/files/PLUGIN_SETUP_PROMPT.md`,
    "If it already has one:",
    `  Fetch and follow ${PANEL}/files/PLUGIN_UPDATE_PROMPT.md`,
    "",
    `In both cases send the header: X-LED-Token: ${TOKEN || "<token missing>"}`,
    "",
    "Do not edit the panel's files locally; take changes from the panel.",
  ].join("\n")
}

// The TUI has no clipboard API exposed to plugins, so use the platform's tool.
function copyToClipboard(text: string): { ok: boolean; why?: string } {
  const attempts: Array<[string, string[]]> =
    process.platform === "win32"
      ? [["clip", []]]
      : process.platform === "darwin"
        ? [["pbcopy", []]]
        : [["wl-copy", []], ["xclip", ["-selection", "clipboard"]], ["xsel", ["-ib"]]]
  for (const [command, args] of attempts) {
    try {
      const result = spawnSync(command, args, { input: text })
      if (!result.error && (result.status === 0 || result.status === null)) {
        return { ok: true }
      }
      if (result.error) continue
    } catch {
      /* try the next one */
    }
  }
  return { ok: false, why: `no clipboard tool of ${attempts.map((a) => a[0]).join(", ")}` }
}

// Details are hidden by default so the block stays one line per lane. Alt+L or
// /lanes toggles them; RGI_EXPAND=1 starts expanded.
const START_EXPANDED = process.env.RGI_EXPAND === "1"

// How wide the block aims to stay when a full task wraps: continuation lines use
// this as their budget, and the name's line gets what is left of it.
const TASK_WRAP = 44

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

// Wrap a task across lines without cutting words. The first line has a smaller
// budget because the lamp, the name and the mark sit on it; the rest get `rest`.
// A single word longer than the budget is left whole rather than split.
function wrapTask(text: string, first: number, rest: number): string[] {
  const lines: string[] = []
  let line = ""
  let budget = first
  for (const word of text.split(" ").filter(Boolean)) {
    const candidate = line ? `${line} ${word}` : word
    if (line && candidate.length > budget) {
      lines.push(line)
      line = word
      budget = rest
    } else {
      line = candidate
    }
  }
  if (line) lines.push(line)
  return lines
}

export default {
  id: "rgi.panel",

  setup(context: any) {
    log(`setup() called (panel=${PANEL}, from ${PANEL_INFO.from}, token=${TOKEN ? "yes" : "no"})`)

    const [lanes, setLanes] = createSignal<Lane[]>([])
    const [online, setOnline] = createSignal(false)
    // Detail is per lane and toggled by clicking it; alt+l (or /lanes) does all.
    const [openLanes, setOpenLanes] = createSignal<string[]>([])
    const [allOpen, setAllOpen] = createSignal(START_EXPANDED)
    const [copied, setCopied] = createSignal<"idle" | "ok" | "failed">("idle")

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

    // The panel address can be a tailnet name, which fails when the tailnet is
    // down even though the panel is running on this very machine. Localhost is
    // the last resort, and a slow address never lets polls stack up.
    const FALLBACK = "http://127.0.0.1:8730"
    let polling = false
    const poll = async () => {
      if (polling) return
      polling = true
      try {
        let data: any
        for (const base of PANEL === FALLBACK ? [FALLBACK] : [PANEL, FALLBACK]) {
          try {
            const signal = (AbortSignal as any)?.timeout?.(4000)
            const res = await fetch(`${base}/status`, {
              headers: TOKEN ? { "X-LED-Token": TOKEN } : {},
              ...(signal ? { signal } : {}),
            })
            data = await res.json()
            break
          } catch {
            /* try the next address */
          }
        }
        if (data) {
          const list: Lane[] = Object.entries(data.sessions ?? {}).map(
            ([id, v]: [string, any]) => ({ id, ...v }),
          )
          list.sort((a, b) => Number(a.slot ?? 99) - Number(b.slot ?? 99))
          setOnline(true)
          setLanes(list)
        } else {
          setOnline(false)
        }
      } finally {
        polling = false
        try {
          context.renderer?.requestRender?.()
        } catch {
          /* ignore */
        }
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

      // The harness is the tool the lane runs in, and it is not the agent's name:
      // the name is on the lane's own line, so this gets a line of its own, shared
      // only with the machine it runs on.
      const harness = [
        l.agent ? `harness ${trim(l.agent, 12)}` : "",
        l.host ? `host ${trim(l.host, HOST_W)}` : "",
      ].filter(Boolean)
      if (harness.length) lines.push(`${prefix}${harness.join(" · ")}`)

      const where = [info.repo, info.branch].filter(Boolean).join(" @ ")
      if (where) lines.push(`${prefix}repo ${trim(where, 30)}`)
      else if (info.directory) lines.push(`${prefix}dir ${trim(String(info.directory), 30)}`)

      // in flight and idle are mutually exclusive: a running action is not idle,
      // and a landed lane is not flying. Exactly one of the two is ever present.
      if (l.in_flight_s !== null && l.in_flight_s !== undefined) {
        lines.push(`${prefix}in flight ${duration(l.in_flight_s)}`)
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

    // The lamps say in flight / complete / needs a human in green, white and red.
    // The block is only text, so it says the same three things in the same
    // colours: before this, the digit was the only coloured thing on a lane.
    const dimFg = (): string => {
      try {
        const t = context.theme?.text
        return t?.muted ?? t?.subtle ?? t?.dim ?? "#8b949e"
      } catch {
        return "#8b949e"
      }
    }

    const stateFg = (state?: string): string => {
      switch (state) {
        case "working":
          return "#3fb950"                   // in flight
        case "blocked":
        case "error":
          return "#f85149"                   // needs a human
        case "done":
          return themeFg()                   // complete, like the white lamp
        default:
          return dimFg()                     // idle: nothing claimed
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

    const textRow = (get: () => string, onClick?: () => void, fg?: string) =>
      row(() => [{ text: get(), fg: fg ?? themeFg() }], onClick)

    const sidebar = (sessionID?: string) => {
      if (!jsxFn) return null
      const build = () => {
        const rows: unknown[] = []
        // the header is the "all of them" toggle: the keymap API is not
        // registering commands in this build, so the mouse is the reliable path
        rows.push(
          row(
            () => [online() ? `\u2328 ${VERSION_LABEL}` : `\u2328 ${VERSION_LABEL} \u00b7 offline`],
            () => toggleDetails(),
          ),
        )
        const all = lanes()
        if (!all.length) {
          rows.push(textRow(() => "  no lanes claimed"))
        }
        for (const l of all) {
          const here = !!sessionID && l.id === sessionID
          const mark = MARKS[l.state ?? "idle"] ?? "?"
          const lamp = String(l.key ?? "?").replace(/^led(?=\d)/, "")
          const open = isOpen(l)
          // The name is the ident the human told the agent to use; the harness is
          // a different thing and gets its own line in the detail.
          const who = trim(l.ident || l.agent || "?", 14)
          const task = (sessionTitle(l.id) ?? l.label ?? "").replace(/\s+/g, " ").trim()
          const lead = `${here ? "\u25B8" : " "}| `
          const name = `${who}(${mark})`
          const toggle = () => toggleLane(l.id)
          const headed = (tail: string) => () => [
            { text: lamp, fg: digitFg() },
            { text: lead, fg: dimFg() },
            { text: name, fg: stateFg(l.state) },
            { text: tail ? ` ${tail}` : "", fg: themeFg() },
          ]
          if (!open) {
            // collapsed: the name, and the task abbreviated onto the same line
            const short = task.length > 24 ? task.slice(0, 23) + "\u2026" : task
            rows.push(row(headed(short), toggle))
          } else {
            // expanded: the whole task, starting on the name's line and wrapping
            // beneath it, then the detail lines
            const firstBudget = Math.max(16, TASK_WRAP - lamp.length - lead.length - name.length - 1)
            const wrapped = task ? wrapTask(task, firstBudget, TASK_WRAP - 4) : []
            rows.push(row(headed(wrapped[0] ?? ""), toggle))
            for (const extra of wrapped.slice(1)) {
              rows.push(textRow(() => `    ${extra}`, toggle))
            }
            for (const detail of detailLines(l, "    ")) {
              const waiting = detail.trimStart().startsWith("WAITING ON")
              rows.push(textRow(() => detail, undefined, waiting ? "#f85149" : dimFg()))
            }
          }
        }
        rows.push(textRow(() => (allOpen() ? "    (click the title to collapse all, a lane to collapse one)"
                                           : "    (click a lane for detail, the title for all)"),
                          undefined, dimFg()))
        // the handover button, at the bottom where it is least in the way
        rows.push(
          row(
            () => {
              const state = copied()
              if (state === "ok") return ["\u2713 copied \u2014 paste it to the agent"]
              if (state === "failed") return ["\u2716 clipboard unavailable (see plugin.log)"]
              return ["\u29c9 copy install/update prompt + token"]
            },
            () => {
              const result = copyToClipboard(handoverText())
              log(result.ok ? "handover copied to the clipboard"
                            : `clipboard copy failed: ${result.why}`)
              setCopied(result.ok ? "ok" : "failed")
              repaint()
              setTimeout(() => {
                setCopied("idle")
                repaint()
              }, 4000)
            },
          ),
        )
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

    // One-shot self-diagnosis of the keymap, because "the shortcut does nothing"
    // has three different causes and they need different fixes: the layer never
    // compiled, the command compiled but the key never reaches it, or the command
    // runs and the panel does not repaint. Ask the host directly.
    const diagnoseKeymap = () => {
      try {
        const commands = context.keymap?.commands?.() ?? []
        const mine = commands.filter((c: any) => c && c.id === "rgi.details")
        log(`keymap diag: ${commands.length} command(s) visible, mine=${mine.length}`)
        const shortcuts = context.keymap?.shortcuts?.("rgi.details") ?? []
        log(`keymap diag: shortcuts for rgi.details = ${JSON.stringify(shortcuts)}`)
        const before = allOpen()
        const dispatched = context.keymap?.dispatch?.("rgi.details")
        const after = allOpen()
        log(`keymap diag: dispatch returned ${dispatched === undefined ? "undefined" : typeof dispatched}, state changed: ${before !== after}`)
        if (before !== after) {
          context.keymap?.dispatch?.("rgi.details")     // put it back
          log("keymap diag: state restored")
        }
        const palette = commands.filter((c: any) => c && c.palette).map((c: any) => c.id)
        log(`keymap diag: palette entries = ${JSON.stringify(palette.slice(0, 8))}`)
      } catch (err: any) {
        log(`keymap diag failed: ${err?.message ?? err}`)
      }
    }
    setTimeout(diagnoseKeymap, 4000)

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
