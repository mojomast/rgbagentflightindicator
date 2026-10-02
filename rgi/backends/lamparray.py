"""HID LampArray backend - the vendor-neutral RGB keyboard standard.

A HID LampArray device exposes its LEDs as ``LampArray`` on USB usage page
``0x59`` (Lighting and Illumination), the open standard behind Windows 11
Dynamic Lighting, defined by USB-IF HUTRR84. Because the standard carries each
lamp's ``InputBinding`` - the Keyboard/Keypad usage id of the key it sits under -
the number row can be found with no calibration at all.

The device speaks *feature reports*, not a framebuffer:

    attributes  read the lamp count, kind and MinUpdateInterval
    request     ask about one lamp id
    response    position, purposes, channel level counts, InputBinding
    update      LampMultiUpdateReport / LampRangeUpdateReport with colours
    control     take the array out of autonomous mode (or hand it back)

Everything below the HID boundary is a pure function whose wire layout is
spelled out in the constants and documented here. **Report ids and report
lengths are assigned per device by its HID report descriptor**, so the
functions that build/parse reports state the layout they assume:

* report ids are read from the device's report descriptor when hidapi can
  return one (``get_report_descriptor``); the fallback values are the
  reference ids from HUTRR84's own example descriptor, which Microsoft's
  ArduinoHidForWindows and OpenRGB also use. A ``report_ids`` constructor
  argument overrides both.
* the field order inside each report is the HUTRR84-defined order used by the
  reference descriptor (little-endian, HID's USB convention).
* ``InputBinding`` is assumed to be the 8-bit value the reference descriptors
  declare, interpreted as a Keyboard/Keypad page (0x07) usage id. HUTRR84's
  prose describes it as a 16-bit usage; a device that declares 16 bits will
  not parse here.

Safety: LED feature reports only - no firmware, DFU or bootloader traffic.
``open()`` takes the array out of autonomous mode; ``close()`` hands it back.
Tested against constructed reports and a fake transport, not on hardware.
"""

from __future__ import annotations

import struct
from typing import Sequence

from .base import RGB, Backend, BackendUnavailable, Lamp

USAGE_PAGE = 0x59
USAGE = 0x01

#: HUTRR84 report usages (the value inside the report descriptor).
USAGE_ATTRIBUTES_REPORT = 0x02
USAGE_ATTRIBUTES_REQUEST_REPORT = 0x20
USAGE_ATTRIBUTES_RESPONSE_REPORT = 0x22
USAGE_MULTI_UPDATE_REPORT = 0x50
USAGE_RANGE_UPDATE_REPORT = 0x60
USAGE_CONTROL_REPORT = 0x70

#: Reference report ids from HUTRR84's example descriptor. Real devices choose
#: their own in the report descriptor; ``open()`` prefers what the device
#: declares and only falls back to these.
DEFAULT_REPORT_IDS: dict[int, int] = {
    USAGE_ATTRIBUTES_REPORT: 0x01,
    USAGE_ATTRIBUTES_REQUEST_REPORT: 0x02,
    USAGE_ATTRIBUTES_RESPONSE_REPORT: 0x03,
    USAGE_MULTI_UPDATE_REPORT: 0x04,
    USAGE_RANGE_UPDATE_REPORT: 0x05,
    USAGE_CONTROL_REPORT: 0x06,
}

#: Payload sizes, excluding the leading report id byte (reference layout).
ATTRIBUTES_REPORT_BYTES = 22
LAMP_ATTRIBUTES_REPORT_BYTES = 28
MULTI_UPDATE_REPORT_BYTES = 50
RANGE_UPDATE_REPORT_BYTES = 9
CONTROL_REPORT_BYTES = 1
LAMPS_PER_MULTI_UPDATE = 8

#: LampUpdateFlags bit 0: this update completes the frame (HUTRR84 3.5.4).
UPDATE_COMPLETE = 0x01

AUTONOMOUS = 1
DIRECT = 0

#: RGBI is four channels in this order; intensity is always full because the
#: daemon encodes brightness in the RGB values themselves.
CHANNELS = 4
FULL_INTENSITY = 0xFF

#: read generously; get_feature_report returns the device's real length
MAX_FEATURE_BYTES = 256

