# The Pi coding agent extension

Pi sessions can carry a panel lane too. The integration is one TypeScript file,
`plugin/pi/extension.ts`, loaded by Pi's own extension host: the same host
process claims a lamp when a run starts, reports the run working, reports a
confirmation dialog as blocked while it waits on a human, marks the lane done
when the run finally settles, and releases the lamp when the session shuts
down. No watcher, no Python and no build step on the Pi machine.

This page is written against the released package, not a development branch.
Nothing here has been executed inside a real Pi process: the file is checked
structurally and parsed by Node, while the event semantics below are read from
the released source named in [Supported versions](#supported-versions).

For the other harness, see the [OpenCode plugin](opencode.md); for the panel
API every adapter shares, see [Wiring up agents](agents.md).

## Where extensions live

For the released version the *agent directory* defaults to `~/.pi/agent` and
can be moved with `PI_CODING_AGENT_DIR`:

| location | contents |
|---|---|
| `<agent-dir>/extensions/` | user extensions, loaded in every project |
| `.pi/extensions/` | project extensions, loaded after project trust |

Pi loads direct `.ts` or `.js` files, and directories containing an
`index.ts` or `index.js`. A single file is enough here. Pi uses a bundled
TypeScript loader, so there is no compile step and no `package.json` to add
unless the extension needs npm dependencies.

## Installing it

```sh
mkdir -p ~/.pi/agent/extensions
cp <this repo>/plugin/pi/extension.ts ~/.pi/agent/extensions/rgi-panel.ts
```

Restart Pi, or run `/reload`. For a single run without installing:

```sh
pi --extension <this repo>/plugin/pi/extension.ts
```

The extension directory is listed at startup; extension load failures are
reported there. To watch the panel calls themselves, set `RGI_HOOK_DEBUG=1`
(diagnostics go to stderr; the extension never writes stdout).

## Configuration

| variable | effect |
|---|---|
| `RGI_URL` | the panel; otherwise `~/.config/rgi/url` is read, then `http://127.0.0.1:8730` |
| `RGI_TOKEN` | the shared secret; otherwise `~/.config/rgi/token` is read |
| `RGI_IDENT` | lane name; otherwise the machine's hostname |
| `RGI_HOOK_DEBUG` | `1` prints one line per failure class to stderr |

The ordering is the one every other client in this project uses, and the
files matter because Pi may have started before the environment changed. There
is no requirement that the panel exist: every call is bounded by a 1.5-second
`AbortSignal.timeout`, every error is swallowed, and a failure is logged once
at most. The token is only ever sent in the `X-LED-Token` header; it is never
printed and never written to the transcript.

The lane is named with the machine's hostname (`os.hostname()`), or `RGI_IDENT`
when set. Its identity is `pi:<session id>`: two Pi sessions are two lanes, and
resuming the same session re-adopts the same lane. As with the Python reporter,
a lane is claimed by number from the panel's free list and never evicts
somebody else's.

## What each event reports

| Pi event | panel effect |
|---|---|
| `agent_start` | claims a lamp if the session has none (adopt by id, otherwise a free number), then posts `working` |
| `agent_settled` | posts `done`; the lane stays visible until the session releases it |
| `ui_prompt_start` (kind `confirm` or `select`) | posts `blocked`, with `blocked_on.action` = the dialog kind, `blocked_on.message` = its title, and one `pending_requests` id |
| `ui_prompt_end` (kind `confirm` or `select`) | resolves that id and returns the lane to its previous state |
| `session_start` | re-keys to the new session and releases a lane left over by a replaced session |
| `session_shutdown` | awaits `POST /session/end`, stops the heartbeat, removes every listener and clears state; safe to run twice |

`agent_start` reports `working`; a repeated `working` is not sent again. While
a run is in flight the extension heartbeats the lane every 30 seconds, so a
long quiet tool call does not look abandoned, and the panel restarting
mid-run is recovered on the next heartbeat. The interval is cleared on settle
and on shutdown.

## Completion is `agent_settled`, never `agent_end`

`agent_end` fires when a low-level run ends, but Pi may still auto-retry,
auto-compact and retry, or continue with queued follow-up messages afterwards.
Treating it as completion would blink the lane `done` in the middle of work
that is still going to happen, so this extension does not listen to it at all.

`agent_settled` is the final, notification-only boundary: it fires when no
automatic retry, compaction or queued continuation remains. That is the only
event that marks the lane done. The trade-off is that `agent_settled` carries
no outcome - a completed run, an aborted run and a run that ended in an error
all settle - so the lane reads `done` rather than `error` in those cases.

## Approval reporting

The released API has no permission or approval *event*. A permission check is
a user-facing extension dialog - `ctx.ui.confirm()` or `ctx.ui.select()`, as
the published `examples/extensions/permission-gate.ts` uses - and every
blocking dialog is bracketed by `ui_prompt_start` and `ui_prompt_end`. Those
two events are the confirmation mechanism this extension integrates with:

- a `confirm` or `select` dialog turns the lane `blocked` while it is open,
  and resolving it returns the lane to the state it had before;
- the wait is counted by id, so it resolves exactly when that dialog closes;
- `input`, `editor` and `custom` prompts are notifications, not approvals, and
  are deliberately not reported as blocked;
- nothing else in the extension ever reports `blocked`. There is no heuristic
  for "safe" or "dangerous" tools.

Two consequences worth stating plainly. First, Pi's built-in tools do not ask
for approval on their own in this version, so a lane only ever shows `blocked`
while a confirm/select dialog is open through Pi's extension UI - a
permission-gate extension or Pi's own project-trust prompt - and never
otherwise. This extension reports such waits, it does not create them.
Second, a dialog that Pi shows outside its extension UI layer is not covered
by these events, and the extension has no other way to see it.

## Cleanup and reloads

The factory registers exactly one handler per event; a second factory call in
the same runtime returns the existing dispose function instead of adding a
second listener. `dispose()` clears the heartbeat interval, empties the wait
bookkeeping and calls every unsubscribe function returned by the event
registration.

The factory also returns that dispose function, but the released loader
ignores a factory's return value, so the cleanup that actually runs is wired
to `session_shutdown`. That event fires for quit, `/reload` and session
replacement; the handler is awaited - bounded by the same short timeout - so
the lane is released before Pi goes away rather than left claimed with no
reporter. On reload, the replacement runtime claims a fresh lane on the next
`agent_start`.

## Verifying it loaded

Extension load errors and the extension list appear at Pi startup. With
`RGI_HOOK_DEBUG=1`, a healthy load records the resolved panel and whether a
token was found, and each failure class (unreachable panel, rejected token, no
free lamp, handler error) is logged once to stderr.

## Supported versions

- **Package:** `@earendil-works/pi-coding-agent` - the Pi coding agent
  (repository `github.com/earendil-works/pi`, package directory
  `packages/coding-agent`).
- **Verified against:** `0.99.2`, the current release at the time of writing.
- **How:** read from the published npm tarball
  `https://registry.npmjs.org/@earendil-works/pi-coding-agent/-/pi-coding-agent-0.99.2.tgz`,
  specifically `docs/extensions.md` and
  `dist/core/extensions/types.d.ts`. The default branch was not used. The
  event list, event semantics, `ExtensionAPI`, `ExtensionContext`, the
  `session_shutdown` cleanup contract and the `ui_prompt_*` envelope all come
  from that tarball.
- **Test status:** simulated, not run against the real runtime. The extension
  is checked by [tests/test_pi_extension.py](../tests/test_pi_extension.py)
  (structural assertions plus `node --check`); no Pi installation is required
  and no Pi process was started.

### Limitations

- Not executed inside Pi. The event ordering and dialog semantics are as
  documented and typed in 0.99.2; real-runtime behavior is unverified.
- Completion is `agent_settled`, so aborted and errored runs also read `done`;
  the lane never reports `error`.
- `agent_end` is intentionally not handled; between an `agent_end` and the
  following retry, compaction or queued turn the lane stays `working`.
- Approval reporting depends on a confirm/select dialog being raised through
  Pi's extension UI (a permission-gate extension is the usual source; Pi's
  project-trust prompt is another). Pi has no built-in tool-approval event in
  this version, so without any such dialog a lane never becomes blocked.
- Only `confirm` and `select` are treated as waits; `input`, `editor` and
  `custom` prompts are ignored.
- The extension reports states only; it does not publish tokens, context usage,
  repository or child-subagent metadata.
- Lane names come from `RGI_IDENT` or the hostname only; the
  `~/.config/rgi/name` file the Python reporter reads is not consulted.
- On `session_shutdown` the lane is released. A session resumed after Pi exits
  claims a lane again and the lamp number can change. A `/reload` mid-run
  releases the lane and re-claims it on the next `agent_start`.
- Panel downtime is invisible by design: failures are swallowed and logged
  once at most to stderr, never to the agent or to stdout.
