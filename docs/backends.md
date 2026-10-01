# Adding support for a keyboard

RGB keyboards have no common standard. This document is the map: what to try
first for a given brand, and how to write a backend when nothing fits.

## Try these first, in order

1. **OpenRGB supports it.** Check the
   [supported devices list](https://openrgb.org/devices.html) — around 300
   keyboards are covered, across Logitech, Razer, Corsair, SteelSeries, ASUS,
   MSI, HyperX, Keychron, Ducky, Anne Pro and many white-label boards. If yours
   is there, you are done:
   ```sh
   rgi daemon --backend openrgb --device 0
   ```
   You get every LED, with zone names that usually map to keys.

2. **Windows 11 Dynamic Lighting sees it.** Devices implementing HID
   *LampArray* (USB usage page `0x59`) appear under Settings → Personalization →
   Dynamic Lighting. That is an open standard, so it works without vendor code on
   any OS with a LampArray driver.
   **A `lamparray` backend is designed but not implemented yet** — the protocol is
   written up in [protocols.md](protocols.md#hid-lamparray-usage-page-0x59), but
   `rgi` cannot drive these boards today. If yours is one, that is a
   well-specified place to start.

3. **It is a QMK/VIA board.** QMK speaks a documented raw-HID
   [VIA protocol](https://caniusevia.com/docs/specification/) where per-key
   colours are addressable. Most custom keyboards are QMK. A `via` backend is a
   small, well-specified job — and there is
   [openrgb's qmk driver](https://github.com/CalcProgrammer1/OpenRGB) to read as
   a reference.

4. **A vendor SDK exists and is free.** Some vendors ship one:
   | Vendor | Route | Notes |
   |---|---|---|
   | Logitech G | Logi LED SDK (`LogiLedSetLighting`, per-key via `LogiLedSetLightingForKeyWithKeyName`) | Windows/macOS DLL; on Linux use `g810-led` or libratbag |
   | SteelSeries | GameSense HTTP API on `localhost:27301` | plain JSON, easy to drive, no SDK |
   | Razer | Razer Chroma SDK, or OpenRazer on Linux | SDK is Windows-only; OpenRazer covers Linux |
   | Corsair | iCUE SDK (C++) | heavier; OpenRGB covers most devices |
   | ASUS / MSI / Gigabyte | Aura / Mystic Light / RGB Fusion SDKs | Windows-only, often version-locked; prefer OpenRGB |

5. **Laptop keyboard backlight.** Single colour, no per-key anything. On Linux
   the kernel already exposes it:
   ```sh
   ls /sys/class/leds/*kbd_backlight*
   rgi daemon --backend sysfs
   ```
   One LED means one lane; a panel needs several, so this is best for a
   single "busy anywhere" indicator rather than per-session lamps.

6. **Nothing fits.** Write a backend — see below. This is normal: the board this
   project started from (Sinowealth `258A:0049`, sold under various names with no
   vendor software at all) needed it.

## Writing a backend

One file in `rgi/backends/`, one entry in the registry. The interface is four
methods:

```python
from .base import RGB, Backend, BackendUnavailable, Lamp

class MyKeyboardBackend(Backend):
    name = "mykeyboard"
    min_interval = 0.013          # gap between writes; firmware may need one

    @classmethod
    def available(cls) -> bool:
        return True               # cheap check: device or dependency present

    def open(self) -> None: ...   # claim it, or raise BackendUnavailable
    def close(self) -> None: ...  # release it; safe to call twice
    def lamps(self) -> list[Lamp]: ...
    def write(self, colours: list[RGB]) -> None: ...
```

Four rules, each of which cost someone hours:

1. **`write()` paints everything.** Most controllers have no per-key update at
   all, and several repaint the whole panel on any write. Take the full list and
   build one frame. The daemon only calls you when the rendered result changed.
2. **Label every lamp in `lamps()`,** and set `group="number-row"` on the keys
   you would like sessions to land on. That is how `default_lanes()` picks a
   sensible pool without any configuration.
3. **Set `min_interval` honestly.** Back-to-back writes get dropped or corrupt
   on some firmware; ~13 ms is right for the Sinowealth board.
4. **Never touch firmware or bootloader interfaces.** LED feature reports only.
   Every backend here is safe across a replug, and none can brick a device.

Then add it to `_registry()` in `rgi/backends/__init__.py`, and add a test that
does not need the hardware — for example by checking the exact bytes your
`write()` would send, the way `tests/test_sinowealth.py` and
`tests/test_openrgb.py` do. A backend with byte-level tests is the difference
between "works on my board" and something others can trust.

## Calibrating an unknown board

You do not need the key map to get started; you need it to place *lanes*.

```sh
python -m rgi map --backend mykeyboard
```

That lights one lamp at a time so you can write down what each index is. For a
keyboard, note the lamp indices for the row you want to use, then:

```sh
python -m rgi daemon --backend mykeyboard --lanes 12 13 14 15 16 17 18 19 20 21 22 23
```

If the vendor splits LEDs across two reports (the Sinowealth board does), lamp
indices may be non-contiguous — mapping them once is enough.

## Reverse engineering an unknown protocol

If you have to go this far, this is the path that worked here:

1. **Look at the HID interfaces.** `hid.enumerate()` (or `lsusb -v`,
   `hidrd-convert`) shows interfaces, usage pages and report lengths. Vendor
   lighting usually appears as a vendor-defined page (`0xFF00`) with a large
   feature report. *Take note of which interface you write to*: on the board
   here, one plausible-looking interface accepts writes and silently ignores
   them, which is a classic way to lose a day.
2. **Capture the vendor software.** On Windows, USBPcap + Wireshark; on Linux,
   `usbmon`. Drive the vendor app to set one key, and look at what changed.
   Feature reports carry the whole panel more often than not.
3. **Read existing drivers.** OpenRGB's per-vendor driver files in
   `Controllers/` are the single best reference; if a driver exists there for a
   related chipset, it is usually two constants away from yours.
4. **Probe safely.** Write a mode/unlock command once, then stream frames rather
   than re-committing per frame. Blink a documented "all LEDs" frame first: if
   every key lights, your offsets are right and only the key *order* is unknown.
5. **Document what you learned** in [protocols.md](protocols.md) — including what
   you could not explain. The next person will thank you.

## Device matrix

| Device | Protocol | Backend | Verified |
|---|---|---|---|
| Sinowealth `258A:0049` (white-label "Gaming Keyboard", READSON, many rebrands) | vendor HID feature reports, planar B/G/R blocks | `sinowealth` | yes, on hardware |
| EVision/SONiX `320F:501D` (Magic Refiner MK 17) and siblings | 64-byte command protocol on usage page `0xFF1C` | `evision` | yes, on hardware |
| Other EVision boards (Redragon, Husky, EvoFox, Kreo, VGN/ATK�) | same protocol, v1 or v2 | `evision` (v2) / OpenRGB | protocol family confirmed; per-board maps differ |
| Any OpenRGB-supported keyboard | OpenRGB SDK over TCP | `openrgb` | SDK implemented + unit tested; not yet against a board |
| HID LampArray keyboards | USB HID usage page `0x59` feature reports | `lamparray` (planned) | protocol documented here |
| QMK on Vial firmware (`VIALRGB_ENABLE`) | raw HID `0xFF60`/`0x61`, VialRGB per-LED HSV | `qmk` | protocol to source; needs a board to confirm |
| QMK on stock VIA firmware | same channel, VIA rgb_matrix = one colour | `qmk` (single colour) | protocol to source; needs a board to confirm |
| QMK with the OpenRGB or SignalRGB module | their own raw HID protocols | not driven — use that project's host, or flash Vial | detected and reported only |
| Logitech G LIGHTSYNC | Logi LED SDK | not yet | — |
| SteelSeries | GameSense HTTP | not yet | easiest of the vendor SDKs |
| Laptop backlights | Linux LED class | `sysfs` | implemented, untested here |

## Contributing a backend

Include in the pull request:

- the file, and the registry entry;
- `rgi detect` output for your device;
- which parts you verified on hardware and which you could not;
- a byte-level test if the protocol is fixed-width.

Boards nobody has written down before are the most valuable contribution of all.
