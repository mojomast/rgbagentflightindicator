# The web configuration UI

Open it on the machine running the daemon:

```sh
rgi ui                  # opens http://127.0.0.1:8730/ui/#token=<token>
rgi ui --no-open        # print the URL instead (headless machines)
```

The page is a **map of your keyboard**. Keys, base/underglow strips and logo
lamps are drawn as they are configured, painted with the colours the daemon is
writing, and edited directly. Hardware is never touched until you press
**Apply**, or a time-boxed **test** button.

## What is on each page

| Page | What it does |
|---|---|
| **Live** | the map, the agent table with ticking timers, and a state-derived setup checklist |
| **Layout** | drag/resize keys, rename them, press-to-label codes, zones, lamp tray, import/export |
| **Paint** | brush static overrides and lane membership onto the map; zone overrides; pool order |
| **Mapping** | preferred-lane rules, live sessions with one-click pin |
| **Identify** | the mapping wizard: manual press-to-label, or webcam assist with review |
| **States** | colour, pattern, timing, brightness and presets for every agent state |
| **Devices / Agents / Settings / Logs / Help** | capability facts, registries, config, diagnostics |

The map is always drawn from the **draft**, so what you see is what Apply will
write. Live sessions are an overlay on it.

### The map

- **Keys and zones.** A layout has keys (physical identity + geometry), lamps
  (addressable slots with a kind) and zones (ordered groups; a perimeter zone
  is a path with its lamps ordered along it). A lamp can be a key, a
  perimeter/base LED, a logo, an indicator, an accent or unknown; a key can
  have several lamps; a lamp can have no key.
- **Honest defaults.** When nothing is known the map shows an "approximate
  grid" banner. A built-in profile ships key geometry only — it never claims a
  lamp index. Unverified or low-confidence lamps are dashed in the editor.
- **Zoom and zones.** `−` / `%` / `+` zoom, zoom remembers per device, zone
  chips above the map show/hide groups.
- **Keyboard access.** The map is one tab stop with an `aria-activedescendant`
  cursor; arrows move, Enter/Space selects, Shift extends. Each lamp is an
  option with a full label (`"Space — lamp 122, 2 lamps"`). The Layout page
  also carries a **lamp table** — filterable, with Flash and Select per row —
  as the equivalent editor for screen readers and bulk work.

### Editing on the map

- **Layout:** drag moves with 0.25u snapping (`Ctrl` disables it), the
  inspector has numeric x/y/w/h, arrow keys nudge, multi-select aligns left/top,
  lamps without a key wait in the tray, and deleting a key returns its lamp to
  the tray rather than forgetting the lamp exists. Perimeter zones have
  draggable corner handles.
- **Paint:** Override paints a static colour (a lamp override), Lane appends to
  the lane pool in click order, Erase removes overrides. Zone overrides paint a
  whole strip with one colour. The pool order can be rearranged, or rebuilt in
  reading order from the selection.
- Every gesture is one undo entry; `Ctrl+Z` walks them back.

## The config file

One versioned file: `~/.config/rgi/config.json`. Every field has a default, so
a hand-written file with one key works. Each successful Apply bumps `revision`,
rotates `config.json.bak.1` … `.bak.5`, and keeps a snapshot in
`~/.config/rgi/history/`. Unknown fields survive write-back; a file with a
newer `schema_version` is refused, never downgraded.

Schema 2 splits a layout into keys, lamps and zones:

