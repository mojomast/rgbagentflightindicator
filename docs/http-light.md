# HTTP light sink

Not every desk light is a keyboard. blink(1), busylight, Luxafor and many DIY
lamps expose a small local HTTP API instead of a documented USB protocol - and
rgi already knows the hard part: which single colour the most urgent lane
should show. This backend turns that colour into one configurable request.

```sh
python -m rgi daemon --backend http-light --http-light-preset blink1
python -m rgi daemon --backend http-light --http-light-preset busylight
python -m rgi daemon --backend http-light \
  --http-light-url "http://127.0.0.1:1880/rgi?r={r}&g={g}&b={b}"
```

`per_lamp` is `False`: one physical light cannot hold twelve colours, so the
daemon collapses the lane set to the most urgent state's colour and repeats it
for each of the backend's lamps. `lamps()` returns 12 identical entries so the
daemon can track that many sessions on the one light - the same honest pattern
the QMK single-colour path uses.

## Presets

| preset | request |
|---|---|
| `blink1` | `GET http://127.0.0.1:8934/blink1/fadeToRGB?rgb=%23{hex}` |
| `busylight` | `POST http://127.0.0.1:8000/api/v1/lights/on` with `{"color": "#{hex}", "dim": 1.0, "led": 0}` |

For a blink(1), run `blink1-tiny-server` (from `blink1-tool`) and leave it
serving on :8934. For busylight, install `busylight-for-humans[webapi]` and
run `busyserve` (default port 8000). Point either at another host by passing
the full URL yourself; a preset only supplies defaults.

## Templates

The URL and body are rendered with exactly these placeholders:

| placeholder | value |
|---|---|
| `{r}` `{g}` `{b}` | decimal channels, 0-255 |
| `{hex}` | uppercase `RRGGBB`, for devices that take `23FF00` or `%23FF00` |
| `{on}` | `true` / `false` (black counts as off) |

Rendering is literal substitution, not `str.format`: `{r.__class__}` stays on
the wire as text and can never reach into the process. A `body_template` that
is a dict is always sent as JSON; a string body is sent as JSON when it
renders to a JSON object or array, and as literal text otherwise (so
`--http-light-template "{hex}"` sends a bare hex body). Timeouts are 1.5 s for
a write and 0.6 s for the probe.

## Configuration

Explicit constructor/CLI arguments win, then the environment:

- `RGI_HTTP_LIGHT_URL` - the full endpoint, placeholders allowed
- `RGI_HTTP_LIGHT_PRESET` - `blink1` or `busylight`

`available()` pings the configured origin with a cheap `GET /` and returns
`False` on any failure; it never raises. `open()` repeats the probe and raises
`BackendUnavailable` when nothing answers, and every failed `write()` raises
`BackendUnavailable`, so the daemon logs it, reconnects, and never pretends the
light was painted. The probe only touches the origin, never the templated
endpoint, so it cannot change the colour.

> **Opt-in.** All three new 0.8 backends (`lamparray`, `gamesense`,
> `http-light`) are listed in `rgi/backends/__init__.py` but marked opt-in
> until someone verifies them on real hardware, so auto-detection never opens
> this one. Start it explicitly with `--backend http-light`.

## Verified status and limitations

- **Not tested on hardware.** No blink(1), busylight or Luxafor was available.
  `tests/test_http_light.py` runs a local recording HTTP server and pins the
  preset URLs, the template output and the daemon's colour collapse; the mock
  records requests, it does not emulate any device.
- `close()` sends nothing: a status light keeps showing the last colour, which
  is the honest state until the panel paints again. Devices whose API needs an
  explicit off (rather than black) can be given a template that ends in the
  right request for `{on}`.
- A JSON string body must render to an object or array to be sent as
  `application/json`; a malformed snippet is sent as literal text rather than
  guessed at.