KIND_NAMES = {
    0x00: "undefined",
    0x01: "keyboard",
    0x02: "mouse",
    0x03: "game-controller",
    0x04: "peripheral",
    0x05: "scene",
    0x06: "notification",
    0x07: "chassis",
    0x08: "wearable",
    0x09: "furniture",
    0x0A: "art",
}
KIND_KEYBOARD = 0x01

#: LampPurposes flags (HUTRR84 3.6.2).
LAMP_PURPOSES = {
    0x01: "control",
    0x02: "accent",
    0x04: "branding",
    0x08: "status",
    0x10: "illumination",
    0x20: "presentation",
}

#: Keyboard/Keypad page (0x07) usages rgi can name. The number row is the
#: default lane pool, so it is labelled exactly as rgi's other backends do.
HID_KEYBOARD_USAGE_PAGE = 0x07


def _keyboard_usages() -> dict[int, str]:
    table = {0x04 + i: ch for i, ch in enumerate("abcdefghijklmnopqrstuvwxyz")}
    table.update({0x1E + i: str((i + 1) % 10) for i in range(10)})   # 1..9,0
    table.update({0x3A + i: f"F{i + 1}" for i in range(12)})
    table.update({0x59 + i: f"kp{i + 1}" for i in range(9)})
    table.update({
        0x28: "enter", 0x29: "esc", 0x2A: "backspace", 0x2B: "tab",
        0x2C: "space", 0x2D: "-", 0x2E: "=", 0x2F: "[", 0x30: "]",
        0x31: "\\", 0x32: "#", 0x33: ";", 0x34: "'", 0x35: "`",
        0x36: ",", 0x37: ".", 0x38: "/", 0x39: "capslock",
        0x46: "printscreen", 0x47: "scrolllock", 0x48: "pause",
        0x49: "insert", 0x4A: "home", 0x4B: "pageup",
        0x4C: "delete", 0x4D: "end", 0x4E: "pagedown",
        0x4F: "right", 0x50: "left", 0x51: "down", 0x52: "up",
        0x53: "numlock", 0x54: "kp/", 0x55: "kp*", 0x56: "kp-",
        0x57: "kp+", 0x58: "kpenter", 0x62: "kp0", 0x63: "kp.",
        0x64: "\\", 0x65: "menu", 0x66: "kp=", 0x67: "F13", 0x68: "F14",
        0x69: "F15", 0x6A: "F16", 0x6B: "F17", 0x6C: "F18", 0x6D: "F19",
        0x6E: "F20", 0x6F: "F21", 0x70: "F22", 0x71: "F23", 0x72: "F24",
        0x7F: "mute", 0x80: "volumeup", 0x81: "volumedown",
        0xE0: "leftctrl", 0xE1: "leftshift", 0xE2: "leftalt", 0xE3: "leftgui",
        0xE4: "rightctrl", 0xE5: "rightshift", 0xE6: "rightalt",
        0xE7: "rightgui",
    })
    return table


HID_KEYBOARD_USAGES = _keyboard_usages()

#: `` ` 1 2 3 4 5 6 7 8 9 0 - = `` - the row rgi puts lanes on first.
NUMBER_ROW_USAGES = frozenset({0x35, *range(0x1E, 0x28), 0x2D, 0x2E})


# -- pure parsers -----------------------------------------------------------
def _le(data: bytes, offset: int, size: int) -> int:
    return int.from_bytes(data[offset:offset + size], "little")


def parse_attributes_report(payload: bytes) -> dict:
    """LampArrayAttributesReport payload -> the fields rgi needs.

    Reference layout (22 bytes, report id not included)::

        0   u16 lamp_count
        2   u32 bounding_box_width_um
        6   u32 bounding_box_height_um
        10  u32 bounding_box_depth_um
        14  u32 kind
        18  u32 min_update_interval_us
    """
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("LampArrayAttributesReport payload must be bytes")
    data = bytes(payload)
    if len(data) != ATTRIBUTES_REPORT_BYTES:
        raise ValueError(
            f"LampArrayAttributesReport payload must be "
            f"{ATTRIBUTES_REPORT_BYTES} bytes, got {len(data)}"
        )
    kind = _le(data, 14, 4)
    min_us = _le(data, 18, 4)
    return {
        "lamp_count": _le(data, 0, 2),
        "bounding_box_width_um": _le(data, 2, 4),
        "bounding_box_height_um": _le(data, 6, 4),
        "bounding_box_depth_um": _le(data, 10, 4),
        "kind": kind,
        "kind_name": KIND_NAMES.get(kind, f"kind{kind}"),
        "min_update_interval_us": min_us,
        "min_update_interval_s": min_us / 1_000_000.0,
    }


