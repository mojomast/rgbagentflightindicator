# Agent registration prompt

Give this to any agent so it reports itself to the panel. It is self-contained:
the agent needs no prior knowledge of this project.

The panel publishes this file, so you can hand over a single line instead:

```
Fetch and follow the instructions at
<panel-host>/files/AGENT_PROMPT.md
sending the header  X-LED-Token: <your token>
```

---

## PASTE FROM HERE

**You are being asked to register with a physical status panel.**

A service drives an RGB keyboard as an annunciator panel: one lamp per agent
session, so a human can see at a glance what is in flight, what has finished and
what is waiting on them.

### Endpoint

```
http://127.0.0.1:8730        if you run on the panel's machine
http://<panel-host>:8730     otherwise (whatever address the human gives you)
```

Every request carries the shared secret:

```
X-LED-Token: <token>
```

Missing or wrong token means `401` on every call. Ask the human for it; it is in
`~/.config/rgi/token` on the panel's machine.

**If the panel is on another machine, it is reached over Tailscale** — encrypted
between devices, and never exposed to the public internet. Two things matter in
practice:

- **use the full tailnet name** (`http://panel.example.ts.net:8730`), not the
  short form: short names can resolve to a link-local IPv6 address first and the
  connection will hang; and not a bare IP, because names survive address changes
  and read better on the lanes;
- **the token is required and is a real credential.** Do not log it, do not paste
  it into anything you commit, and if you are told it rotated, stop using the old
  one. Sending it in a header over Tailscale is the intended and secure use.
- if the connection times out rather than answering `401`, the tailnet path or an
  ACL is blocking you — say so, do not retry in a loop.

### 1. Claim a lamp when you start

```
POST /session/start
{"agent": "hermes", "sessionID": "<unique id>", "label": "<short label>",
 "host": "<machine>", "ident": "<your name>"}
```

Response: `{"slot": 3, "key": "3", "group": "number-row"}` — that response **is**
your number. Use it if you ever report your state to the human.

- `sessionID` — unique and stable for the whole task. A UUID, a job id, anything.
- `label` — what the human reads. Keep it under ~16 characters.
- `host` — the machine you run on. Optional: omit it and the panel records the
  address the call came from.
- `ident` — your name on the lane. You usually do not send it: every lane on a
  machine is named after the machine (`RGI_IDENT`, then `~/.config/rgi/name`, then
  the hostname) because the watcher claims it that way. Send your own only if the
  human gave you one — it is also accepted later with `POST /session/info`, and a
  re-claim will not rename a lane that already exists.
- `slot` — optional. Ask for a specific lamp; if it is taken you get `409` with
  the list of free ones. Without it you get the first free lamp.
- `409` means no lamps are free. **Never treat that as fatal** — carry on without
  an indicator, or retry later.

Call it once, at the start. Not per step, not in a loop.

### 2. Report state changes

```
POST /session/state
{"sessionID": "<unique id>", "state": "<state>"}
```

| state | when | panel |
|---|---|---|
| `working` | you are busy | steady **green** |
| `blocked` | **you need the human** | **blinking red** |
| `done` | you finished | **blinks white**, then holds white |
| `error` | you failed | steady red |
| `idle` | claimed but doing nothing | dim |

`blocked` also accepts `question`, `permission`, `pending`, `approval`, `waiting`.

**Transitions only.** Every call repaints the whole device, so spamming it makes
the panel flicker and tells the human nothing.

**`blocked` is the important one.** It exists so the human notices you need them.
Use it for a question, an approval or a permission prompt — and send a new state
the moment you are moving again, because it blinks until you do.

### 3. Release the lamp when you are done

```
POST /session/end
{"sessionID": "<unique id>"}
```

Lamps are the scarce resource: a keyboard has a dozen. A lamp you never release
stays claimed until the human clears it.

### Ask about yourself

```
GET /session/<your sessionID>
```

```
200 {"found": true, "sessionID": "job-123", "slot": 3, "key": "3",
     "agent": "hermes", "label": "nightly sync", "host": "kimi",
     "state": "working", "age": 12.3}

404 {"found": false, "hint": "not holding a lamp - POST /session/start"}
```

A `404` means you are not holding a lamp: you never claimed one, you released it,
or the panel was restarted. Claim again if you still need one.

### Which lamps are free?

```
GET /slots
```

```json
{"free": [2, 4],
 "slots": [{"slot": 1, "key": "1", "group": "number-row", "free": false,
            "sessionID": "ses_abc", "label": "VAM packages", "state": "working"},
           {"slot": 2, "key": "2", "free": true}]}
```

### Minimal example

