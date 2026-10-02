# The web configuration UI

Open it on the machine running the daemon:

```sh
rgi ui                  # opens http://127.0.0.1:8730/ui/#token=<token>
rgi ui --no-open        # print the URL instead (headless machines)
```

The same page is at `http://127.0.0.1:8730/ui/` — or at your tailnet address if
you bound the daemon to `0.0.0.0`. It edits **everything** the panel paints:
per-state colours and effects, layouts and key names, which keys carry lanes,
which agent lands on which lane, the agents registry, and the device settings.

The hardware is never touched until you press **Apply**, or press an explicit,
time-boxed **Test** button. Everything in between is a draft in your browser.

## The draft / Apply model

| Layer | Lives in | Touches hardware |
|---|---|---|
| Draft (every edit) | browser `sessionStorage`, undo/redo stack | no |
| Preview (diagram, swatches) | browser, mirrors the render maths | no |
| Test / mapping probe | daemon overlay consumed by the render loop | yes, time-boxed |
| Apply | `~/.config/rgi/config.json` | yes, on the next tick |

- Apply sends the whole draft as one `PUT /ui/api/config` with
  `If-Match: <revision>`. Two tabs editing at once cannot silently overwrite
  each other: the stale one gets a 409 and a choice of *Reload* or *Overwrite*.
- The server validates before writing. Errors (a colour that is not a colour,
  a lane beyond `settings.count`, a duplicate id) block Apply and point at the
  field. Warnings (two states that look alike, duplicate match rules) are
  advisory.
- `settings.host`, `settings.port`, `settings.count`, device `enabled` flags and
  endpoint parameters are **startup** concerns. The UI saves them and tells you
  a restart is required rather than pretending a hot reload happened.
- The old `~/.config/rgi/lanes.json` is imported once into
  `lanes.overrides`; the legacy file is left untouched. `rgi lane-map` still
  reads and writes it, so nothing breaks.

### Where the config lives

One versioned file: `~/.config/rgi/config.json`. Every field has a default, so a
hand-written file with one key works. Each successful Apply:

- bumps the monotonic `revision` (used for `If-Match`),
- rotates `config.json.bak.1` … `.bak.5`,
- writes a snapshot to `~/.config/rgi/history/<revision>.json` (last 50).

Unknown fields are preserved on write-back, so a config written by a newer
daemon is not stripped by an older browser tab. A file with a higher
`schema_version` than the daemon understands is refused, never downgraded.

Schema (abridged; see `rgi/webconfig.py` for the source of truth):

