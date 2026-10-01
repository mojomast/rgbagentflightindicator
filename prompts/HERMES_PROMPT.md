# Hermes instructions

Give this to hermes. It covers both directions: reporting your own state to the
panel, and **reading** the panel to see what every other lane is doing — which is
how you can tell when something is waiting on the human.

The panel publishes this file, so you can hand over a single line instead:

```
Fetch and follow the instructions at
<panel-host>/files/HERMES_PROMPT.md
sending the header  X-LED-Token: <token>
```

---

## PASTE FROM HERE

**You are being asked to use a status panel: report your state, and read
everyone else's.**

A service drives one or more RGB keyboards as an annunciator panel. One lamp per
agent session; the colour is the state. It also exposes everything over HTTP, so
you can see the whole fleet before deciding what to do.

### Reporting: claim a lane, report transitions, release it

```
POST /session/start
{"agent": "hermes", "sessionID": "<unique id>", "label": "<short label>", "host": "<machine>"}
-> {"slot": 2, "key": "led2", "devices": ["evision"]}
```

`sessionID` is yours for the task; `label` is what the human reads (keep it under
~16 characters); `host` is optional and defaults to your address. `409` means no
lanes are free — never fatal, carry on without an indicator.

```
POST /session/state   {"sessionID": "<id>", "state": "working|done|blocked|error|idle"}
POST /session/end     {"sessionID": "<id>"}
```

| state | panel |
|---|---|
| `working` | steady green |
| `blocked` | **blinking red - you need the human** |
| `done` | blinks white, then holds white |
| `error` | steady red |

**Transitions only**, and always send a new state after `blocked` — it blinks
until you do. Release the lane when the task is over: lamps are scarce.

Every request needs `X-LED-Token: <token>` when the panel is not on localhost.

### Observing: reading the lanes

**1. The command line** — a table of every lane and every device:

```sh
rgi status                      # add --url http://<panel-host>:8730 if remote
```

```
rbgafi 0.3.0  http://127.0.0.1:8730
  device evision       126 lamps  per-key

  lane  0  done      Greeting                           [workstation]  evision=led0  ses_agent0003
  lane  1  done      QMK host-side RGB control protocol [workstation]  evision=led1  ses_agent0002
  lane  2  working   Keyboard status: LED daemon + open [workstation]  evision=led2  ses_agent0001
```

`--json` prints the raw `/status` for scripting, `--follow` reprints whenever
anything changes, and `--interval 0.5` makes it quicker.

**2. The API** — the same data, and the canonical source:

```
GET /status              every lane plus every device
GET /slots               every lamp in order, with its occupant or free
GET /session/<id>        just your own lane (404 if you do not hold one)
```

```json
{"devices": [{"label": "evision", "lamps": 126, "per_lamp": true,
              "lanes": ["led0", "led1", "led2"]}],
 "lanes": 12,
 "free": [3, 4, 5],
 "sessions": {
   "job-123": {"slot": 2, "key": "led2", "agent": "hermes", "label": "nightly sync",
               "host": "kimi", "state": "working", "age": 12.3}}}
```

**3. Watching for work that needs a human.** This is the useful one if you
orchestrate other agents: a lane in `blocked` is blinking red on a physical
keyboard because something is waiting for a person.

```python
import json, urllib.request

def lanes(base="http://127.0.0.1:8730", token=None):
    req = urllib.request.Request(base + "/status",
                                 headers={"X-LED-Token": token} if token else {})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())

data = lanes()
waiting = {sid: info for sid, info in data["sessions"].items()
           if info["state"] == "blocked"}
for sid, info in waiting.items():
    print(f"needs a human: {info['label']} on {info['host']} (lamp {info['key']})")
```

Prefer this to polling the human: the panel exists so they do not have to be
interrupted for everything.

### Rules

1. **Never block your real work on the panel.** Short timeouts, ignore failures.
2. **Transitions only.** Every state change repaints hardware.
3. **`blocked` is a promise.** Use it when you genuinely cannot proceed.
4. **Release your lane** with `/session/end` when you are done.
5. **Read `/status` rather than guessing** which lamps exist: devices differ, and
   one lane can be a different key on each keyboard.

### Publishing detail others can read

State is one word; detail is everything else. Send it separately, and it never
repaints hardware:

```
POST /session/info
{"sessionID": "<id>", "info": {
   "repo": "rgbagentflightindicator", "branch": "main",
   "tokens": {"input": 2400000, "output": 940000, "cost": 4.12},
   "context": {"entries": 473, "compactions": 3},
   "blocked_on": {"action": "bash", "resources": ["rm -rf build"],
                  "message": "delete the build directory?"},
   "children": [{"id": "ses_x", "label": "subagent task", "state": "working"}]}}
```

Use it to name the repository you are working in, to say **what** you are blocked
on rather than just that you are, and to list your own subagents — a child lists
here, under `children`, instead of taking a lane of its own. Partial updates
merge, so you can send one field at a time.

### Reading the detail of other lanes

The same information comes back, so you can see what the fleet is doing rather
than only whether it is busy:

```sh
rgi status --json | jq '.sessions[] | {label, state, in_flight_s, repo: .info.repo,
                                        blocked: .info.blocked_on.action, kids: (.info.children|length)}'
```

`GET /status` gives every lane with `in_flight_s`, `idle_s` and its `info`, which
is how you tell a lane that is *working* from one that has been *stuck for twenty
minutes*, or find the one that is blocked on a permission prompt you could
resolve yourself.
