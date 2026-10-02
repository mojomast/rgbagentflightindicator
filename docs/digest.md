# History and digest

The panel's `/status` is in memory, and the Home Assistant events are transient.
`rgi/history.py` is the durable, boring alternative: one JSON line per lane
event, one rotated backup, and a pure function that answers "what happened while
I was away?" without any service running.

```python
from rgi.history import HistoryLog, digest, text

history = HistoryLog("~/.config/rgi/history.jsonl", max_bytes=2_000_000, hub=hub)
# hub.subscribe(history.handle) is equivalent to passing the hub

summary = digest("~/.config/rgi/history.jsonl", since_epoch=last_night)
print(text(summary))
```

```
rgi digest: 3 lanes | 2 done, 0 error, 1 blocked (1 open) | $4.21 | longest wait 12m30s | hosts workstation=2, laptop=1
```

## Configuration

| key | default | meaning |
| --- | --- | --- |
| `enabled` | `false` | the integrator only constructs the log when true |
| `max_bytes` | `2000000` | rotate to `<path>.1` once the current file would exceed this |

The path is the integrator's choice; `~/.config/rgi/history.jsonl` matches where
the rest of the local configuration lives.

## The log

One JSON object per line, with exactly these keys:

```json
{"at":1780000000.123,"kind":"state","session":"opencode:fix-tests",
 "slot":2,"state":"blocked","host":"workstation","ident":"workstation",
 "label":"Fix tests","cost":1.42,"tokens_in":120345,"tokens_out":8012,
 "context_percent":71.5}
```

- `cost`, `tokens_in`, `tokens_out` and `context_percent` come from the lane's
  `info` (`tokens.input`, `tokens.output`, `tokens.cost`, `context.percent`, and
  the flat variants), and are `null` when the integration did not publish them.
- Rotating keeps exactly one backup: when the next line would push the current
  file past `max_bytes`, the file becomes `<path>.1` (replacing any old backup)
  and a fresh one starts. The footprint stays near twice `max_bytes`.
- Writes are append-only and every I/O error is swallowed. A full disk loses
  history; it never takes the panel down.
- The subscriber never raises into the event hub and never blocks it.

## `digest(path, since_epoch=None)`

Aggregates the current file and its backup, line by line, skipping anything that
is not a JSON object with a usable timestamp. `since_epoch=None` means all of
the history. The optional `now=` keyword (default `time.time()`) anchors waits
that are still open and exists so scripts and tests are deterministic.

| key | meaning |
| --- | --- |
| `lanes` | distinct sessions with at least one event in the window |
| `done` / `error` / `blocked` | transitions *into* those states; a repeated state is not a new transition |
| `blocked_open` | sessions whose last observed state in the window is `blocked` |
| `spend` | the latest cumulative cost reported per session, summed — never a sum over every line, which would multiply a running total |
| `longest_wait_s` | the longest blocked interval, closed by the next state change or an `end`, or still open at `now` |
| `by_host` | sessions per host (`unknown` when no host was ever reported) |

A lane that is released (`end` / `clear`) while blocked closes its wait at the
release, so a wait interrupted by a restart still counts.

`text(summary)` formats one short human line; spend is shown only when non-zero,
and the host list is sorted. If the file does not exist the summary is all
zeroes and the text says so.

## Reading it later

The digest is a pure function, so a morning report needs no daemon:

```python
import time
from rgi.history import digest, text

# everything since midnight local time
midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
print(text(digest("~/.config/rgi/history.jsonl", since_epoch=midnight)))
```

The natural next steps are a scheduled message (route it through the notifier's
command contract in `docs/notify.md`) or a "while you were away" line in a
dashboard. Both read the same file; neither needs rgi running.
