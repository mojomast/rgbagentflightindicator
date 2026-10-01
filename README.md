# rgbagentflightindicator

Turn an RGB keyboard into an **annunciator panel for AI agent sessions**.

Each session claims one key. Its colour tells you, from across the room, what
every agent is doing — the same way a flight deck tells a pilot which systems are
live, which have failed, and which need attention.

```
   green    in flight
   white    complete (blinks when it lands, then holds)
   red      needs a human — blinking, so you notice
   off      everything else
```

```
   opencode  ──┐
               ├──►  rgi daemon  ──►  your keyboard
   any agent ──┘     (the panel)
```

Agents talk to the panel over a small HTTP API, so anything that can send a POST
can have a lamp: an editor plugin, a CI job, a shell script, a long-running
inference run.

## Quick start

```sh
pip install rgbagentflightindicator          # add [sinowealth] for that board
python -m rgi detect                         # what can it see?
python -m rgi daemon                         # run the panel
python -m rgi watch                          # report OpenCode sessions (optional)
```

`detect` prints the backends it found and how many lamps each exposes — and it is
**read-only**: it will not claim a device that something else is driving. `daemon`
starts the HTTP API and starts painting. `watch` makes OpenCode sessions appear
without the agents knowing anything about this project.

Nudge a lamp by hand:

```sh
python -m rgi push working --label "hello panel"
python -m rgi push done
```

## The bit that is actually hard

Almost every RGB keyboard is different, and most have no public protocol. This
project exists because the usual advice — "just use the vendor software" — does
not work when you want an indicator that *you* control.

Behind one interface, `rgi` speaks several very different protocols:

| Backend | Covers | Platform | Status |
|---|---|---|---|
| `sinowealth` | Sinowealth 258A:0049 boards (many white-label "gaming keyboards") | Win/Linux/macOS via hidapi | **verified on hardware** |
| `openrgb` | hundreds of keyboards, mice, cases — anything [OpenRGB](https://openrgb.org) supports | where OpenRGB runs | implemented to the documented SDK; needs a device to confirm per-board labels |
| `sysfs` | Linux LED-class devices: laptops with a keyboard backlight, boards exposing `:rgb:` LEDs | Linux | implemented, untested on hardware here |
| `dummy` | nothing — a fake panel for tests, demos and dry runs | everywhere | used by the test suite |

`rgi detect` reports what is present. If none of them fit your board,
[docs/backends.md](docs/backends.md) walks through writing one — usually 60
lines once you know how the vendor talks.

## Install the plugin, so the panel is visible in the editor

The panel is useful on its own, but inside OpenCode you also get a sidebar block
naming every lane. `prompts/PLUGIN_SETUP_PROMPT.md` is a ready-to-hand prompt for
an agent to do it; the short version:

```sh
cd ~/.config/opencode
npm install "@opentui/core@^0.5.14" "@opentui/solid@^0.5.14" "solid-js@^1.9.12"
mkdir -p plugins/rgi-panel
cp <repo>/plugin/*.ts plugins/rgi-panel/
```

Details, including the four non-obvious rules of OpenCode's plugin contract, are
in [docs/opencode.md](docs/opencode.md).

## Wiring up agents

Give any agent [prompts/AGENT_PROMPT.md](prompts/AGENT_PROMPT.md). It is
self-contained: claim a lamp, report transitions, release it. The daemon
publishes its own copy at `GET /files/AGENT_PROMPT.md`, so you can hand over one
line instead of a wall of text.

```sh
curl -sS -X POST http://127.0.0.1:8730/session/start \
  -H "Content-Type: application/json" -H "X-LED-Token: $TOKEN" \
  -d '{"agent":"my-service","sessionID":"nightly-1","label":"nightly sync"}'
```

## The HTTP API

```
GET  /status                every lane: state, key, agent, label, host, age
GET  /slots                 every lamp in order, with its occupant or free
GET  /session/<id>          which lamp a session holds (404 if none)
POST /session/start         {"agent","sessionID","label","host","slot"} -> {"slot","key"}
POST /session/state         {"sessionID","state"}   working|done|blocked|error|idle
POST /session/end           {"sessionID"}
POST /clear
GET  /files[/<name>]        published files, with hashes
```

Lamps are the scarce resource — a keyboard has a dozen, not a thousand — so a
lane is held until the session releases it, is evicted as least-recently-used, or
goes quiet for `--stale` seconds (two hours by default). That is deliberate: the
number should keep meaning the same session.

Full details, including the states and the error codes, are in
[docs/api.md](docs/api.md).

## Configuration

| Flag | Default | Meaning |
|---|---|---|
| `--backend` | `auto` | `sinowealth`, `openrgb`, `sysfs`, `lamparray`, `dummy` |
| `--host` / `--port` | `127.0.0.1:8730` | bind address; use `0.0.0.0` to accept agents from other machines |
| `--token` | `RGI_TOKEN` or `~/.config/rgi/token` | shared secret; **required** once you are not on localhost |
| `--count` | `12` | how many lanes to offer |
| `--lanes` | backend default | explicit lamp indices for lanes |
| `--device` / `--leds` | `0` / detected | OpenRGB device index, lamp count override |
| `--no-quiet` | off | do not freeze the frame while you type (see below) |

## Things that bite

Collected from hardware, not theory — the long form is in
[docs/troubleshooting.md](docs/troubleshooting.md):

- **Typing drops keypresses while the panel animates.** Many controllers repaint
  the entire panel on every write. The daemon freezes the frame on steady colours
  while you are typing (`--no-quiet` to disable).
- **A replug invalidates the device handle.** Writes appear to succeed and go
  nowhere. The daemon notices, reopens and repaints.
- **`subprocess` with `shell=True` and an argument list runs only the first
  element on POSIX.** `["opencode", "api", "get", path]` became a bare command
  that hung until timeout — and Windows hid it, because `cmd.exe` joins the list.
  Resolve binaries with `shutil.which()`; never use a shell here.
- **A malformed request body used to claim a lamp called "unknown".** Bad input is
  now refused with a reason instead of quietly consuming a scarce resource.

## Status and honesty

This grew out of one person's setup — a white-label Sinowealth board with no
vendor software — and was then generalised. The Sinowealth backend is verified on
that hardware. The OpenRGB backend is written against OpenRGB's documented SDK
and covered by unit tests for its packet framing and controller-data parsing, but
**it has not been run against a physical board**; `rgi detect --backend openrgb
--debug` prints what it parsed so a mismatch is diagnosable in one line. The same
applies to `sysfs`.

Bug reports that include `rgi detect` output and the backend name are genuinely
useful. Pull requests that add a backend are very welcome — one file, one entry
in the registry, and a note in your commit about what hardware you verified it
on.

## Documentation

| File | What |
|---|---|
| [docs/backends.md](docs/backends.md) | the landscape of RGB keyboards, and how to add one |
| [docs/protocols.md](docs/protocols.md) | the wire formats: Sinowealth, OpenRGB SDK, HID LampArray |
| [docs/api.md](docs/api.md) | the HTTP API in full |
| [docs/agents.md](docs/agents.md) | wiring agents, watchers and other tools |
| [docs/opencode.md](docs/opencode.md) | the OpenCode plugin, and its four sharp edges |
| [docs/troubleshooting.md](docs/troubleshooting.md) | symptoms, causes, fixes |

## Tests

```sh
python -m unittest discover -s tests -t .
```

No hardware and no network: the daemon is exercised against a dummy backend over
real HTTP, and the device protocols are checked byte for byte.

## Licence

MIT — see [LICENSE](LICENSE).
