# HID LampArray

HID LampArray is the open, vendor-neutral keyboard-lighting standard behind
Windows 11 Dynamic Lighting (USB usage page `0x59`, defined by USB-IF
HUTRR84). The device tells the host how many lamps it has, where they are,
what each is for, and - the useful part - **which key each lamp sits under**
(`InputBinding`, a Keyboard/Keypad page `0x07` usage). rgi needs no
calibration to find the number row: it looks for the lamp bound to `1`, `2`,
and so on.

This backend drives such a device directly over HID feature reports, on
Windows, Linux and macOS, with no vendor software and no OpenRGB. HID access
needs the optional `hidapi` package (`pip install hidapi`); nothing is
imported until the backend is opened.

## Running it

```sh
python -m rgi detect
python -m rgi daemon --backend lamparray
python -m rgi map --backend lamparray     # light one lamp at a time
```

`rgi detect` reports the backend as available whenever a HID interface with
usage page `0x0059` / usage `0x0001` is plugged in. It is opt-in until someone
confirms it on hardware - Windows Dynamic Lighting can own the same device -
so start it explicitly with `--backend lamparray`.

## What rgi assumes about the protocol

Report **ids** are assigned per device by its HID report descriptor, so the
backend reads the descriptor (`hid_get_report_descriptor`, which hidapi
exposes) and maps the HUTRR84 usages to ids. When a descriptor cannot be read,
it falls back to the ids from HUTRR84's own example descriptor - the same
values Microsoft's ArduinoHidForWindows sample and OpenRGB use:

| HUTRR84 usage | report | fallback id |
|---|---|---|
| `0x02` LampArrayAttributesReport | read lamp count, kind, `MinUpdateIntervalInMicroseconds` | `0x01` |
| `0x20` LampAttributesRequestReport | send one `LampId` | `0x02` |
| `0x22` LampAttributesResponseReport | position, purposes, level counts, `InputBinding` | `0x03` |
| `0x50` LampMultiUpdateReport | up to 8 × (`LampId`, RGBI) | `0x04` |
| `0x60` LampRangeUpdateReport | one colour for an id range | `0x05` |
| `0x70` LampArrayControlReport | `AutonomousMode` | `0x06` |

The **field order inside each report** is likewise the reference layout, which
this backend parses and builds little-endian:

```
attributes (22 B)  u16 count, u32 width/height/depth (µm), u32 kind,
                   u32 min_update_interval_us
lamp       (28 B)  u16 lamp_id, u32 x/y/z (µm), u32 update_latency_us,
                   u32 purposes, u8 r/g/b/intensity level counts,
                   u8 is_programmable, u8 input_binding
multi      (50 B)  u8 count, u8 flags, 8 × u16 lamp_id, 8 × u8 R,G,B,I
range       (9 B)  u8 flags, u16 start, u16 end, u8 R,G,B,I
control     (1 B)  u8 autonomous_mode
```

One deviation to know: HUTRR84's prose describes `InputBinding` as a 16-bit
usage, but its own sample descriptor (and OpenRGB's parser) carry it in 8 bits;
this backend assumes the 8-bit reference layout. A device that declares a
16-bit binding will not get key labels.

`LampUpdateFlags` bit 0 (`UpdateComplete`) is set on the last report of a
frame. A uniform frame is sent as one range report when the descriptor
declares one, otherwise it is chunked into multi-update reports of eight
lamps.

## What rgi does to the device

- `open()` reads the attributes report, then asks about every lamp id, then
  sends the control report with `AutonomousMode = 0` (leave autonomous mode)
  and sets `min_interval` from `MinUpdateIntervalInMicroseconds`.
- `write()` sends the full frame; `close()` sends `AutonomousMode = 1`, so the
  firmware effect comes back. Safe to call twice. Only feature reports are
  ever written - no firmware, DFU or bootloader traffic.
- A device that does not answer the attributes report raises
  `BackendUnavailable` with the report id it tried, rather than silently
  painting nothing.

## Verified status and limitations

- **Not tested on hardware.** There was no LampArray keyboard here. The
  protocol is checked by `tests/test_lamparray.py`, which builds every report
  byte by byte and drives `open()`/`write()`/`close()` against a fake
  transport and a constructed report descriptor. None of it has been seen on a
  real board.
- Windows Dynamic Lighting may own the device: if the OS service is painting
  it, the device can ignore third-party writes until the user turns Dynamic
  Lighting off for that device. Prefer one writer - rgi or the OS, not both.
- The exact report *layout* is assumed rather than parsed from the descriptor;
  only report ids are discovered. A board whose descriptor reorders fields
  will need the offsets taught here.
- `MinUpdateInterval` is used as-is; a device reporting nonsense (e.g. the
  unset `2^31`) will be obeyed.