```jsonc
{
  "schema_version": 2,
  "layouts": {
    "mk17-measured": {
      "format_version": 2,
      "source": "mixed",
      "name": "MK 17",
      "canvas": { "margin": 0.6 },
      "case": { "x": -0.55, "y": -0.55, "w": 19.55, "h": 8.05, "rx": 0.4 },
      "keys": [
        { "id": "k16", "code": "Backquote", "label": "`", "group": "number-row",
          "geometry": { "type": "rect", "x": 0, "y": 1.5, "w": 1, "h": 1 } }
      ],
      "lamps": [
        { "index": 16, "kind": "key", "key": "k16",
          "source": "webcam", "confidence": 0.97, "verified": true,
          "pixel": { "points": [[412, 288]], "confidence": 0.97 } },
        { "index": 118, "kind": "perimeter", "zone": "base",
          "path": { "zone": "base", "t": 0.412 },
          "source": "webcam", "confidence": 0.88 }
      ],
      "zones": [
        { "id": "base", "label": "Base / underglow", "kind": "perimeter",
          "geometry": { "type": "path", "closed": true, "width": 0.22,
                        "points": [[-0.7,-0.7],[19.7,-0.7],[19.7,8.2],[-0.7,8.2]] },
          "lamps": [116, 117, 118, 119], "ordering": "cw", "source": "webcam" }
      ]
    }
  },
  "devices": { "evision": { "layout": "mk17-measured",
                            "lane_pool": [16, 17, 18, 19] } },
  "zone_overrides": [{ "device": "evision", "zone": "base",
                       "mode": "static", "color": "#00aaff" }],
  "profiles": { "evision-320f-501d": { "enabled": true } }
}
```

Geometry shapes: `rect` (`x y w h`, optional `rotation`), `point`
(`x y size`), `path` (`points`, `closed`, `width`) and `union` (`shapes`).
Coordinates are keyboard units, may be negative, and the case and perimeter
paths deliberately live outside the key grid.

`source` records how a fact was obtained: `firmware`, `measured`, `webcam`,
`press`, `hand`, `import:kle`, `import:qmk`, `import:vial`, `builtin-profile`,
`auto`, `mixed`. The UI never merges guessed and measured data silently:
`builtin-profile` and low-confidence lamps are flagged wherever they are used.

### Migrating from schema 1

Migration is pure-add: a v1 key gains an `id` and `geometry` copied from
`x/y/w/h`, each v1 `lamp` becomes a lamp record with `kind: "key"`, and empty
`zones`/`canvas`/`meta` are defaulted. Legacy fields are preserved. A v1
`lanes.json` is imported once into `lanes.overrides`.

## Device profiles

Profiles live in `rgi/profiles/data/` and are suggestions, never facts. They
match on backend + lamp count; a profile is only applied when you use it or
enable it. The shipped Magic Refiner MK 17 profile (`evision-320f-501d`) carries
an 87-key ANSI TKL **geometry template** and nothing else:

- `lamp_count` 126 is the size of the EVision direct-colour buffer, not proof
  of 126 physical emitters.
- Whether the base LEDs are individually addressable (per-slot `0x12` writes)
  or only the firmware's `EDGE` parameter is an open question.
- Base ordering is unmeasured; the wizard measures it or you assign it by hand.

The EVision v2 protocol documents a read-only `0x1b` "physical map" command that
could fill key identities directly on boards that answer it. It is recorded as a
probe in the profile but not yet implemented (it needs hardware to confirm the
reply format).

## Identify: teaching the map

**Manual mode is ground truth and works everywhere.** The board lights one lamp
at a time; you press the key it is under and the keyboard names itself via
`KeyboardEvent.code`. Skip and back are supported; results go to Review, then
the draft.

**Webcam assist** adds evidence, never final answers:

1. References: all lamps off (dark), then all on at 60% (all-on). The probe
   frame tells us whether there is per-key structure at all.
2. Calibration: click the four corners of the key area on the video; a
   normalized four-point homography maps keyboard units to image pixels and the
   overlay shows where each key is expected. Keycap-grid detection
   (projection-profile periodicity) reports how regular the board looks.
3. Sequential evidence: each lamp is painted for a moment, captured, and
   differenced against the dark frame. Blob statistics (area, elongation,
   peak, energy) produce a position and an SNR.
4. Classification: compact blob inside a key cell → **key**; blob on the case
   path → **perimeter** with an arc-length `t`; compact blob inside the case
   away from keys → **logo**; otherwise **unknown**. Perimeter lamps are
   ordered along the path and the wizard offers a two-click
   "reverse direction" plus a neighbours test.
5. Review: a table with source and confidence; every row can be named by
   pressing its key, marked as base, or flashed again. Nothing is saved until
   **Save to draft**; the normal Apply review follows.

Camera guidance: mount the camera near top-down (15–30° tilt) with the whole
board plus a margin in frame (the underglow halo lives outside the case), lock
exposure/white balance/focus if the camera offers it, dim the room, and use a
matte surface. The webcam needs a secure context: open the page at
`http://localhost:8730/ui/` (or HTTPS), not at a LAN/Tailscale address. Frames
never leave the machine and the stream stops when you leave the wizard.