```sh
BASE=http://127.0.0.1:8730
TOKEN=<token>

api() { curl -sS -X POST "$BASE$1" -H "Content-Type: application/json" \
        -H "X-LED-Token: $TOKEN" -d "$2"; }

api /session/start '{"agent":"hermes","sessionID":"job-123","label":"nightly sync"}'
api /session/state '{"sessionID":"job-123","state":"working"}'
# ... you need a decision ...
api /session/state '{"sessionID":"job-123","state":"blocked"}'
# ... unblocked ...
api /session/state '{"sessionID":"job-123","state":"working"}'
api /session/state '{"sessionID":"job-123","state":"done"}'
api /session/end   '{"sessionID":"job-123"}'
```

On Windows, quoting inline JSON through PowerShell mangles it - write the body to
a file and post `-d @file`, or use `Invoke-RestMethod`:

```powershell
$h = @{ "Content-Type" = "application/json"; "X-LED-Token" = $TOKEN }
Invoke-RestMethod -Method Post -Uri "$BASE/session/start" -Headers $h `
  -Body (@{ agent="hermes"; sessionID="job-123"; label="nightly sync" } | ConvertTo-Json)
```

### Rules

1. **Never block your real work on the panel.** Use a short timeout and ignore
   failures. It is an indicator, not a dependency.
2. **One `sessionID` per task.** Do not reuse an id after releasing it.
3. **Always release.** A leaked claim is a lamp the human cannot use.
4. **`blocked` is a promise.** Send it only when you genuinely cannot proceed.
5. **Transitions only.**
6. **Never edit the panel's own files on this machine.** The OpenCode plugin, the
   watcher and the daemon are published by the panel and updated by fetching them:
   `GET <panel>/files` for the current copies, and `PLUGIN_UPDATE_PROMPT.md` for
   the procedure. A hand-edited local copy diverges from every other machine and
   is silently overwritten by the next update. If something needs to change, tell
   the human rather than patching it locally.
7. **To change which lane you get**, ask the human. It is their configuration -
   `rgi lane-map` on the panel's machine maps an `ident` (or agent name) to a
   lane. You can *request* one with `"slot"` on `/session/start`, but the map is
   what makes it stick across runs.

### 4. Detail the human can see when a lane is uncollapsed

The lane line is deliberately terse. Everything else goes through a second call,
which never repaints hardware — it only changes what `/status` and the editor
sidebar show:

```
POST /session/info
{"sessionID": "<unique id>", "info": { ... }}
```

Send it when the detail changes, not on a timer. Known fields, all optional:

| field | example | shown as |
|---|---|---|
| `repo` | `"rgbagentflightindicator"` | which project this lane is working on |
| `branch` | `"main"` | `repo @ branch` |
| `ident` | `"hermes-3"` | your name on the lane — the machine's name by default; accepted when you claim and via `/session/info` |
| `directory` | `"D:\\vam"` | used when there is no repo |
| `tokens` | `{"input": 2400000, "output": 940000, "cache_read": 800000000, "cost": 4.12}` | token and spend line |
| `context` | `{"entries": 473, "compactions": 3, "percent": 42.5, "limit": 128000}` | context pressure |
| `blocked_on` | `{"action": "bash", "resources": ["rm -rf /tmp"], "message": "…"}` | **WAITING ON …** |
| `children` | `[{"id": "ses_x", "label": "subagent task", "state": "working", "tokens": 6710}]` | the subagent lines — shown only while they are in flight |
| `running` | `[{"tool": "bash", "detail": "npm test"}]` | shell commands running right now |

Anything else you send is stored and returned by `/status`, so a custom field is
not lost — it simply has no line of its own. Partial updates merge: sending
`{"tokens": {"output": 999}}` updates that number and leaves the rest alone.

**Subagents belong here.** A child session should not claim its own lamp — lamps
are scarce. List it under its parent's `children` instead, with its own `state`,
and it appears indented when the human uncollapses that lane.

Where the human sees it: clicking a lane in OpenCode's sidebar expands it, and
clicking the block's title expands or collapses every lane. `alt+l` and `/lanes`
are meant to do the same, but the keymap registers no command on current builds —
the plugin's log records what the host reports.

### If your runtime has an integration, use it

Claude Code, Gemini CLI, Pi, the GitHub Copilot SDK, the Zoo Code CLI, the Python
agent frameworks (OpenAI Agents SDK, Pydantic AI, LangGraph, CrewAI, Microsoft
Agent Framework) and the ChatGPT/Codex MCP adapter all report through the same
shared client, so lanes, waits counted by id, event ordering and cleanup are
handled for you. The index - with the supported version of each upstream - is
published at `GET /files/integrations.md` and lives in the repository at
`docs/integrations.md`. If your runtime is not there, this page is the contract
to follow by hand.

## PASTE TO HERE
