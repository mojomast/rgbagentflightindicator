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

### 1. Claim a lamp when you start

```
POST /session/start
{"agent": "hermes", "sessionID": "<unique id>", "label": "<short label>", "host": "<machine>"}
```

Response: `{"slot": 3, "key": "3", "group": "number-row"}` — that response **is**
your number. Use it if you ever report your state to the human.

- `sessionID` — unique and stable for the whole task. A UUID, a job id, anything.
- `label` — what the human reads. Keep it under ~16 characters.
- `host` — the machine you run on. Optional: omit it and the panel records the
  address the call came from.
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

## PASTE TO HERE
