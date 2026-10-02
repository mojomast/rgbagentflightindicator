# SteelSeries GameSense

SteelSeries Engine exposes a documented HTTP JSON API on loopback. Any program
can find the Engine by reading the `coreProps.json` file it writes, then POST
events to `{address}/game_event`. The `rgb-per-key-zones` device type with
`mode: bitmap` takes a 132-colour array interpreted as a 22×6 grid and maps it
to the nearest keys of the user's keyboard - per-key lighting with no SDK
binary, no HID handle, and no vendor code to ship.

This backend makes an Engine-managed SteelSeries keyboard an rgi panel:

```sh
python -m rgi daemon --backend gamesense
```

It is opt-in until someone confirms it against a real Engine and keyboard, so
auto-detection never opens it.

Engine must be installed and running. `coreProps.json` lives at:

| OS | path |
|---|---|
| Windows | `%PROGRAMDATA%\SteelSeries\SteelSeries Engine 3\coreProps.json` |
| macOS | `/Library/Application Support/SteelSeries Engine 3/coreProps.json` |
| Linux | `~/.config/SteelSeries Engine 3/coreProps.json` (where Engine is packaged) |

`RGI_GAMESENSE_CORE_PROPS=/path/to/coreProps.json` overrides the lookup.
`available()` is true only when the file exists **and** something answers at
its address, so a stale file from a previous boot does not make the daemon
open a dead backend.

## The 22×6 view

`LANE_TO_BITMAP` in `rgi/backends/gamesense.py` is rgi's **own** approximate
layout of a standard full-size ANSI keyboard; it was not copied from any
vendor table or third-party mapping. The first colour of the 132 is the top
left, the 23rd starts the second row (Engine's documented serialisation).
Cells rgi does not own are sent black; Engine ignores cells that map to no
key, and maps each cell to the nearest key that exists. Because the grid is
coarser than many keyboards, a lane can land one key off - that is Engine's
mapping, not a per-key protocol.

Lamps are one per mapped grid cell. The number row (`` ` 1 2 3 4 5 6 7 8 9 0
- = ``) is group `number-row`, so it is the default lane pool, exactly as on
rgi's other keyboard backends. Keys with no `InputBinding` equivalent are just
grid positions here - Engine does the mapping.

## What rgi sends

- `open()` POSTs `/bind_game_event` with `game: "RGI"`, `event: "LANES"`,
  `value_optional: true` and a handler
  `{"device-type": "rgb-per-key-zones", "mode": "bitmap"}`.
- `write()` POSTs `/game_event` with
  `{"game": "RGI", "event": "LANES", "data": {"frame": {"bitmap": [...132...]}}}`.
- While open, a keepalive thread POSTs `/game_heartbeat` every 10 s; Engine
  deactivates a game after ~15 s without an event, so a steady frame would
  otherwise go dark.
- `close()` POSTs `/remove_game_event`, so the keyboard's own lighting comes
  back promptly instead of after the timeout.

Every failed request raises `BackendUnavailable`, so the daemon logs it and
reopens the backend rather than pretending the keys were painted. The 1 s
timeout keeps a missing Engine from stalling the panel.

## Verified status and limitations

- **Not tested on hardware or against a real Engine.** There was no
  SteelSeries keyboard or Engine install here. `tests/test_gamesense.py` runs
  a local mock Engine and pins the binding, heartbeat, frame and removal
  bodies; none of it has been seen on a real board.
- Engine rewrites `coreProps.json` and changes its port when it restarts; the
  backend re-reads the file on every `open()`, which is also what the daemon's
  reconnect loop calls after a write fails.
- Only one writer should own the keyboard. If Engine itself is playing an
  effect for another game, GameSense gives priority to whichever game most
  recently sent events; stop the other integration if the panel looks wrong.
- The bitmap deliberately does not use `partial-bitmap`; rgi paints the whole
  mapped layout each frame and leaves everything else black.
