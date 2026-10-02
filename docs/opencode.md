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
| `RGI_URL` | the panel; otherwise `~/.config/rgi/url` is read, then `http://127.0.0.1:8730` |
| `RGI_TOKEN` | the shared secret; otherwise `~/.config/rgi/token` is read |

The file matters on machines that are not the panel: a running OpenCode cannot
acquire a new environment variable, so exporting `RGI_URL` after it started leaves
its sidebar on localhost. The plugin also falls back to localhost when the address
it was given does not answer, so a tailnet name that is down does not blank the
panel on the machine running the panel.

Lane **names** come from the watcher, not the plugin: it claims every local
session under the machine's name — `RGI_IDENT`, then `~/.config/rgi/name`, then
the hostname — and the sidebar falls back to the lane's machine when an older
watcher sent no name at all. An agent the human gave a personal name overrides it
with `ident`, and the harness it runs in is shown in the detail, never as a name.

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

A collapsed lane is one line: the lamp, the name the agent was told to use, the
state mark and an abbreviated task. The name is the agent's `ident` — what the
human told it to call itself — never the harness it runs in. The name and the
mark carry the lamps' colours: green in flight, white complete, red for needs a
human, dim for idle.

```
⌨ rgbafi v0.6
▸0| opencode(▶) Keyboard status: LED daemon…
 7| hermes-3(!) Verify the nightly artifacts
 3| opencode(✔) OpenCode2 updates and n…
    (click a lane for detail, the title for all)
⧉ copy install/update prompt + token
```

Uncollapsing shows the whole task, wrapped across as many lines as it needs
without cutting words, then the detail lines, where the harness and the machine
appear on their own line because neither is the agent's name:

```
▸7| hermes-3(!) Verify the nightly artifacts, rebuild
    the index and republish
    harness hermes · host kimi
    repo vam @ main
    idle 12m
    tokens 2.6M in / 1.0M out · $4.55
```

Rows are **clickable**: clicking a lane uncollapses just that lane; clicking the
block's title line uncollapses all of them. `alt+l` and `/lanes` are meant to do
the same, but `context.keymap.layer()` registers no command on current OpenCode
builds — the plugin's own diagnosis records what the host reports (`mine=0` in
`plugin.log`) — so the mouse is the path that works.

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
- **subagents** — only the child sessions that are **currently in flight**, up to
  six, each with its own state mark and output tokens. A finished subagent is
  history, and history in a one-line-per-lane panel is noise; it disappears from
  the lane the moment it lands. A subagent **never takes a lamp of its own**: the
  watcher claims one lane per root session and reports children as metadata, and
  it classifies a session before binding it, so a child that starts between
  metadata refreshes cannot grab a lamp — if one ever does, the lamp is handed
  back on the next pass. (`--include-subagents` asks for the old behaviour: a
  lamp per child.)
- **running commands** — the shell commands the lane is running right now, one
  line each (`> bash npm test`), with the command itself rather than the tool
  name. Only shells: a lane reading a file is busy, while a lane running a command
  is busy in a way you may want to interrupt.
- **in flight** — the duration of the **current action only**, and absent the
  moment the lane lands. `idle` keeps counting, because a landed lane that has
  gone quiet is exactly what you want to notice.

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
⌨ rgbafi v0.6
▸1| hermes(▶)
    nightly sync
 2| build-box(✔)
    some other session
```

`▸` marks the session whose sidebar you are looking at; then the lamp's key, the
name the agent claims as its `ident` (the agent kind only when it claimed without
one), and the state mark. The task sits on its own line beneath. The host is part
of the detail lines, shown when the lane is expanded. The footer carries the
compact form: `⌨ 1▶ 2✔`.

Lane names come from OpenCode's own session titles when the lane *is* an OpenCode
session, and from the label the agent registered with otherwise, so a CI job
shows as `nightly sync` rather than a UUID.
