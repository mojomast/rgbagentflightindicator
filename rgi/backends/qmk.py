"""QMK backend - any QMK keyboard, with per-key control where the firmware allows it.

QMK exposes a raw HID channel on usage page ``0xFF60`` / usage ``0x61`` (32-byte
reports, 64 on boards built with the OpenRGB module). What can be done over it
depends entirely on the firmware, so this backend *probes* rather than assuming:

1. **VialRGB** (Vial firmware, ``VIALRGB_ENABLE=yes``) - per-LED HSV, with a real
   LED map. This is the good one: every lane gets its own key. Tested by probing
   ``0x08 0x40`` for the lighting-info reply.
2. **VIA rgb_matrix** (stock QMK + VIA, which is most QMK keyboards) - brightness,
   effect and colour for the *whole board*. No per-LED command exists in stock
   QMK, so this backend reports ``per_lamp = False`` and the daemon shows the most
   urgent lane as one colour instead of pretending.
3. **OpenRGB / SignalRGB modules** - per-LED, but they require custom firmware
   flashed to the board. Detected and reported, deliberately not driven: shipping
   packet builders for firmware nobody in this project can test would be a guess
   dressed as support. Use OpenRGB or SignalRGB itself for those.

Only read-only commands are ever used while detecting, and nothing is written to
EEPROM: no save, no reset, no bootloader jump.
"""

from __future__ import annotations

import colorsys
import time
from typing import Sequence

from .base import RGB, Backend, BackendUnavailable, Lamp

RAW_USAGE_PAGE = 0xFF60
RAW_USAGE = 0x61

# VIA
VIA_PROTOCOL_VERSION = 0x01
VIA_SET = 0x07
VIA_GET = 0x08
VIA_LIGHTING_CHANNEL_RGB_MATRIX = 0x03
VIA_RGB_MATRIX_BRIGHTNESS = 0x01
VIA_RGB_MATRIX_EFFECT = 0x02
VIA_RGB_MATRIX_COLOR = 0x04
RGB_MATRIX_SOLID_COLOR = 1

# Vial
VIAL_PREFIX = 0xFE
VIAL_GET_KEYBOARD_ID = 0x00

# VialRGB (hijacks VIA's lighting commands when compiled in)
VIALRGB_GET_INFO = 0x40
VIALRGB_SET_MODE = 0x41
VIALRGB_GET_SUPPORTED = 0x42
VIALRGB_GET_NUMBER_LEDS = 0x43
VIALRGB_DIRECT_FASTSET = 0x42
VIALRGB_EFFECT_DIRECT = 1
VIALRGB_LEDS_PER_PACKET = 9          # 32 - 2 header - 2 index - 1 count, / 3

# other people's firmware, detected but not driven
SIGNALRGB_GET_PROTOCOL_VERSION = 0x22
OPENRGB_PROTOCOL_REVISIONS = {0x09, 0x0B, 0x0C, 0x0D, 0x0E}

REPORT_LEN_DEFAULT = 32


def rgb_to_hsv_qmk(rgb: RGB) -> tuple[int, int, int]:
    """QMK/Vial HSV: hue 0-255 is 0-360 degrees, saturation and value 0-255."""
    r, g, b = (c / 255.0 for c in rgb)
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    return int(round(h * 255)) & 0xFF, int(round(s * 255)), int(round(v * 255))


class RawHid:
    """The 0xFF60 channel: one outstanding request at a time, leading report id."""

    def __init__(self, path, report_len: int = REPORT_LEN_DEFAULT):
        import hid

        self.report_len = report_len
        self.dev = hid.device()
        self.dev.open_path(path if isinstance(path, bytes) else path.encode())
        self.dev.set_nonblocking(1)

    def close(self) -> None:
        try:
            self.dev.close()
        except Exception:
            pass

    def request(self, payload: Sequence[int], timeout: float = 0.6) -> bytes | None:
        """Write one report and wait for the reply.

        The write carries a leading 0x00 report id (QMK's documented convention);
        a read may or may not include one depending on the platform, so it is
        stripped if present.
        """
        message = bytes([0x00]) + bytes(payload).ljust(self.report_len, b"\x00")
        self.dev.write(message)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                reply = self.dev.read(self.report_len, int((deadline - time.time()) * 1000) or 1)
            except TypeError:                     # older hidapi binding
                reply = self.dev.read(self.report_len)
            if reply:
                data = bytes(reply)
                if len(data) == self.report_len + 1 and data[0] == 0x00:
                    data = data[1:]
                return data
            time.sleep(0.002)
        return None


