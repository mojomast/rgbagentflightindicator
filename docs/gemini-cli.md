# Gemini CLI

Gemini CLI runs a command at each point of its agent loop and reads the
command's stdout and exit code as a decision. This adapter turns those events
into panel states, one lane per session, named `gemini-cli:<session_id>`:

```sh
rgi hook gemini-cli        # one JSON event on stdin; nothing on stdout; exit 0
```

The hook never writes to stdout and never returns a non-zero exit code, because
Gemini CLI treats those as flow control: a status indicator must never allow,
block, or warn about anything the agent does. Diagnostics go to stderr only
while `RGI_HOOK_DEBUG=1` (or `RGI_DEBUG=1`). The implementation is
[`rgi/integrations/gemini_cli.py`](../rgi/integrations/gemini_cli.py); its tests
are [`tests/test_hooks_gemini.py`](../tests/test_hooks_gemini.py).

## What the panel shows

| Gemini CLI event | Lane state |
| --- | --- |
| `SessionStart` (`startup` / `resume` / `clear`) | `idle`; also clears a wait left behind by a crashed run |
| `BeforeAgent` | `working` |
| `BeforeTool`, `AfterTool` | `working`; clears a `done` left by a previous turn |
| `Notification` with `ToolPermission` | `blocked`, with the pending request id and action |
| `PreCompress` | `working` |
| `AfterAgent` | `done`; the lane stays visible until the session ends |
| `SessionEnd` (`exit` / `clear` / `logout` / `prompt_input_exit` / `other`) | all waits resolved, lane released |

A ToolPermission wait is resolved by the next event of that session: the tool
running (`BeforeTool`/`AfterTool`), the turn ending after a denial
(`AfterAgent`), a new prompt (`BeforeAgent`), compression, a resume, or
shutdown. Released Gemini CLI sends no linking id with the notification, so the
adapter cannot wait for one specific later event; it clears the wait rather
than leaving the lane blinking after a denial or cancellation.

The adapter reads only `session_id`, `hook_event_name`, `timestamp`, `cwd`, and
the notification fields; prompts and tool input are never inspected.

Reported: lane state, a short label from the project directory, and the pending
request (`exec`, `edit`, `mcp` or `info`, plus the CLI's message such as
`Tool Shell requires execution`). Not reported: prompts, model responses, tool
arguments, tool output, file contents or diffs, and transcripts. When the
notification carries no id, the request id is a one-way hash of the message and
details; the details themselves are never forwarded.

## Install

Hooks live in the `hooks` object of `~/.gemini/settings.json` (everywhere) or
`.gemini/settings.json` (one project, and only after you accept the trust
prompt). Merge this into the existing file; all eight events are needed for the
full lifecycle:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "*",
        "hooks": [
          { "name": "rgi-session-start", "type": "command", "command": "rgi hook gemini-cli", "timeout": 5000 }
        ]
      }
    ],
    "BeforeAgent": [
      {
        "matcher": "*",
        "hooks": [
          { "name": "rgi-before-agent", "type": "command", "command": "rgi hook gemini-cli", "timeout": 5000 }
        ]
      }
    ],
    "BeforeTool": [
      {
        "matcher": "*",
        "hooks": [
          { "name": "rgi-before-tool", "type": "command", "command": "rgi hook gemini-cli", "timeout": 5000 }
        ]
      }
    ],
    "AfterTool": [
      {
        "matcher": "*",
        "hooks": [
          { "name": "rgi-after-tool", "type": "command", "command": "rgi hook gemini-cli", "timeout": 5000 }
        ]
      }
    ],
    "Notification": [
      {
        "matcher": "*",
        "hooks": [
          { "name": "rgi-notification", "type": "command", "command": "rgi hook gemini-cli", "timeout": 5000 }
        ]
      }
    ],
    "PreCompress": [
      {
        "matcher": "*",
        "hooks": [
          { "name": "rgi-precompress", "type": "command", "command": "rgi hook gemini-cli", "timeout": 5000 }
        ]
      }
    ],
    "AfterAgent": [
      {
        "matcher": "*",
        "hooks": [
          { "name": "rgi-after-agent", "type": "command", "command": "rgi hook gemini-cli", "timeout": 5000 }
        ]
      }
    ],
    "SessionEnd": [
      {
        "matcher": "*",
        "hooks": [
          { "name": "rgi-session-end", "type": "command", "command": "rgi hook gemini-cli", "timeout": 5000 }
        ]
      }
    ]
  }
}
```

`rgi` must be on the `PATH` Gemini CLI was launched with. GUI and IDE launches
often see a different `PATH`; in that case use the interpreter that has the
package, for example `/opt/rgi/venv/bin/rgi hook gemini-cli`, or on Windows
`C:\\path\\to\\python.exe -m rgi hook gemini-cli` (JSON needs the doubled
backslashes). `timeout` is in milliseconds; 5000 bounds how long the CLI can
wait for one event.

Restart Gemini CLI after editing, then run `/hooks panel` to confirm all eight
hooks are registered. Project hooks are fingerprinted: after an edit the CLI
asks for trust again. `/hooks disable <name>` and `/hooks enable <name>` toggle
one hook without editing the file.

The panel itself is documented in the [README](../README.md); make sure the
daemon is running and `RGI_URL` / `RGI_TOKEN` (or `~/.config/rgi/url` and
`~/.config/rgi/token`) match your machine. If the panel is unreachable the hook
still exits 0 and the agent carries on.

## Package it as an extension

The same eight hooks can ship inside a Gemini CLI extension, which is easier to
distribute than editing settings on every machine. Extensions declare hooks in
`hooks/hooks.json` next to `gemini-extension.json` - not inside the manifest:

```
rgi-gemini-hooks/
  gemini-extension.json
  hooks/hooks.json