def parse_lamp_attributes_report(payload: bytes) -> dict:
    """LampAttributesResponseReport payload -> one lamp's attributes.

    Reference layout (28 bytes, report id not included)::

        0   u16 lamp_id
        2   u32 position_x_um
        6   u32 position_y_um
        10  u32 position_z_um
        14  u32 update_latency_us
        18  u32 purposes
        22  u8  red_level_count
        23  u8  green_level_count
        24  u8  blue_level_count
        25  u8  intensity_level_count
        26  u8  is_programmable
        27  u8  input_binding   (Keyboard/Keypad usage id, reference layout)
    """
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("LampAttributesResponseReport payload must be bytes")
    data = bytes(payload)
    if len(data) != LAMP_ATTRIBUTES_REPORT_BYTES:
        raise ValueError(
            f"LampAttributesResponseReport payload must be "
            f"{LAMP_ATTRIBUTES_REPORT_BYTES} bytes, got {len(data)}"
        )
    purposes = _le(data, 18, 4)
    binding = data[27]
    return {
        "lamp_id": _le(data, 0, 2),
        "position_um": (_le(data, 2, 4), _le(data, 6, 4), _le(data, 10, 4)),
        "position_x_um": _le(data, 2, 4),
        "position_y_um": _le(data, 6, 4),
        "position_z_um": _le(data, 10, 4),
        "update_latency_us": _le(data, 14, 4),
        "purposes": purposes,
        "purpose_names": tuple(name for bit, name in LAMP_PURPOSES.items()
                               if purposes & bit),
        "red_level_count": data[22],
        "green_level_count": data[23],
        "blue_level_count": data[24],
        "intensity_level_count": data[25],
        "level_counts": (data[22], data[23], data[24], data[25]),
        "is_programmable": bool(data[26]),
        "input_binding": binding,
        "input_binding_usage_page": HID_KEYBOARD_USAGE_PAGE if binding else None,
        "input_binding_usage_id": binding or None,
    }


def _clean_rgb(colour) -> tuple[int, int, int]:
    out = []
    for channel in tuple(colour)[:3]:
        try:
            value = int(channel)
        except (TypeError, ValueError):
            value = 0
        out.append(max(0, min(255, value)))
    while len(out) < 3:
        out.append(0)
    return out[0], out[1], out[2]


# -- pure builders (return the full report, report id first) ----------------
def build_attributes_request_report(lamp_id: int, *, report_id: int) -> bytes:
    """LampAttributesRequestReport: ask the device about one lamp id."""
    return bytes([report_id & 0xFF]) + struct.pack("<H", int(lamp_id) & 0xFFFF)


def build_multi_update_report(entries, *, report_id: int,
                              update_complete: bool = False) -> bytes:
    """LampMultiUpdateReport for up to eight ``(lamp_id, rgb)`` entries.

    Reference layout (50-byte payload, report id first)::

        u8  lamp_count
        u8  lamp_update_flags
        8 x u16 lamp_ids
        8 x u8  red, green, blue, intensity
    """
    entries = list(entries)
    if not entries:
        raise ValueError("LampMultiUpdateReport needs at least one entry")
    if len(entries) > LAMPS_PER_MULTI_UPDATE:
        raise ValueError(
            f"LampMultiUpdateReport carries at most {LAMPS_PER_MULTI_UPDATE} "
            f"lamps, got {len(entries)}"
        )
    lamp_ids = []
    colours = bytearray()
    for lamp_id, colour in entries:
        red, green, blue = _clean_rgb(colour)
        lamp_ids.append(int(lamp_id) & 0xFFFF)
        colours += bytes([red, green, blue, FULL_INTENSITY])
    while len(lamp_ids) < LAMPS_PER_MULTI_UPDATE:
        lamp_ids.append(0)
        colours += bytes(CHANNELS)
    flags = UPDATE_COMPLETE if update_complete else 0
    payload = (bytes([len(entries), flags])
               + struct.pack(f"<{LAMPS_PER_MULTI_UPDATE}H", *lamp_ids)
               + bytes(colours))
    return bytes([report_id & 0xFF]) + payload


