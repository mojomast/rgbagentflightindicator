# Notifications

The panel already shows `blocked` to whoever is in the room. `rgi/notify.py` is
the half that reaches you when you are not: it subscribes to the lane event
stream, decides when a transition is worth interrupting someone for, and runs
one command you configure. Routing is your job — ntfy, Pushover, desktop toast,
Slack, a speaker — rgi just decides *when*.

```python
from rgi.events import EventHub
from rgi.notify import Notifier

hub = EventHub()
notifier = Notifier(
    hub,
    ["sh", "-c", 'curl -sS -H "Title: $RGI_STATE on $RGI_HOST" '
                 '-d "$RGI_LABEL: $RGI_WHAT" https://ntfy.sh/my-rgi-topic'],
    quiet_hours="22:00-07:00",
)
# the daemon publishes lane events to `hub`, and calls notifier.poll()
# on its tick so min_duration_s and repeat_s mature without a timer thread
```

## Configuration

The daemon hands these keywords straight through:

| key | default | meaning |
| --- | --- | --- |
| `enabled` | `false` | the integrator only constructs the notifier when true |
| `states` | `["blocked", "error"]` | states that may fire, on entry only |
| `min_duration_s` | `3.0` | the state must persist this long before a notification goes out |
| `cooldown_s` | `30.0` | at most one delivery per this many seconds, globally; a due notification is held, not dropped |
| `repeat_s` | `0.0` | one optional repeat after this many seconds while the state is still open (`0` disables) |
| `quiet_hours` | `""` | `"22:00-07:00"`, comma-separated ranges allowed; wraps midnight |
| `dry_run` | `false` | log what would happen, run no command — shadow mode |
| `command` | `null` | argv list; `null` means decisions are logged only |

## What fires

- **A transition, never a state.** Entering `blocked` or `error` queues one
  notification; detail updates (`info`, heartbeats) and repeated states do not
  re-fire.
- **Deduplicated by wait.** The key is `(session, lane.changed_at)`, falling back
  to the first pending request id. One wait, one notification.
- **Cancelled by change.** Any later `working`, `done`, `idle`, `error`, `end`,
  `clear` or ack drops the pending notification. Nothing arrives after the thing
  resolved.
- **Minimum duration.** A question answered in three seconds never rings. The
  timer is measured from the lane's own change stamp, so a notifier started late
  still knows how long the wait has been open.
- **Cooldown.** Two lanes going `blocked` together produce one delivery; the
  second waits until the cooldown passes and is delivered then if still blocked.
- **One repeat.** With `repeat_s > 0`, a wait still open after that many seconds
  gets exactly one reminder. A state change or ack cancels it.
- **Acks are silent.** While `lane.acked` is true a wait is suppressed, and the
  suppression sticks until the lane changes state.
- **Quiet hours.** During the configured local-time window every state except
  `blocked` is suppressed. Suppressed means dropped, not queued: quieter hours
  should not produce a burst at 07:00. `blocked` always bypasses quiet hours.
- **First observation counts as entry.** A notifier started beside an already
  blocked lane still fires once; that wait is real.

`handle(event)` is the hub subscriber and also calls `poll()`, so any later
event advances time. The daemon tick should call `poll()` as well (it is cheap
and never raises) so `min_duration_s` and `repeat_s` mature in quiet periods.

## The command contract

`command` is an **argv list** (a string is split with `shlex` for convenience).
The command runs in its own daemon thread with a hard 10-second timeout and
never blocks the event stream. It receives:

- the payload as JSON on **stdin**:

  ```json
  {
    "at": 1780000000.123,
    "event": "state",
    "session": "opencode:fix-tests",
    "state": "blocked",
    "slot": 2,
    "ident": "workstation",
    "label": "Fix tests",
    "host": "workstation",
    "what": "approve: Allow write?"
  }
  ```

- the same fields as environment variables, merged over the current
  environment:

  | variable | value |
  | --- | --- |
  | `RGI_EVENT` | the event kind (`state`, `info`, `ack`, …) |
  | `RGI_SESSION` | the session key |
  | `RGI_STATE` | `blocked` / `error` / … |
  | `RGI_SLOT` | the lane slot |
  | `RGI_IDENT` | the agent's identifier |
  | `RGI_LABEL` | the lane label |
  | `RGI_HOST` | the machine |
  | `RGI_WHAT` | the blocked-on detail (`action: message`) |

Only those bounded fields are sent. Prompts, transcripts and tool arguments are
never forwarded — the same discipline as the reporter and the Home Assistant
bridge.

Every decision (fire, held by cooldown, suppressed by ack or quiet hours,
dry-run, command failure, timeout) goes through `log(level, message)` when a
logger is supplied, so the daemon's Logs page shows exactly why it did or did
not interrupt. A command that crashes, times out or cannot be found is logged
and swallowed; the hub subscriber never raises.

## Recipes

`ntfy` (phone push), through a shell so the environment variables expand:

```json
{
  "command": ["sh", "-c",
    "curl -sS -H \"Title: $RGI_STATE · $RGI_LABEL\" -H \"Tags: warning\" -d \"$RGI_WHAT ($RGI_SESSION on $RGI_HOST)\" https://ntfy.sh/my-rgi-topic"]
}
```

`notify-send` (Linux desktop), through a shell so the environment variables
expand:

```json
{"command": ["sh", "-c", "notify-send -u critical \"RGI $RGI_STATE\" \"$RGI_LABEL — $RGI_WHAT\""]}
```

Windows toast or `msg` (the latter needs the Windows Pro `msg` command):

```json
{"command": ["cmd", "/c", "msg", "%USERNAME%", "RGI %RGI_STATE%: %RGI_LABEL% - %RGI_WHAT%"]}
```

A small wrapper script is the best option when the message needs logic; read
JSON from stdin and use the `RGI_*` variables for the subject.

Start every new configuration in `dry_run` mode for a day. The log then tells
you exactly what would have fired, which is the cheapest way to tune
`min_duration_s`, `cooldown_s` and quiet hours before anyone's phone buzzes.

## Limits and intent

- One global cooldown and one repeat per wait on purpose: the evidence from
  on-call practice and the survey is that more than a single reminder trains
  people to mute the channel.
- There is no acknowledgement *transport*. An ack set through the panel or MQTT
  silences the notifier; answering the agent is what clears the wait.
- The notifier is an observer. It holds no lane, sends no state and is never
  required for the panel to work.