```jsonc
{
  "schema_version": 1,
  "revision": 17,
  "settings": { "host": "127.0.0.1", "port": 8730, "count": 12,
                "quiet": true, "quiet_ms": 1500, "reduce_motion": false },
  "appearance": {
    "blink": { "period_ms": 560, "duty": 0.5 },
    "states": {
      "working": { "label": "Working", "icon": "play", "pattern": "steady",
                   "color": "#00ff00", "brightness": 255 },
      "done":    { "label": "Done", "icon": "check", "pattern": "blink",
                   "color": "#ffffff", "brightness": 255, "cycles": 10,
                   "then": "steady" }
      // blocked, error, idle, off
    }
  },
  "devices":  { "sinowealth": { "label": "Desk", "enabled": true,
                                "layout": "sinowealth-default",
                                "lane_pool": [0, 1, 2, 3] } },
  "layouts":  { "sinowealth-default": { "format_version": 1, "source": "auto",
                "keys": [{ "lamp": 0, "label": "`", "code": "Backquote",
                           "x": 0, "y": 0, "w": 1, "h": 1,
                           "group": "number-row" }] } },
  "lanes":    { "overrides": [{ "id": "hermes-3",
                "match": { "ident": "hermes-3", "agent": "hermes" },
                "lane": 5, "enabled": true }] },
  "lamp_overrides": [{ "device": "sinowealth", "lamp": 12,
                       "mode": "static", "color": "#00aaff" }],
  "agents":   [{ "id": "opencode", "label": "OpenCode",
                 "match": { "agent": "opencode" }, "enabled": true }],
  "endpoints": [{ "id": "openrgb-local", "kind": "openrgb",
                  "url": "127.0.0.1:6742", "enabled": true }]
}
```

`pattern` is a deliberately bounded vocabulary: `off`, `steady`, `blink`,
`breathe`, with `period_ms`, `duty` (0–1) and `cycles` (`0` = forever), then
`steady` or `off`. No scripting language, so the browser preview and the daemon
renderer can be — and are — tested against the same fixture
(`rgi/webui/tests/fixtures/effects.json`).

## Pages

| Page | What it does |
|---|---|
| **Dashboard** | every lane live (SSE), per-state counts, clear/end actions, 15 s hardware test |
| **Devices** | display labels, enabled flags, layout choice, ordered lane pool, endpoint list |
| **Layout** | SVG keyboard: click/shift-click keys, edit label/code/position, import/export JSON and CSV, flash the selection |
| **Appearance** | per-state colour, pattern, period, duty, cycles, "then", brightness, label; presets; colour-blind and contrast warnings; static lamp overrides |
| **Mapping** | live sessions, preferred-lane rules, the lane × device pool matrix |
| **Mapping wizard** | find which key each lamp is: press-to-label walk, or webcam locate + name |
| **Agents** | registry of known agents/idents, observed identities, synthetic test lane |
| **Settings** | quiet mode and window, startup settings, import/export, resets |
| **Logs** | the daemon's last 500 events (write failures, reconnects, applies) |
| **Help** | shortcuts, glossary, links to the published integration docs |

## Auth and security

- The shell and its assets are static and contain no data, so they are served
  without a token. Every `/ui/api/*` call requires `X-LED-Token`.
- `rgi ui` puts the token in the URL **fragment** (`#token=…`), which browsers
  never send to a server or place in `Referer`; the page moves it to
  `sessionStorage` and strips the fragment. There are no cookies, so there is
  no CSRF surface and nothing for another site to ride on.
- Strict CSP from the daemon: `default-src 'none'; script-src 'self'; …`, no
  inline script or style, no CDN, no analytics, no font downloads. User data is
  rendered with `textContent`, never `innerHTML`.
- Static serving is path-confined to `rgi/webui/` (realpath check) with an
  allow-list of content types; traversal attempts are 404s.

## Live updates

`GET /ui/api/events` is a coalesced full-snapshot SSE stream. It is consumed
with `fetch()` + `ReadableStream` because `EventSource` cannot send the token
header; the parser also keeps a 2 s polling fallback that pauses while the tab
is hidden. The render loop never blocks on a slow client: streams are capped
(four tabs) and dead clients are reaped.

## Hardware tests and the mapping wizard

Hardware is only ever written by the one render-loop thread. Test and mapping
probes set a time-boxed overlay (`POST /ui/api/test`, `/ui/api/paint`) which the
loop consumes and then clears, restoring lane rendering. Mapping probes are
paced by the loop's tick (about 8 frames/s, under the measured keystroke-drop
threshold) and the paint endpoint refuses bursts.

The wizard has two honest paths:

1. **Manual (always available).** Light lamp *N*, press the key directly above
   it — the keyboard names itself via `KeyboardEvent.code`. Skip, back, and
   resume are supported; nothing is saved until *Save to draft*.
2. **Webcam assist (optional).** For locating lights: lock exposure if the
   camera allows it, capture a dark baseline, then light each lamp and find the
   brightest positive blob via differential luma frames. The camera says
   *where* a light appeared, never *what key it is*, so the webcam pass is
   followed by the same press-to-label naming pass. Detections are stored as
   `px`/`py` with a confidence, reviewed, and only then saved.

Camera notes: `getUserMedia` needs a secure context, so the webcam mode only
works when the page is opened at `http://localhost:8730/ui/` (or via HTTPS) —
not at a LAN/Tailscale address. Frames never leave the machine. Manual mode
works everywhere, including boards with opaque keycaps or no camera.

## Not in this version

Deliberately deferred, not forgotten: homography/template fitting against
standard form factors (60%/TKL/full), coded multi-frame capture, live endpoint
probing, and keyboard-only accessibility hardening beyond the diagram's roving
tabindex and the equivalent list views. The roadmap and evidence live in the
mapping research notes that shaped this feature; the manual path is the
ground truth those phases will be validated against.

## Tests

```sh
python -m unittest discover -s tests -t .        # Python, including the API
cd rgi/webui && node --test                      # pure JS modules (Node 18+)
```

The Python suite covers config migration/validation/atomic writes and every
`/ui/api` route against a real HTTP server. The JS suite covers the effect
maths against the shared fixture, colour/contrast helpers, the undo stack,
layout generation, and the webcam blob maths.