def build_range_update_report(lamp_id_start: int, lamp_id_end: int, colour,
                              *, report_id: int,
                              update_complete: bool = False) -> bytes:
    """LampRangeUpdateReport: one colour for ids ``start..end`` inclusive.

    Reference layout (9-byte payload, report id first)::

        u8  lamp_update_flags
        u16 lamp_id_start
        u16 lamp_id_end
        u8  red, green, blue, intensity
    """
    start, end = int(lamp_id_start), int(lamp_id_end)
    if not 0 <= start <= end <= 0xFFFF:
        raise ValueError(f"bad LampRangeUpdate range {start}..{end}")
    red, green, blue = _clean_rgb(colour)
    flags = UPDATE_COMPLETE if update_complete else 0
    payload = (bytes([flags])
               + struct.pack("<HH", start, end)
               + bytes([red, green, blue, FULL_INTENSITY]))
    return bytes([report_id & 0xFF]) + payload


def build_control_report(report_id: int, autonomous: bool) -> bytes:
    """LampArrayControlReport: 1 leaves autonomous mode, 0 takes control."""
    return bytes([report_id & 0xFF, 1 if autonomous else 0])


def parse_report_ids(descriptor: bytes) -> dict[int, int]:
    """Map HUTRR84 report usages to report ids from a HID report descriptor.

    Only the items needed for the mapping are walked: Usage Page / Usage /
    Report ID, and the Report ID a LampArray collection declares itself under.
    Unrecognised items are skipped by their size, so a descriptor that uses
    other report types or vendors' fields still parses.
    """
    if not isinstance(descriptor, (bytes, bytearray, memoryview)):
        raise TypeError("HID report descriptor must be bytes")
    data = bytes(descriptor)
    out: dict[int, int] = {}
    usage_page = 0
    usage = 0
    report_id = 0
    pos = 0
    while pos < len(data):
        prefix = data[pos]
        if prefix == 0xFE:                                  # long item
            if pos + 2 >= len(data):
                break
            size = data[pos + 1]
            pos += 3 + size
            continue
        size = (0, 1, 2, 4)[prefix & 0x03]
        item_type = (prefix >> 2) & 0x03
        tag = prefix >> 4
        value_bytes = data[pos + 1:pos + 1 + size]
        if len(value_bytes) < size:
            break
        value = int.from_bytes(value_bytes, "little")
        pos += 1 + size

        if item_type == 1:                                   # global
            if tag == 0x0:
                usage_page = value
            elif tag == 0x8:
                report_id = value
        elif item_type == 2:                                 # local
            if tag == 0x0:
                if size == 4:
                    usage_page = (value >> 16) & 0xFFFF
                    usage = value & 0xFFFF
                else:
                    usage = value
        elif item_type == 0:                                 # main
            if tag in (0xA, 0xB) and usage_page == USAGE_PAGE and report_id:
                out.setdefault(usage, report_id)
            usage = 0                                        # locals clear
    return out


# -- transport --------------------------------------------------------------
class HidTransport:
    """The only part that touches hidapi: one feature-report channel."""

    def __init__(self, path):
        import hid
        self._dev = hid.device()
        self._dev.open_path(path if isinstance(path, bytes) else str(path).encode())
        self._closed = False

    def get_feature(self, report_id: int, length: int = MAX_FEATURE_BYTES) -> bytes:
        """Read one feature report; the leading report id is stripped."""
        data = bytes(self._dev.get_feature_report(report_id, length) or ())
        if data and data[0] == report_id:
            data = data[1:]
        return data

    def send_feature(self, report: bytes) -> None:
        self._dev.send_feature_report(bytes(report))

    def report_descriptor(self) -> bytes:
        return bytes(self._dev.get_report_descriptor() or ())

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._dev.close()
            except Exception:
                pass