## Live data and the draft

- `GET /ui/api/events` is a coalesced SSE snapshot stream consumed with
  `fetch()` (because `EventSource` cannot send the token header), with a 2 s
  polling fallback that pauses while the tab is hidden.
- Timers tick client-side from `changed_at`/`idle_at` anchors, so they never
  freeze on a stale snapshot.
- **Context and cost** appear in the lane table when the reporter sends them:
  `42% · 128k` and `$4.12`, amber from 70% and red from 90% context. They are
  annotations, never new lane states.
- **Ack** per row (`POST /session/ack`): a done lane dims to idle without being
  released, and a blocked or errored lane keeps its colour but stops blinking
  and stops re-notifying. A new state or a new wait re-arms it; a real wait is
  never auto-expired.
- **Stale, not wrong:** when a working lane has sent nothing (state, info or
  heartbeat) for `settings.stale_s` (default 900 s), the row gets a `stale?`
  marker whose tooltip says the reporter may have died and that the state shown
  is the last one sent. The lamp is never changed by staleness alone.
- **While you were away**: the Live page offers a digest card from
  `/ui/api/history` - counts, spend and the longest wait since your last visit.
  "Mark all seen" moves the baseline. The underlying ring is the JSONL file
  `~/.config/rgi/history/events.jsonl`, also readable with `rgi digest`.
- **Reach** (Settings): the notify, MQTT, OTLP and history blocks are shown
  read-only for now, including the exact notification command that would run.
  Enabling them is a config + daemon-restart change.
- The draft autosaves to `sessionStorage` (debounced) and offers to restore
  after a reload; the dirty bar counts real changes and Apply is disabled when
  clean. Apply shows a grouped diff review; a stale `If-Match` gives
  Reload/Overwrite, never a silent merge.
- `settings.host`, `settings.port`, `settings.count`, device `enabled` flags
  and endpoint parameters are startup concerns; the UI says a restart is
  required instead of pretending otherwise.

## Auth and security

- The shell and its assets are static and carry no data, so they need no token.
  Every `/ui/api/*` call requires `X-LED-Token`.
- `rgi ui` puts the token in the URL fragment (`#token=…`), which is never sent
  to a server or a `Referer`; the page stores it in `sessionStorage` and strips
  it. No cookies, no CSRF surface.
- Strict CSP, no inline script or style, no CDN, no analytics; user data is
  rendered with `textContent`, never `innerHTML`.

## Tests

```sh
python -m unittest discover -s tests -t .        # Python, including the API
cd rgi/webui && node --test                      # pure JS modules (Node 18+)
```

The JS suite covers the shared effect fixture (Python/JS parity), topology and
path maths, the colour and confidence helpers, the homography solver, lattice
detection, blob statistics, perimeter ordering and the diff/undo logic.

## Not in this version

Deliberately deferred: coded/binary fast capture (sequential is the evidence
default), lens-distortion self-calibration, automatic profile matching by
VID:PID, and an OpenCV.js optional path. The manual path is the ground truth
those phases are validated against.
