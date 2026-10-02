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
git clone https://github.com/mojomast/rgbagentflightindicator
cd rgbagentflightindicator
pip install -e ".[sinowealth]"               # the extra is only for that board
python -m rgi detect                         # what can it see?
python -m rgi daemon                         # run the panel
python -m rgi ui                             # configure it in a browser
python -m rgi watch                          # report OpenCode sessions (optional)
```

The package is not on PyPI yet — install from the checkout above, or
`pip install "git+https://github.com/mojomast/rgbagentflightindicator"`. A machine
that only needs to *report* to somebody else's panel needs none of this: the
watcher is one published file (see `docs/machines.md`), and a panel serves a
built wheel of the client at `GET /files`, so a reporting machine can install
`rgi` without a registry.

`detect` prints the backends it found and how many lamps each exposes — and it is
**read-only**: it will not claim a device that something else is driving. `daemon`
starts the HTTP API and starts painting. `watch` makes OpenCode sessions appear
without the agents knowing anything about this project.

### Every connected keyboard, automatically

By default `daemon` uses **every supported keyboard it can see**, not just the
first one. Plug a second board in and it joins the panel; no configuration, and
no `--backend` needed:

```
[rgi] sinowealth: 126 lamps; lanes on `, 1, 2, 3, 4, 5, 6, 7, 8, 9, 0, -, =
[rgi] evision: 126 lamps; lanes on 0, 1, 2, 3, …
[rgi] listening on http://127.0.0.1:8730
```

A session is one *lane*, and each device maps lanes onto its own lamps — so the
same session can be key `1` on one board and `7` on another.