```

`gemini-extension.json`:

```json
{
  "name": "rgi-gemini-hooks",
  "version": "1.0.0"
}
```

`hooks/hooks.json` holds the same `{ "hooks": { ... } }` object as the settings
example above. Install it with:

```sh
gemini extensions install /path/to/rgi-gemini-hooks
```

Commands inside an extension may use `${extensionPath}`, but this adapter needs
nothing from the extension directory, so plain `rgi hook gemini-cli` is enough.
Remove the extension with `gemini extensions uninstall rgi-gemini-hooks`.

## Verify, debug, remove

Smoke test on Linux/macOS (on Windows, replace `rgi` with
`python -m rgi` or the full path):

```sh
echo '{"session_id":"smoke-test","hook_event_name":"BeforeAgent","cwd":"/srv/project","timestamp":"2026-01-01T00:00:00.000Z","prompt":"hello"}' \
  | rgi hook gemini-cli; echo "exit=$?"
```

This prints nothing and `exit=0`; `rgi status` then shows
`gemini-cli:smoke-test` as `working`. Send a `SessionEnd` payload the same way
to release it.

Set `RGI_HOOK_DEBUG=1` to see one line per event and panel errors on stderr.
Do not leave it on: when stdout is empty, Gemini CLI falls back to parsing
stderr, so debug text can appear in the session as a hook system message.

To remove the integration, delete the eight entries from `settings.json` (or
uninstall the extension) and restart the CLI. Lanes are released when their
session ends; a lane orphaned by a killed CLI is cleaned up the next time that
session resumes, because `SessionStart` resolves stale waits. If you must clear
everything, the daemon's `POST /clear` releases all lanes - it is the human
cleanup switch, not something to give an agent:

```sh
curl -sS -X POST http://127.0.0.1:8730/clear -H "X-LED-Token: $TOKEN"
```

## Troubleshooting

**No lane appears.** Run `rgi status` and confirm the daemon is up; then check
`/hooks panel` in Gemini CLI. A hook command that is not on `PATH` fails
silently by design: the CLI continues, the panel hears nothing. Use an absolute
path and verify with the smoke test above. Project-level hooks also need trust
consent, and `/hooks disable-all` switches everything off.

**The lane stays `blocked` after I answered the prompt.** The wait is resolved
by the session's next event; a denial is resolved by `AfterAgent`. If the CLI
was killed between the two, resume that session once (`SessionStart` clears
it) or clear the panel as the human. Treat it as stale rather than trusting
the colour.

**Every event takes a noticeable moment.** The adapter's HTTP calls are
best-effort with a 2-second timeout each, so a panel host that blackholes
traffic can delay the CLI; the `timeout` in settings bounds the total and the
hook still exits 0. Use a reachable `RGI_URL`.

**Debug lines show up in the Gemini CLI session.** That is the stderr
fallback: an empty stdout makes the CLI parse stderr. Turn `RGI_HOOK_DEBUG` off
after setup; the hook is intentionally silent on stdout.

**A `done` lane stays lit.** That is the design: a completed session keeps its
lamp visible until `SessionEnd` releases it, so you can see which agent
finished. `idle` after `SessionStart` means the CLI is open and waiting for
input.

## Supported versions

- **Package**: Gemini CLI, `@google/gemini-cli`, version **0.62.0** (the
  released version at the time of writing). The event names, stdin fields,
  stdout/exit-code contract, and `settings.json` / extension shapes were read
  from the released package's bundled docs:
  <https://unpkg.com/@google/gemini-cli@0.62.0/bundle/docs/hooks/reference.md>
  and <https://github.com/google-gemini/gemini-cli/blob/v0.62.0/docs/hooks/reference.md>.
  The same pages are published at
  <https://geminicli.com/docs/hooks/reference/>.
- **Package**: rgbagentflightindicator / rgi, version 0.8.0 in this tree.
- **How it was verified**: against the released schema and source, and with
  simulated event payloads in [`tests/test_hooks_gemini.py`](../tests/test_hooks_gemini.py).
  The hook was **simulated, not run against the real runtime**; the tests use a
  local mock panel, never a live Gemini CLI session or real hardware.
- **Limitations**:
  - One lane per session id, from `session_id` or `GEMINI_SESSION_ID`. If
    neither exists, the event is ignored; the transcript path is never guessed
    into an id, because such a lane would collide with other sessions.
  - Only one pending request per session is tracked. Gemini CLI asks for one
    tool confirmation at a time, but concurrent confirmations in one session
    would collapse into the newest one.
  - Released payloads carry no approval id, so waits are matched by session and
    cleared by the next event, not by a specific decision. The pending list
    can briefly disagree with a lane if events arrive out of order.
  - `done` is only released by `SessionEnd`, which Gemini CLI runs best effort
    and does not wait for; a killed CLI leaves the lane until the session
    resumes or a human clears the panel.
  - `Notification` details (file diffs, commands, prompts) are hashed for the
    request id and never published; the panel shows the CLI's summary message
    only.
  - Subagents, `BeforeModel`, `AfterModel`, and `BeforeToolSelection` are not
    mapped; they are neither subscribed to nor reported.
  - Newer Gemini CLI versions may add events or fields; unknown events are
    ignored, and the adapter fails silent and exits 0 by design.