class QmkBackend(Backend):
    name = "qmk"
    min_interval = 0.002

    def __init__(self, leds: int | None = None, verbose: bool = False, count: int = 12):
        self.forced_leds = leds
        self.verbose = verbose
        self.lane_count = count
        self.transport: RawHid | None = None
        self.mode = "unknown"                  # vialrgb | via | openrgb | signalrgb
        self._lamps: list[Lamp] = []
        self.per_lamp = False
        self.report_len = REPORT_LEN_DEFAULT

    # -- discovery --------------------------------------------------------
    @staticmethod
    def _hid():
        try:
            import hid
        except ImportError as exc:                             # pragma: no cover
            raise BackendUnavailable(
                "hidapi is not installed (pip install hidapi)"
            ) from exc
        return hid

    @classmethod
    def candidates(cls) -> list[dict]:
        hid = cls._hid()
        return [d for d in hid.enumerate()
                if (d.get("usage_page") or 0) == RAW_USAGE_PAGE
                and (d.get("usage") or 0) == RAW_USAGE]

    @classmethod
    def available(cls) -> bool:
        try:
            return bool(cls.candidates())
        except BackendUnavailable:
            return False

    # -- lifecycle --------------------------------------------------------
    def open(self) -> None:
        found = self.candidates()
        if not found:
            raise BackendUnavailable(
                f"no raw HID interface at usage page 0x{RAW_USAGE_PAGE:04X}/"
                f"usage 0x{RAW_USAGE:04X} - is a QMK keyboard on wired USB?"
            )
        chosen = found[0]
        self.report_len = self._report_len(chosen)
        self.transport = RawHid(chosen["path"], self.report_len)

        if self._probe_vialrgb():
            self.mode = "vialrgb"
            self.per_lamp = True
        elif self.report_len == 64 and self._probe_openrgb():
            self.mode = "openrgb"
            raise BackendUnavailable(
                "this board runs the OpenRGB QMK module: use OpenRGB itself "
                "(rgi daemon --backend openrgb) - it speaks that protocol properly"
            )
        elif self._probe_signalrgb():
            self.mode = "signalrgb"
            raise BackendUnavailable(
                "this board runs the SignalRGB QMK module: use SignalRGB itself, "
                "or the openrgb backend - this project does not ship an untested "
                "driver for custom firmware"
            )
        elif self._probe_via_lighting():
            self.mode = "via"
            self.per_lamp = False
        else:
            raise BackendUnavailable(
                "the raw HID channel answered, but neither VialRGB nor VIA lighting "
                "is compiled in - nothing to drive"
            )

        if self.mode == "vialrgb":
            count = self.forced_leds or self._vialrgb_led_count() or 0
            if not count:
                raise BackendUnavailable("VialRGB did not report an LED count")
            self._lamps = [Lamp(index=i, label=f"led{i}", group="unmapped")
                           for i in range(count)]
            self._enter_vialrgb_direct()
        else:
            # one physical colour, exposed as `lane_count` identical lamps so the
            # daemon can still report per-lane state on the API - it just cannot
            # show it, and `per_lamp` tells it to pick the most urgent lane
            n = self.forced_leds or self.lane_count
            self._lamps = [Lamp(index=i, label="whole-board", group="whole-board")
                           for i in range(n)]
            self._ensure_via_hsv()

        if self.verbose:
            print(f"[qmk] {chosen.get('vendor_id'):04X}:{chosen.get('product_id'):04X} "
                  f"report {self.report_len}B  mode {self.mode}  "
                  f"{len(self._lamps)} lamps  per_lamp={self.per_lamp}")

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()
            self.transport = None

    def lamps(self) -> list[Lamp]:
        return list(self._lamps)

    def describe(self) -> dict:
        info = super().describe()
        info["mode"] = self.mode
        return info

    # -- probing (read-only) ----------------------------------------------
    @staticmethod
    def _report_len(device: dict) -> int:
        try:
            import ctypes
            import ctypes.wintypes as wt

            class HIDP_CAPS(ctypes.Structure):
                _fields_ = [("Usage", ctypes.c_ushort), ("UsagePage", ctypes.c_ushort),
                            ("InputReportByteLength", ctypes.c_ushort),
                            ("OutputReportByteLength", ctypes.c_ushort),
                            ("FeatureReportByteLength", ctypes.c_ushort),
                            ("Reserved", ctypes.c_ushort * 17),
                            ("NumberLinkCollectionNodes", ctypes.c_ushort),
                            ("NumberInputButtonCaps", ctypes.c_ushort),
                            ("NumberInputValueCaps", ctypes.c_ushort),
                            ("NumberInputDataIndices", ctypes.c_ushort),
                            ("NumberOutputButtonCaps", ctypes.c_ushort),
                            ("NumberOutputValueCaps", ctypes.c_ushort),
                            ("NumberOutputDataIndices", ctypes.c_ushort),
                            ("NumberFeatureButtonCaps", ctypes.c_ushort),
                            ("NumberFeatureValueCaps", ctypes.c_ushort),
                            ("NumberFeatureDataIndices", ctypes.c_ushort)]

            kernel32 = ctypes.windll.kernel32
            hid_dll = ctypes.WinDLL("hid.dll")
            kernel32.CreateFileW.restype = wt.HANDLE
            path = device["path"]
            text = path.decode("utf-8", "replace") if isinstance(path, bytes) else path
            handle = kernel32.CreateFileW(text, 0, 3, None, 3, 0, None)
            if handle == wt.HANDLE(-1).value or not handle:
                return REPORT_LEN_DEFAULT
            try:
                preparsed = ctypes.c_void_p()
                caps = HIDP_CAPS()
                if hid_dll.HidD_GetPreparsedData(wt.HANDLE(handle), ctypes.byref(preparsed)) and \
                   hid_dll.HidP_GetCaps(preparsed, ctypes.byref(caps)) == 0x00110000:
                    hid_dll.HidD_FreePreparsedData(preparsed)
                    return max(caps.InputReportByteLength, REPORT_LEN_DEFAULT)
            finally:
                kernel32.CloseHandle(wt.HANDLE(handle))
        except Exception:
            pass
        return REPORT_LEN_DEFAULT

    def _probe_vialrgb(self) -> bool:
        reply = self.transport.request([VIA_GET, VIALRGB_GET_INFO])
        if not reply or reply[0] != VIA_GET or reply[1] != VIALRGB_GET_INFO:
            return False
        # supported effects should include direct (1)
        supported = self.transport.request([VIA_GET, VIALRGB_GET_SUPPORTED, 0x00, 0x00])
        if supported and supported[0] == VIA_GET:
            ids = [supported[i] | (supported[i + 1] << 8)
                   for i in range(2, len(supported) - 1, 2)
                   if supported[i] != 0xFF]
            return VIALRGB_EFFECT_DIRECT in ids or not ids
        return True

    def _vialrgb_led_count(self) -> int:
        reply = self.transport.request([VIA_GET, VIALRGB_GET_NUMBER_LEDS])
        if reply and reply[0] == VIA_GET and reply[1] == VIALRGB_GET_NUMBER_LEDS:
            return reply[2] | (reply[3] << 8)
        return 0

    def _probe_openrgb(self) -> bool:
        reply = self.transport.request([0x01])       # GET_PROTOCOL_VERSION
        return bool(reply and reply[0] == 0x01 and reply[1] in OPENRGB_PROTOCOL_REVISIONS)

    def _probe_signalrgb(self) -> bool:
        reply = self.transport.request([SIGNALRGB_GET_PROTOCOL_VERSION])
        return bool(reply and reply[0] == SIGNALRGB_GET_PROTOCOL_VERSION and reply[1] == 0x01)

    def _probe_via_lighting(self) -> bool:
        reply = self.transport.request(
            [VIA_GET, VIA_LIGHTING_CHANNEL_RGB_MATRIX, VIA_RGB_MATRIX_COLOR])
        return bool(reply and reply[0] != 0xFF)

    # -- painting ---------------------------------------------------------
    def _enter_vialrgb_direct(self) -> None:
        """Select the direct effect; the colours then live entirely with us."""
        self.transport.request([VIA_SET, VIALRGB_SET_MODE, VIALRGB_EFFECT_DIRECT, 0x00,
                                0x00, 0x00, 0x00, 0x00])

    def _ensure_via_hsv(self) -> None:
        """Solid colour effect, so whole-board colours actually show."""
        self.transport.request([VIA_SET, VIA_LIGHTING_CHANNEL_RGB_MATRIX,
                                VIA_RGB_MATRIX_EFFECT, RGB_MATRIX_SOLID_COLOR])

    def write(self, colours: Sequence[RGB]) -> None:
        if self.transport is None:
            raise BackendUnavailable("backend is not open")
        if self.mode == "vialrgb":
            self._write_vialrgb(colours)
        else:
            self._write_whole_board(colours[0] if colours else (0, 0, 0))

    def _write_vialrgb(self, colours: Sequence[RGB]) -> None:
        leds = list(colours[:len(self._lamps)])
        for start in range(0, len(leds), VIALRGB_LEDS_PER_PACKET):
            chunk = leds[start:start + VIALRGB_LEDS_PER_PACKET]
            payload = [VIA_SET, VIALRGB_DIRECT_FASTSET,
                       start & 0xFF, (start >> 8) & 0xFF, len(chunk)]
            for rgb in chunk:
                payload.extend(rgb_to_hsv_qmk(rgb))
            self.transport.request(payload)

    def _write_whole_board(self, rgb: RGB) -> None:
        h, s, v = rgb_to_hsv_qmk(rgb)
        if rgb == (0, 0, 0):
            self.transport.request([VIA_SET, VIA_LIGHTING_CHANNEL_RGB_MATRIX,
                                    VIA_RGB_MATRIX_BRIGHTNESS, 0])
            return
        # effect first (enables the matrix), then brightness, then colour
        self.transport.request([VIA_SET, VIA_LIGHTING_CHANNEL_RGB_MATRIX,
                                VIA_RGB_MATRIX_EFFECT, RGB_MATRIX_SOLID_COLOR])
        self.transport.request([VIA_SET, VIA_LIGHTING_CHANNEL_RGB_MATRIX,
                                VIA_RGB_MATRIX_BRIGHTNESS, max(v, 1)])
        self.transport.request([VIA_SET, VIA_LIGHTING_CHANNEL_RGB_MATRIX,
                                VIA_RGB_MATRIX_COLOR, h, s])
