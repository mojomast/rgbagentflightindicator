# Wiring up agents

Anything that can send an HTTP POST can have a lamp. Three ways, easiest first.

## 0. Reading the panel (observability)

Before reporting anything, it helps to be able to *see* the lanes. Three ways, in
order of convenience:

```sh
rgi status                       # a table: lane, state, label, host, which lamp
rgi status --json                # the raw /status, for scripting
rgi status --follow              # reprint whenever anything changes
```

```
rgbafi 0.7.0  http://127.0.0.1:8730
  device evision       126 lamps  per-key

  lane  0  done      Greeting                           [workstation]  evision=0  ses_agent0003
  lane  1  done      QMK host-side RGB control protocol [workstation]  evision=1  ses_agent0002
  lane  2  working   Keyboard status: LED daemon + open [workstation]  evision=2  ses_agent0001
```

The same data is on the API (`GET /status`, `GET /slots`, `GET /session/<id>`), so
an agent can act on it. The most useful query for an orchestrator is **which lanes
are `blocked`** — those are the ones blinking red because something is waiting on
a human:

```python
data = GET /status
waiting = {sid: i for sid, i in data["sessions"].items() if i["state"] == "blocked"}
```

Prefer that to interrupting the human yourself: it is what the panel is for.
[prompts/HERMES_PROMPT.md](../prompts/HERMES_PROMPT.md) is written for an agent
that both reports and observes.

## 1. Hand the agent the prompt

[prompts/AGENT_PROMPT.md](../prompts/AGENT_PROMPT.md) is self-contained: how to
claim a lamp, which states to send, when to release. The panel also serves it, so
you can hand over one line instead of a wall of text:

```
Fetch and follow the instructions at
http://<panel-host>:8730/files/AGENT_PROMPT.md
sending the header  X-LED-Token: <token>
```

That is usually all it takes. The agent reports `working` while busy, `blocked`
when it needs you, `done` when it stops — and its lamp number appears in OpenCode
if you installed the plugin.

## 2. Wrap a command

Any shell has the pieces:

```sh
#!/usr/bin/env sh
# run a command with a lamp that reflects it
BASE=${BASE:-http://127.0.0.1:8730}
TOKEN=${TOKEN:-$(cat ~/.config/rgi/token 2>/dev/null)}
SID="cmd-$$"

api() { curl -sS -m 2 -X POST "$BASE$1" -H "Content-Type: application/json" \
        -H "X-LED-Token: $TOKEN" -d "$2" >/dev/null || true; }

api /session/start "{\"agent\":\"shell\",\"sessionID\":\"$SID\",\"label\":\"$1\"}"
api /session/state "{\"sessionID\":\"$SID\",\"state\":\"working\"}"
"$@"; rc=$?
api /session/state "{\"sessionID\":\"$SID\",\"state\":\"$([ $rc -eq 0 ] && echo done || echo error)\"}"
api /session/end   "{\"sessionID\":\"$SID\"}"
exit $rc
```

Every call is best-effort with a short timeout: the panel is an indicator, never
a dependency. If the daemon is down, the work still completes.

## 3. Write a watcher

A watcher reports sessions that are *already* running, without the agent knowing
this project exists. That is what `rgi watch` does for OpenCode:

```sh
rgi watch --url http://127.0.0.1:8730 --stale 7200
```

On a machine that does not have the client there is nothing to install: the same
watcher is published as one file that needs only Python 3.

```sh
curl -sS -H "X-LED-Token: $TOKEN" "http://<panel-host>:8730/files/rgi-watch.py" \
  -o /tmp/rgi-watch.py
python3 /tmp/rgi-watch.py --url http://<panel-host>:8730
```

It polls OpenCode's own API, claims a lamp for each session that starts working,
lands it when the turn ends, and releases it after two hours of quiet. It logs
every decision to `~/.config/rgi/watcher.log`:

```
[bind] ses_agent0001 -> 1  (in flight)
[land] ses_agent0001 -> 1  (turn finished)
[go]   ses_agent0001 -> 1  (working again)
[hold] ses_agent0001 -> 1  (needs you)
[free] ses_agent0001 -> 1  (idle > 120 min)
```

Copy the shape for anything else with a pollable status: a CI runner, a training
job, a queue.

### Design notes for watchers

These came from getting it wrong first:

- **Do not free a lamp because a turn ended.** The lamp means "this session", not
  "this turn". Free it on real inactivity, or you will hand the human's number to
  somebody else mid-conversation.
- **Ignore brief gaps.** Polling catches sessions between tool calls; a few
  seconds of grace stops the panel blinking "finished" every time the model
  thinks.
- **Re-claim, do not assume.** If the panel restarts, your lanes are gone — a
  `404` on `/session/state` is the signal. Re-claim *asking for the same lamp* so
  the number keeps meaning the same session.
- **Distinguish "waiting on a human" from "finished".** A permission prompt is
  not a completed task, and it is the one state worth interrupting someone for.
- **Log every decision.** A watcher is invisible when it works and baffling when
  it does not.

## Running it as a service

There is no init integration yet. The portable version:

```sh
nohup rgi daemon --host 0.0.0.0 --token "$(cat ~/.config/rgi/token)" >~/.config/rgi/daemon.log 2>&1 &
nohup rgi watch --url http://127.0.0.1:8730 >/dev/null 2>&1 &
```

systemd, launchd or a Windows scheduled task all work; the two rules are to bind
the daemon to localhost unless you need remote agents, and to set a token the
moment you do not.

## More than one machine

Agents on other machines can use the panel if you bind it beyond localhost:

```sh
rgi daemon --host 0.0.0.0 --token "$(cat ~/.config/rgi/token)"
```

Then give them `http://<that-machine>:8730` and the same token. Use a real
network address (a Tailscale name, a LAN hostname) rather than an IP where you
can: it survives address changes, and hosts display better than `100.x.y.z` on the
lanes. If a call arrives without a host name, the panel falls back to the caller's
address.