If a device can only show **one colour at a time** (a keyboard whose firmware only
exposes VIA's effect controls, or a laptop backlight), the panel does not pretend
otherwise: that device lights up in the colour of the **most urgent** lane —
blinking red if anything needs you, white if something just finished, green while
work is in flight. One lamp is still an honest indicator; twelve fake ones are not.

Choose explicitly if you prefer:

```sh
python -m rgi daemon --backend evision                 # one of them
python -m rgi daemon --backend evision --backend qmk   # exactly these two
```

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
| `evision` | EVision/SONiX boards: Magic Refiner, Redragon, Husky, EvoFox, Kreo, VGN/ATK and many others | Win/Linux/macOS via hidapi | **verified on hardware** (Magic Refiner MK 17, 320F:501D) |
| `qmk` | any QMK keyboard: **per-key** on Vial firmware, one colour on stock QMK+VIA | Win/Linux/macOS via hidapi | protocol implemented to QMK/VIA source; **not yet run against a board** |
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
in [docs/opencode.md](docs/opencode.md). Doing this **on other machines** — with
the install, update and verification steps, and what to say to the agent that does
it — is [docs/machines.md](docs/machines.md).

## Integrations

Beyond the OpenCode watcher, the panel can be driven by the harnesses and
frameworks that agents actually run in. They all speak the same
[Reporter](docs/integrations.md) — one lane per root workflow, attention waits
counted by id, ordered events, explicit free-slot claims — so they behave the
same way, and none of them needs a framework installed to import:

| integration | how it reports | doc |
|---|---|---|
| Claude Code | command hooks installed in `settings.json` | [docs/claude-code.md](docs/claude-code.md) |
| Gemini CLI | hooks in settings, or packaged as an extension | [docs/gemini-cli.md](docs/gemini-cli.md) |
| Pi | a small event-API extension | [docs/pi.md](docs/pi.md) |
| GitHub Copilot SDK | an adapter for sessions your application owns | [docs/copilot-sdk.md](docs/copilot-sdk.md) |
| Zoo Code CLI | the NDJSON stream a headless `roo --print --output-format stream-json` run prints | [docs/zoo-code.md](docs/zoo-code.md) |
| OpenAI Agents SDK | runner boundaries and lifecycle callbacks | [docs/openai-agents.md](docs/openai-agents.md) |
| Pydantic AI | run/event hooks, approvals vs background tools | [docs/pydantic-ai.md](docs/pydantic-ai.md) |
| LangGraph | interrupts, resumption, thread correlation | [docs/langgraph.md](docs/langgraph.md) |
| CrewAI | crew/flow lifecycle and human feedback | [docs/crewai.md](docs/crewai.md) |
| Microsoft Agent Framework | middleware and workflow events | [docs/ms-agent.md](docs/ms-agent.md) |
| ChatGPT and Codex | private stdio MCP adapter | [docs/openai.md](docs/openai.md) |

Two more integrations are displays rather than reporters: a [WLED](docs/wled.md)
strip is a backend like any keyboard, and [Home Assistant](docs/home-assistant.md)
gets an aggregate sensor plus transition events for your automations.

Hook-based integrations run through one command, `rgi hook <harness>`, which the
harness calls with one JSON payload on stdin; the per-harness settings examples
are on each page. The integration index with the supported-version matrix is
[docs/integrations.md](docs/integrations.md).

## Private ChatGPT and Codex testing

Install the optional MCP adapter with `pip install -e '.[openai]'`. It exposes
panel status and session tools through `python -m rgi mcp`. Use OpenAI Secure MCP
Tunnel to reach a workstation behind NAT from ChatGPT developer mode, or use a
local stdio plugin in Codex. The private plugin packager and complete connection,
testing, and cleanup steps are in [docs/openai.md](docs/openai.md).

## Wiring up agents

Give any agent [prompts/AGENT_PROMPT.md](prompts/AGENT_PROMPT.md) — or
[prompts/HERMES_PROMPT.md](prompts/HERMES_PROMPT.md), which also covers *reading*
the panel. It is self-contained: claim a lamp, report transitions, release it. The
daemon publishes its own copy at `GET /files/AGENT_PROMPT.md`, so you can hand
over one line instead of a wall of text.

```sh
curl -sS -X POST http://127.0.0.1:8730/session/start \
  -H "Content-Type: application/json" -H "X-LED-Token: $TOKEN" \
  -d '{"agent":"my-service","sessionID":"nightly-1","label":"nightly sync"}'
```

And anyone can see the whole fleet, from a shell or from an agent:

```sh
rgi status            # lane, state, label, host, and which lamp each one holds
rgi status --json     # the same, for scripting
rgi status --follow   # reprint on every change
```

```
rgbafi 0.6.0  http://127.0.0.1:8730
  device evision       126 lamps  per-key

  lane  0  done      Greeting                           [workstation]  evision=0  ses_agent0003
  lane  2  working   Keyboard status: LED daemon + open [workstation]  evision=2  ses_agent0001
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
GET  /ui/                   the web configuration UI (static, no token needed)
GET  /ui/api/*              live status, config, SSE, health, logs (X-LED-Token)
PUT  /ui/api/config         validate and apply a whole config revision
```

Lamps are the scarce resource — a keyboard has a dozen, not a thousand — so a
lane is held until the session releases it or a new session evicts it as
least-recently-used. The OpenCode watcher additionally releases lanes for
sessions it has not seen for `rgi watch --stale` seconds (two hours by default).
That is deliberate: the number should keep meaning the same session.

Full details, including the states and the error codes, are in
[docs/api.md](docs/api.md).

## Configuration

| Flag | Default | Meaning |
|---|---|---|
| `--backend` | all connected | repeatable: `sinowealth`, `evision`, `openrgb`, `sysfs`, `qmk`, `dummy`. Default = every supported keyboard that is present |
| `--host` / `--port` | `127.0.0.1:8730` | bind address; use `0.0.0.0` to accept agents from other machines |
| `--token` | `RGI_TOKEN` or `~/.config/rgi/token` | shared secret; **required** once you are not on localhost |
| `--count` | `12` | how many lanes to offer |
| `--lanes` | backend default | explicit lamp indices for the *primary* device's lanes |
| `--device` / `--leds` | `0` / detected | OpenRGB device index, lamp count override |
| `--no-quiet` | off | do not hold writes while you type (see below) |
| `--lane-map` | `~/.config/rgi/lanes.json` | which agent gets which lane |

### The web UI

`rgi ui` opens the configuration page: colours and effects for every state,
layouts and key names, lane pools, preferred lanes, the agents registry, and
the keyboard mapping wizard. Everything edits a browser draft; hardware is only
touched when you press **Apply**, or a time-boxed test button. The whole config
is one versioned file at `~/.config/rgi/config.json`, with backups and history
snapshots beside it. Full details: [docs/webui.md](docs/webui.md).

### Which agent gets which lane

By default a session takes the first free lane. To make an agent land on the same
key every time, map it:

```sh
rgi lane-map --set hermes-3=5 --set opencode=1      # then restart the daemon
rgi lane-map                                        # show the map
rgi lane-map --unset hermes-3
```

Keys are matched against an agent's **`ident`** first and its agent name second,
so it works whether the agent names itself or not. A mapping is *advice*, not a
fence: if the lane is taken the session gets a free one rather than being refused,
and an explicit `"slot"` in `/session/start` still wins (or fails honestly with
`409` if it is taken).

Agents identify themselves - `"ident"` at claim time, or through
`POST /session/info` - and that is what the mapping matches on:

```sh
curl -sS -X POST http://127.0.0.1:8730/session/start \
  -H "Content-Type: application/json" -H "X-LED-Token: $TOKEN" \
  -d '{"agent":"hermes","sessionID":"job-1","label":"nightly sync",
       "host":"kimi","ident":"hermes-3"}'
```

The panel reports the map in `/status`, so an agent can see the policy it is
subject to rather than guessing.

### Keeping a fleet in step

The panel publishes the plugin and the prompts at `GET /files`, with hashes, and
the prompts tell agents to **update from there rather than editing their local
copies** — a hand-edited plugin diverges from every other machine and is silently
overwritten by the next update. If you have agents on other machines, that rule is
in their prompts; see [docs/machines.md](docs/machines.md).

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
| [CONTRIBUTING.md](CONTRIBUTING.md) | how to add hardware, a plugin, or a fix — and what a pull request must prove |
| [docs/backends.md](docs/backends.md) | the landscape of RGB keyboards, and how to add one |
| [docs/protocols.md](docs/protocols.md) | the wire formats: Sinowealth, EVision, QMK/VIA, OpenRGB, LampArray |
| [docs/api.md](docs/api.md) | the HTTP API in full |
| [docs/webui.md](docs/webui.md) | the web configuration UI, the config schema, and the mapping wizard |
| [docs/agents.md](docs/agents.md) | wiring agents, watchers and other tools |
| [docs/machines.md](docs/machines.md) | running the panel across machines: install, update, verify |
| [docs/opencode.md](docs/opencode.md) | the OpenCode plugin, and its four sharp edges |
| [docs/troubleshooting.md](docs/troubleshooting.md) | symptoms, causes, fixes |

Prompts you can hand straight to an agent — they are also published by the panel
at `GET /files/<name>`, so a remote machine can fetch its own instructions:

| Prompt | For |
|---|---|
| [prompts/AGENT_PROMPT.md](prompts/AGENT_PROMPT.md) | any agent that should report its state |
| [prompts/HERMES_PROMPT.md](prompts/HERMES_PROMPT.md) | an agent that reports *and* reads the lanes |
| [prompts/PLUGIN_SETUP_PROMPT.md](prompts/PLUGIN_SETUP_PROMPT.md) | a machine with no plugin yet |
| [prompts/PLUGIN_UPDATE_PROMPT.md](prompts/PLUGIN_UPDATE_PROMPT.md) | a machine that already has one |

## Tests

```sh
python -m unittest discover -s tests -t .
cd rgi/webui && node --test        # browser modules; Node 18+, no npm install
```

No hardware and no network: the daemon is exercised against a dummy backend over
real HTTP, and the device protocols are checked byte for byte.

## Licence

MIT — see [LICENSE](LICENSE).
