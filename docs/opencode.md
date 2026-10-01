# The OpenCode plugin

The panel works without any editor integration, but inside OpenCode you also get
a sidebar block naming every lane, plus a one-line strip on the home footer.

Two files, in OpenCode's config directory:

```
<opencode config dir>/plugins/rgi-panel/
    index.ts     server half, deliberately empty
    tui.ts       the sidebar and footer
```

`<opencode config dir>` is `~/.config/opencode` (`C:\Users\<you>\.config\opencode`
on Windows). OpenCode discovers plugins by directory, so nothing goes in
`cli.json`.

For doing this **on another machine** — including retiring an older install,
tokens, and what to hand an agent — see [machines.md](machines.md).

## Installing it

```sh
cd ~/.config/opencode
npm install "@opentui/core@^0.5.14" "@opentui/solid@^0.5.14" "solid-js@^1.9.12"
mkdir -p plugins/rgi-panel
cp <this repo>/plugin/*.ts plugins/rgi-panel/
```

Then restart OpenCode. `prompts/PLUGIN_SETUP_PROMPT.md` is the same thing written
for an agent to follow, and the panel serves both files at
`GET /files/tui.ts` and `GET /files/index.ts`.

The peer dependencies must be resolvable from the plugin directory. `npm` may
warn `EBADENGINE ... node: '>=26.4.0'` for `@opentui/core`: ignore it. OpenCode
loads plugins in its own bundled Bun runtime, not in your Node.

## Configuration

`tui.ts` reads:

| variable | effect |
|---|---|
| `RGI_URL` | the panel, default `http://127.0.0.1:8730` |
| `RGI_TOKEN` | the shared secret; otherwise `~/.config/rgi/token` is read |

Diagnostics go to `~/.config/rgi/plugin.log`.

## The four sharp edges

Each of these was a real failure before it worked. They are the reason this
plugin is not a 20-line file.

1. **The default export must be `{ id, setup }`.** The older v1 shape
   `{ id, tui(...) }` is rejected with
   `Invalid V2 TUI plugin module: <dir>`. OpenCode validates structurally, calls
   `setup(context)` once, and treats the returned function as cleanup.

2. **The entrypoint is `tui.ts`, and it is built without a JSX transform.**
   Writing JSX inside it fails with `N errors building tui.ts`, so elements come
   from `jsx()` loaded at runtime. Whether `tui.tsx` is discovered varies by
   build; `tui.ts` plus an explicit `jsx()` import is the combination that works.

3. **Slots take exactly one placement key.**
   `context.ui.slot({ append: "sidebar.content", render })`. The v1 underscore
   names and multi-key claims are refused with
   `Slot claim requires exactly one placement key`.

4. **OpenCode imports external plugins before installing OpenTUI's Solid runtime
   support**, so a plugin that renders JSX meets `No renderer found` — and the
   host then *disposes the plugin*, because the value it got back could not be
   mounted. The plugin therefore installs the runtime support itself and only
   then imports the JSX runtime:

   ```ts
   await import("@opentui/solid/runtime-plugin-support/configure")  // ensureRuntimePluginSupport()
   await import("@opentui/solid/jsx-runtime")                       // jsx
   ```

   and renders `null` rather than a string if that fails, so a missing renderer
   costs you a blank panel instead of a dead plugin.

## Uncollapsing a lane

The block is one line per lane by default. **`alt+l`** (or `/lanes` from the
command palette) uncollapses it, showing, per lane:

The collapsed line reads `number|who(mark)task`, with the number in its own colour
so it is easy to pick out, and `who` is what the agent calls itself — its `ident`
if it set one, otherwise the machine it runs on:

```
⌨ rbgafi v0.4
▸0| workstation(▶)Keyboard status: LED da…
 7| hermes-3(!)nightly sync
 3| ussy01(✔)OpenCode2 updates and new feat
    (click a lane, alt+l or /lanes for detail)
```

Rows are **clickable**: clicking a lane uncollapses just that lane; `alt+l` (or
`/lanes`) does all of them at once.

- **repository and branch** — which project the lane is actually working in,
  derived from the session's directory.
- **in flight / idle** — how long the current state has lasted, and how long since
  the lane last reported anything. The two numbers that answer "is it stuck?".
- **tokens and spend** — input/output, cache reads, reasoning, and the session's
  cost.
- **context** — how many context entries and how many compactions. OpenCode
  exposes no context *limit*, so a percentage appears only when one is configured:
  set `RGI_CONTEXT_LIMIT` in tokens (e.g. `128000`) and the line becomes
  `context 42.5% of 128k`.
- **WAITING ON …** — for a lane in `blocked`, *what* it wants: the permission
  action and its resources, or the agent's own `blocked_on` message.
- **subagents** — the child sessions, indented, each with its own state mark and
  output tokens. They do not consume lanes of their own.

All of that comes from `POST /session/info`, which agents (and the watcher) send
separately from state changes: detail never triggers a hardware repaint. Start
expanded with `RGI_EXPAND=1` if you prefer it that way.

## Verifying it loaded

```sh
cat ~/.config/rgi/plugin.log
```

A healthy load:

```
setup() called (panel=http://127.0.0.1:8730, token=yes)
OpenTUI runtime support installed
jsx runtime ready
slot registered: sidebar.content
slot registered: home.footer.status
```

| line | meaning |
|---|---|
| `could not install OpenTUI runtime support: … is Bun-only` | you are running the file outside OpenCode's Bun runtime |
| `jsx() failed: No renderer found` | edge 4 above; the panel stays blank but the plugin survives |
| `slot FAILED: …` | the host refused a slot name or placement |
| no lines at all | OpenCode never loaded the plugin: check `opencode plugin list`, and that both files are in the directory |

OpenCode usually reloads a plugin when its files change; restart it if the
sidebar does not pick the change up.

## What it draws

```
⌨ Panel
▸  1 ▶ kimi          the session you are reading
   2 ✔ build-box     some other session
```

`▸` marks the session whose sidebar you are looking at, then the lamp's key name,
its state mark, the machine it runs on and the lane label. The footer carries the
compact form: `⌨ 1▶ 2✔`.

Lane names come from OpenCode's own session titles when the lane *is* an OpenCode
session, and from the label the agent registered with otherwise, so a CI job
shows as `nightly sync` rather than a UUID.