# -- the backend ------------------------------------------------------------
class LamparrayBackend(Backend):
    name = "lamparray"
    per_lamp = True
    #: replaced in open() by the device's own MinUpdateInterval
    min_interval = 0.0

    def __init__(self, count: int | None = None, path=None, report_ids=None,
                 verbose: bool = False, transport_factory=None):
        self.forced_count = count
        self.forced_path = path
        self.forced_report_ids = dict(report_ids or {})
        self.verbose = verbose
        self._transport_factory = transport_factory
        self.transport = None
        self.count = 0
        self.attributes: dict = {}
        self.report_ids = dict(DEFAULT_REPORT_IDS)
        self._declared_usages: set[int] | None = None
        self._lamps: list[Lamp] = []
        self._open = False

    # -- discovery ---------------------------------------------------------
    @staticmethod
    def _hid():
        try:
            import hid
        except ImportError as exc:                            # pragma: no cover
            raise BackendUnavailable(
                "hidapi is not installed (pip install hidapi)"
            ) from exc
        return hid

    @classmethod
    def candidates(cls) -> list[dict]:
        hid = cls._hid()
        return [d for d in hid.enumerate()
                if (d.get("usage_page") or 0) == USAGE_PAGE
                and (d.get("usage") or 0) == USAGE]

    @classmethod
    def available(cls) -> bool:
        try:
            return bool(cls.candidates())
        except BackendUnavailable:
            return False

    # -- lifecycle ---------------------------------------------------------
    def _make_transport(self, path):
        if self._transport_factory is not None:
            return self._transport_factory(path)
        return HidTransport(path)

    def _resolve_report_ids(self) -> dict[int, int]:
        ids = dict(DEFAULT_REPORT_IDS)
        descriptor = None
        getter = getattr(self.transport, "report_descriptor", None)
        if callable(getter):
            try:
                descriptor = getter()
            except Exception:
                descriptor = None
        if descriptor:
            found = parse_report_ids(descriptor)
            if found:
                self._declared_usages = set(found)
                for usage, default in DEFAULT_REPORT_IDS.items():
                    if found.get(usage):
                        ids[usage] = found[usage]
        ids.update(self.forced_report_ids)
        return ids

    def _feature(self, usage: int, expected: int) -> bytes:
        report_id = self.report_ids[usage]
        try:
            data = self.transport.get_feature(report_id)
        except Exception as exc:
            raise BackendUnavailable(
                f"LampArray feature report 0x{report_id:02X} failed: {exc}"
            ) from exc
        if not data:
            raise BackendUnavailable(
                f"the device did not answer LampArray report 0x{report_id:02X} "
                f"(usage 0x{usage:02X}) - a device that does not use this "
                "report id will need the descriptor parsed (see docs/lamparray.md)"
            )
        if len(data) < expected:
            raise BackendUnavailable(
                f"LampArray report 0x{report_id:02X} is {len(data)} bytes, "
                f"expected at least {expected}"
            )
        return data[:expected]

    def _send(self, report: bytes) -> None:
        try:
            self.transport.send_feature(report)
        except Exception as exc:
            raise BackendUnavailable(f"LampArray feature write failed: {exc}") from exc

    def open(self) -> None:
        self.close()
        found = self.candidates()
        if self.forced_path is not None:
            chosen = {"path": self.forced_path}
            for candidate in found:
                if candidate.get("path") == self.forced_path:
                    chosen = candidate
                    break
        elif found:
            chosen = found[0]
        else:
            raise BackendUnavailable(
                "no HID interface on usage page 0x0059 / usage 0x0001 - is a "
                "LampArray keyboard on wired USB?"
            )

        try:
            self.transport = self._make_transport(chosen["path"])
        except Exception as exc:
            raise BackendUnavailable(
                f"could not open LampArray device {chosen.get('path')!r}: {exc}"
            ) from exc

        try:
            self.report_ids = self._resolve_report_ids()
            try:
                self.attributes = parse_attributes_report(
                    self._feature(USAGE_ATTRIBUTES_REPORT, ATTRIBUTES_REPORT_BYTES)
                )
            except ValueError as exc:
                raise BackendUnavailable(
                    f"the device's LampArrayAttributesReport does not match the "
                    f"HUTRR84 reference layout: {exc}"
                ) from exc
            count = self.forced_count or self.attributes["lamp_count"]
            if count <= 0:
                raise BackendUnavailable(
                    "the device answered LampArrayAttributesReport with 0 lamps"
                )

            self._lamps = []
            for lamp_id in range(count):
                self._send(build_attributes_request_report(
                    lamp_id, report_id=self.report_ids[USAGE_ATTRIBUTES_REQUEST_REPORT]))
                try:
                    attrs = parse_lamp_attributes_report(
                        self._feature(USAGE_ATTRIBUTES_RESPONSE_REPORT,
                                      LAMP_ATTRIBUTES_REPORT_BYTES))
                except ValueError as exc:
                    raise BackendUnavailable(
                        f"lamp {lamp_id}: LampAttributesResponseReport does not "
                        f"match the HUTRR84 reference layout: {exc}"
                    ) from exc
                label, group = self._lamp_label(attrs, lamp_id)
                self._lamps.append(Lamp(
                    index=lamp_id, label=label, group=group,
                    x=attrs["position_x_um"] / 1000.0,
                    y=attrs["position_y_um"] / 1000.0,
                ))
            self.count = len(self._lamps)

            # leave autonomous mode: until this is sent the firmware effect
            # owns the array and update reports may be ignored.
            self._send(build_control_report(
                self.report_ids[USAGE_CONTROL_REPORT], DIRECT))
        except Exception:
            transport, self.transport = self.transport, None
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass
            raise

        self.min_interval = max(0.0, float(self.attributes.get("min_update_interval_s") or 0.0))
        self._open = True
        if self.verbose:
            print(f"[lamparray] {len(self._lamps)} lamps, kind "
                  f"{self.attributes.get('kind_name')}, "
                  f"min interval {self.min_interval * 1000:.1f} ms, "
                  f"reports {self.report_ids}")

    def _lamp_label(self, attrs: dict, lamp_id: int) -> tuple[str, str]:
        """Key name from InputBinding when the array declares itself a keyboard."""
        binding = attrs.get("input_binding") or 0
        if self.attributes.get("kind") == KIND_KEYBOARD and binding:
            label = HID_KEYBOARD_USAGES.get(binding)
            if label:
                group = "number-row" if binding in NUMBER_ROW_USAGES else "key"
                return label, group
        return f"lamp{lamp_id}", "key"

    def close(self) -> None:
        self._open = False
        transport, self.transport = self.transport, None
        if transport is None:
            return
        if self.count:
            try:
                transport.send_feature(build_control_report(
                    self.report_ids[USAGE_CONTROL_REPORT], AUTONOMOUS))
            except Exception:
                pass
        try:
            transport.close()
        except Exception:
            pass

    def lamps(self) -> list[Lamp]:
        return list(self._lamps)

    # -- painting ----------------------------------------------------------
    def _use_range_report(self) -> bool:
        """A range report is only sent when the descriptor declared one."""
        return (self._declared_usages is None
                or USAGE_RANGE_UPDATE_REPORT in self._declared_usages)

    def write(self, colours: Sequence[RGB]) -> None:
        if not self._open or self.transport is None:
            raise BackendUnavailable("lamparray backend is not open")
        frame = list(colours)
        if len(frame) != len(self._lamps):
            return                                  # wrong shape: drop, do not paint
        entries = [(lamp.index, _clean_rgb(colour))
                   for lamp, colour in zip(self._lamps, frame)]
        if not entries:
            return

        if all(colour == entries[0][1] for _, colour in entries) and self._use_range_report():
            self._send(build_range_update_report(
                entries[0][0], entries[-1][0], entries[0][1],
                report_id=self.report_ids[USAGE_RANGE_UPDATE_REPORT],
                update_complete=True,
            ))
            return

        for start in range(0, len(entries), LAMPS_PER_MULTI_UPDATE):
            chunk = entries[start:start + LAMPS_PER_MULTI_UPDATE]
            last = start + LAMPS_PER_MULTI_UPDATE >= len(entries)
            self._send(build_multi_update_report(
                chunk,
                report_id=self.report_ids[USAGE_MULTI_UPDATE_REPORT],
                update_complete=last,
            ))


#: Compatibility spelling: the protocol write-ups and older registry entries
#: call it ``LampArrayBackend``. Both names are the one class.
LampArrayBackend = LamparrayBackend
