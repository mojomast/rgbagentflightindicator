# Protocols

Wire formats, as recovered or as documented by their owners. Where something was
established by experiment it says so; where it is unverified it says that too.

## Sinowealth `258A:0049`

The board this project started on: a white-label "Gaming Keyboard" sold under
several names, with no vendor software in existence. Everything below was
recovered by probing.

### Interfaces

```
Col05   6-byte command reports      carries the mode unlock
Col06   1032-byte feature reports   carries LED data
Col03   looks plausible, accepts writes, ignores every one of them
```

Picking `Col03` is the canonical failure mode: writes succeed, nothing lights.

### Command

```
05 83 B6 00 00 00        unlock / initialise
```

### Frame layout (1032 bytes)

```
  0 ..  7   report header
 29 ..154   blue  plane, 126 bytes
155 ..280   green plane, 126 bytes
281 ..406   red   plane, 126 bytes
407 ..1031  padding, zero
```

Two reports carry the panel; both must be written or half the board keeps its
previous colour:

```
06 09 BC 00 40 00 00 00   per-key bank 1
06 09 C0 00 40 00 00 00   per-key bank 2
06 03 B6 00 00 00 00 00   mode commit
```

Colour storage is **planar**: one byte per key inside each of the three planes,
so a key's channels are 126 bytes apart rather than adjacent. Writing every byte
of a plane therefore colours every key without knowing the key map — the fringes
are in the *identities*, not the layout.

### What is known, and what is not

| Fact | Confidence |
|---|---|
| banking above, plane offsets | verified on hardware |
| slot 0 is the backtick key; slots 0-12 are `` ` 1 2 3 4 5 6 7 8 9 0 - = `` | verified by walking one slot at a time |
| slots 13-125 are further real keys | certain, but their identities are unmapped |
| the function row appears to be split across both banks, with a firmware effect bleeding through | observed, unexplained — it is why this project uses the number row |

Probing scripts that established the above (chase, banks, all-white) are worth
recreating before you trust a new board's map: `rgi map` does the walk, and the
notes above say what to look for.

### Behaviour that shapes the software

- **Every write blanks and repaints the whole panel.** A needless update is a
  visible flash, and while the controller is busy it can drop keypresses. Hence
  the daemon's change-detection and the typing-aware quiet mode.
- **The firmware cannot absorb back-to-back reports.** ~13 ms between writes,
  with one retry: the first attempt often returns short.
- **The mode commit restarts the lighting engine,** so it is sent once at open,
  not per frame.
- **A replug invalidates the handle.** Writes to the stale handle appear to
  succeed and go nowhere.

## OpenRGB SDK

A small binary protocol over TCP (port 6742), which is why one backend covers so
many keyboards. Implemented in `rgi/backends/openrgb.py`.

### Packets

```
header = "ORGB" | u32 device index | u32 packet id | u32 body length   (little endian)
```

| id | packet |
|---|---|
| 0 | request controller count |
| 1 | request controller data |
| 40 | request protocol version |
| 50 | set client name (bare NUL-terminated string, *not* length-prefixed) |
| 1050 | update LEDs |
| 1100 | set custom mode (do this before painting) |

`update_leds` body: `u32 data_size` (duplicated), `u16 count`, then **4 bytes per
LED** — `R G B 0`.

### Controller data

The response is one long block, and its shape depends on the protocol version the
server reports. The parser follows
[OpenRGBSDK.md](https://github.com/CalcProgrammer1/OpenRGB/blob/master/Documentation/OpenRGBSDK.md):

```
type, name, [vendor if proto>=1], description, version, serial, location
mode_count, active_mode, modes[]
zone_count, zones[]
led_count, leds[]
```

with per-mode brightness fields only from protocol 3, and zone segments only from
protocol 4. Two places on the wire are ambiguous enough to guess wrong: the
**mode colour list** and the **matrix map**. `parse_controller_data()` therefore
tries each variant and keeps the one that consumes the block *exactly* — a wrong
guess essentially never lands byte-perfect — and reports every attempt if none
do. `rgi detect --backend openrgb --debug` prints what it decoded; `--leds N`
skips parsing entirely.

Zone names are what make lanes meaningful: OpenRGB names zones after the keys or
the area, so a zone called `Number Row` of 13 LEDs maps onto 13 consecutive lamp
indices, and those become the lane pool.

## HID LampArray (usage page `0x59`)

The open standard behind Windows 11 Dynamic Lighting, requested by Microsoft as
HUTRR84. Worth supporting because it is the only vendor-neutral, OS-blessed way
to reach a gaming keyboard's LEDs. Not yet implemented here; this is the summary
you need to write it.

### Reports

Feature reports (report IDs are per-device — parse the descriptor):

| usage | report | contains |
|---|---|---|
| `0x02` | LampArrayAttributesReport | lamp count, bounding box, kind, min update interval |
| `0x20` | LampAttributesRequestReport | which lamp to ask about |
| `0x22` | LampAttributesResponseReport | position, purposes, channel counts, `InputBinding` |
| `0x50` | LampMultiUpdateReport | per-lamp colours (lamp ids + RGBI) |
| `0x60` | LampRangeUpdateReport | one colour for a range of lamp ids |
| `0x70` | LampArrayControlReport | `AutonomousMode` — take or hand back control |

`InputBinding` is the gift: for a keyboard it is the USB HID *Keyboard/Keypad
usage* of the key that lamp sits under (usage page `0x07`). That means **you can
find the number row without any calibration** — look up the lamp whose binding is
`0x1E` ("1"), `0x1F` ("2"), and so on, rather than guessing indices.

Notes that matter:

- Lamps are addressed by id, not index-into-a-frame; `LampMultiUpdateReport` can
  update a subset in one report.
- Devices fall back to firmware effects in *autonomous mode*; send
  `LampArrayControlReport` first if you want full control.
- Colours are RGBI (intensity is a separate channel), and each lamp declares how
  many levels of each channel it supports.
- A device that is being driven by Windows Dynamic Lighting may ignore you until
  the user opts out for that device.

Reference: [HUTRR84](https://www.usb.org/sites/default/files/hutrr84_-_lighting_and_illumination_page.pdf),
[Dynamic Lighting devices](https://learn.microsoft.com/en-us/windows-hardware/design/component-guidelines/dynamic-lighting-devices),
and Microsoft's [ArduinoHidForWindows](https://github.com/microsoft/ArduinoHidForWindows)
for a complete worked report descriptor.

## EVision (SONiX) - Magic Refiner, Redragon, and many others

The second family this project drives, and the protocol behind the Magic Refiner
MK 17 (USB `320F:501D`) that is the reference board here. Recovered from
OpenRGB's `EVisionKeyboardController` (`b9309c61`) and then **confirmed on
hardware**: the board answers the v2 capability query on usage page `0xFF1C` with
`aa 55`, reports a 126-lamp map, and accepts v2 direct-colour packets.

### Interface

```
usage page 0xFF1C, interface 1     output reports only - no feature reports
```

Both OpenRGB detectors key on the usage page and interface number, and ignore the
usage itself. The keyboard boot collection on the same device is not used.

### Framing

Every message is one 64-byte output report:

```
  [0]     report id, always 0x04
  [1..2]  checksum, little endian = sum(bytes[3..63])
  [3]     command
  [4]     payload size
  [5..6]  payload offset, little endian
  [7]     unused
  [8..63] payload, at most 56 bytes
