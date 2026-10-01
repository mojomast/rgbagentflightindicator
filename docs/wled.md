# WLED

[WLED](https://kno.wled.ge) turns an ESP board into an addressable-LED
controller and answers a complete HTTP JSON API. This backend makes a *segment*
of a WLED strip into an `rgi` panel: one lamp per pixel, all individually
addressable.

Use it for a desk strip, a shelf panel or a matrix that already runs WLED.

## Requirements

- WLED **0.13 or newer** (verified against 16.0.1 — see
  [Supported versions](#supported-versions)).
- The device reachable over the network. The JSON API is on by default; there
  is nothing to enable in the WLED UI.
- One segment reserved for `rgi`. Create it in the WLED UI or use an existing
  one; `rgi` only ever writes that segment, but while it runs it **freezes and
  clears** it.

## Configuration

Environment variables (highest priority):

| variable | required | meaning |
|---|---|---|
| `RGI_WLED_URL` | yes | base URL, e.g. `http://wled.local`; a bare host works too |
| `RGI_WLED_SEGMENT` | no | segment id to reserve (default `0`) |
| `RGI_WLED_PIXELS` | no | subset of the segment, e.g. `0-11`, `2,4-5`; default every pixel |

or `~/.config/rgi/wled.json`:

```json
{
  "url": "http://wled.local",
  "segment": 1,
  "pixels": "0-11"
}
```

If both are present, the environment wins. `rgi daemon` sees the backend as
available only when one of the two configures a URL — `available()` never
probes the network, so a strip that is off cannot slow detection down.

## What is on the strip

With segment `1` covering LEDs 30..42 of a 64-LED strip and no `pixels`
setting, `rgi` sees twelve lamps:

    px0, px1, ... px11        group "wled"

`pxN` is the pixel's position *inside the segment*: `px0` is segment offset 0,
which is LED 30 on the strip. That is also how WLED's `i` array counts, so a
lane on `px4` lights the fifth pixel of the segment and nothing else. With a
name on the segment (the `n` field), the lamps' group is `segment<id>` instead.

WLED's individual-pixel control ignores grouping, spacing, reverse and
mirroring, so lanes follow the segment's raw pixel order.

## Running it

```sh
python -m rgi detect --open
python -m rgi daemon --backend wled
python -m rgi map --backend wled      # light one pixel at a time
```

## What rgi does to the device

- **Ownership.** `open()` reads `/json/info` (LED count, firmware version) and
  `/json/state`, finds the reserved segment and remembers its full state. If
  the strip is off or global brightness is 0, `rgi` turns it on and gives it
  brightness, remembering those values too — WLED does not display individual
  pixels otherwise.
- **Frames.** Each render is one POST to `/json/state`:
  `{"seg":[{"id":1,"on":true,"bri":255,"i":["FF0000", ...]}]}`. Only the
  reserved segment id appears in it.
- **Restore.** `close()` POSTs the captured segment (colours, effect, on,
  freeze, geometry) back, and, if `rgi` had to power the strip on, the captured
  global `on`/`bri` as well. WLED never reports the individual-pixel buffer
  back, so a segment that was itself being driven by individual pixels is
  restored to its effect-mode state — WLED's own rule is that `i` output is not
  persistent.
- **Failure.** `write()` never blocks: the newest frame goes to a sender
  thread. One POST is in flight at a time, frames that arrive meanwhile
  coalesce (stale ones are dropped, not queued), and there is a 40 ms floor
  between requests — WLED's docs ask callers to serialise state updates and
  never call in parallel, and one small JSON POST is what a frame is. The
  request timeout is 1 s. A failure is logged once to stderr and the newest
  frame is retried every 2 s, so a strip that drops off the network is painted
  again by itself when it returns.
- **Realtime mode.** If WLED reports `live: true` (UDP/E1.31/DDP is painting
  the strip), `open()` refuses and says so rather than fighting the other
  controller.

## Supported versions

- **WLED 16.0.1** (current stable release): the wire format was verified
  against the official JSON API documentation at
  <https://kno.wled.ge/interfaces/json-api/> and the released source of
  `wled00/json.cpp`
  (<https://github.com/wled/WLED/blob/v16.0.1/wled00/json.cpp>), both fetched
  2026-10-01.
- The fields used (`seg[].id`, segment-relative `i`, `on`, `bri`, `frz`,
  `col`) exist in WLED 0.13 and later; `json.cpp` of 0.13.3, 0.14.4 and 0.15.3
  was spot-checked for the same parsing.
- **Validation status: simulated, not tested on hardware.** The backend and
  `tests/test_wled.py` were run against a mock WLED HTTP server (stdlib
  `http.server`) that records the exact JSON bodies; no WLED device was
  available while this was written, so none of it has been seen on a real
  strip.

### Limitations

- Not tested on hardware (see above): the mock reproduces the requests this
  backend sends, not WLED's rendering.
- No authentication: WLED's JSON API is open on the LAN. Put it behind
  something else if that is not acceptable; an `https://` URL works only behind
  your own TLS proxy.
- A frame carries every owned pixel, so very large segments (many hundreds of
  pixels) can exceed WLED's JSON buffer. Keep the reserved pixel count modest,
  or reserve a small `RGI_WLED_PIXELS` subset; WLED's docs suggest splitting
  beyond roughly 256 pixels.
- If `RGI_WLED_PIXELS` selects a subset, the rest of the reserved segment is
  still frozen and cleared while `rgi` runs — the whole segment is reserved.
- If the strip is unreachable when the daemon starts, the backend is skipped
  for that run, as every backend is. A strip that disappears later is retried
  automatically.
- One segment per backend instance; effects, presets and playlists on other
  segments are never touched.
- Global brightness still scales the lane colours, which is usually what you
  want on a shared strip, but `rgi` does not guarantee absolute brightness.