```

A device that speaks **v2** replies to every packet with a 64-byte response that
echoes the report id, the command, the offset and *the request's checksum*, with a
status byte at `[7]`. Our board does exactly this — which is how the protocol was
identified:

```
sent:  04 0a 00 03 07 00 00 00 ...          read capabilities
reply: 04 0a 00 03 07 00 00 00 aa 55 00 00 0d 7e 50 ...
                                            ^^^^^     ^^^^^^ map_size = 0x7e = 126
```

### Commands

| op | meaning |
|---|---|
| `0x01` / `0x02` | begin / end configuration (wraps stored-profile writes) |
| `0x03` | read capabilities |
| `0x05` / `0x06` | read / write config (profiles, mode, brightness, colour) |
| `0x0A` / `0x0B` | read / write stored custom colours |
| `0x12` | dynamic colours (direct): RGB triples at absolute byte offsets, 56-byte chunks |
| `0x13` | end dynamic colours - hand the board back to its own firmware |

Colour order is **RGB**, stride 3, no per-packet header beyond the 8-byte one
above. v1 uses the same header with different commands (`0x06` mode, `0x11`
colours, 54-byte chunks); a v1 board simply does not answer `0x03`.

### Two details that matter in software

- **Refresh or lose it.** While the board is being driven it wants a nudge about
  every 200 ms: command `0x12` with a single zero byte at offset 18. Without it
  the firmware drifts back to its own effect. `rgi`'s backend keeps a small
  thread for this.
- **Direct mode needs no mode packet.** Unlike the stored profiles, writing
  `0x12` packets is enough — which is ideal for a status panel: no profile is
  consumed and nothing is saved to the keyboard.

### What is not known

The lamp *index* map is not published for this board: the protocol addresses LEDs
by index, but not which index is which key. OpenRGB's v2 driver ships a 106-entry
map for its own devices; ours reports 126. Use `rgi map` to walk them and note
which index sits under which key, then pass `--lanes` with the row you want.

Related: this same device is [OpenRGB issue #4148](https://gitlab.com/CalcProgrammer1/OpenRGB/-/issues/4148)
(reported, still open). The vendor app is the generic eVision OEM utility rebadged
for the seller, and the MCU is a SONiX `SN32F248` (`VS11K09A`), which is also
QMK-portable via [SonixQMK](https://sonixqmk.github.io/SonixDocs/) if you are
willing to open the case.

## Other routes, briefly

- **SteelSeries GameSense** — HTTP JSON on `localhost:27301`. Easiest vendor API
  of the lot; a backend would be ~50 lines and needs no dependency.
- **Razer Chroma SDK** — Windows-only REST/websocket API; on Linux, OpenRazer
  exposes the same devices through sysfs.
- **Logitech G** — Logi LED SDK: `LogiLedSetLighting` for whole-device,
  `LogiLedSetLightingForKeyWithKeyName` for per-key, called through ctypes.
  Linux: `g810-led` or libratbag.
- **QMK/VIA** — raw HID with a documented protocol; per-key colour is native.
- **Linux LED class** — `/sys/class/leds/*`, colour through `multi_intensity`
  where the kernel exposes it, brightness only otherwise.

## Safety

Every backend here writes **LED feature reports only**. No backend touches
firmware, DFU or bootloader interfaces, and none is required to: a keyboard that
has never seen this project returns to its own lighting after a replug. Keep that
property when you add a backend — a status panel is not worth a bricked board.
