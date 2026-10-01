"""OpenRGB backend - one implementation, hundreds of keyboards.

OpenRGB (https://openrgb.org) already speaks the vendor protocol of a huge range
of boards. Rather than reimplementing each vendor's HID dialect, we talk to its
SDK server over TCP, which is a small, documented binary protocol:

    header  = "ORGB" + u32 device + u32 packet id + u32 body length  (little endian)
    port    = 6742 by default

Enable it in OpenRGB under Settings -> SDK Server -> "Start Server", then:

    rgi detect --backend openrgb          # what it sees
    rgi daemon --backend openrgb

The controller-data parse follows the layout documented in OpenRGB's
Documentation/OpenRGBSDK.md and is gated on the protocol version the server
reports. Where the documented layout has variants (the mode colour list, and the
matrix map whose on-the-wire form is easy to misread), the parser tries each
variant and keeps the one that consumes the payload *exactly* - a wrong guess
almost never lands byte-perfect. If your server still defeats it, run with
--debug to dump the raw block and pass --leds N to skip parsing altogether.
"""

from __future__ import annotations

import socket
import struct
from typing import Sequence

from .base import RGB, Backend, BackendUnavailable, Lamp

MAGIC = b"ORGB"
DEFAULT_PORT = 6742

# packet ids we use
REQUEST_CONTROLLER_COUNT = 0
REQUEST_CONTROLLER_DATA = 1
REQUEST_PROTOCOL_VERSION = 40
SET_CLIENT_NAME = 50
UPDATELEDS = 1050
SETCUSTOMMODE = 1100

# device types that mean "keyboard" (RGBController::device_type)
DEVICE_TYPE_KEYBOARD = 1


class ProtocolError(RuntimeError):
    pass


class Reader:
    """Little-endian cursor over a controller-data block."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def left(self) -> int:
        return len(self.data) - self.pos

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise ProtocolError(f"wanted {n} bytes at {self.pos}, only {self.left()} left")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def u16(self) -> int:
        return struct.unpack("<H", self.take(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.take(4))[0]

    def bstring(self) -> str:
        n = self.u16()
        if n == 0:
            return ""
        raw = self.take(n)
        return raw.split(b"\0", 1)[0].decode("utf-8", "replace")


def _skip_matrix(r: Reader) -> None:
    """Skip a matrix map: u32 height, u32 width, then one u32 per cell."""
    height = r.u32()
    width = r.u32()
    r.take(4 * height * width)


def _parse_modes(r: Reader, proto: int, count: int) -> list[dict]:
    modes = []
    for _ in range(count):
        mode = {"name": r.bstring()}
        if proto < 6:
            mode["value"] = r.i32()
        mode["flags"] = r.u32()
        mode["speed_min"] = r.u32()
        mode["speed_max"] = r.u32()
        if proto >= 3:
            mode["brightness_min"] = r.u32()
            mode["brightness_max"] = r.u32()
        mode["colors_min"] = r.u32()
        mode["colors_max"] = r.u32()
        mode["speed"] = r.u32()
        if proto >= 3:
            mode["brightness"] = r.u32()
        mode["direction"] = r.u32()
        mode["color_mode"] = r.u32()
        n = r.u16()
        mode["colors"] = [tuple(r.take(4)[:3]) for _ in range(n)]
        modes.append(mode)
    return modes


def _parse_zones(r: Reader, proto: int, count: int, matrix: str) -> list[dict]:
    zones = []
    for _ in range(count):
        zone = {"name": r.bstring()}
        zone["type"] = r.i32()
        zone["leds_min"] = r.u32()
        zone["leds_max"] = r.u32()
        zone["leds_count"] = r.u32()
        matrix_len = r.u16()
        if matrix_len:
            if matrix == "structured":
                _skip_matrix(r)
            else:
                r.take(matrix_len)
        zone["segments"] = []
        if proto >= 4:
            for _ in range(r.u16()):
                seg = {"name": r.bstring()}
                seg["type"] = r.i32()
                seg["start"] = r.u32()
                seg["leds_count"] = r.u32()
                seg_matrix_len = r.u16()
                if seg_matrix_len:
                    if matrix == "structured":
                        _skip_matrix(r)
                    else:
                        r.take(seg_matrix_len)
                if proto >= 6:
                    seg["flags"] = r.u32()
                zone["segments"].append(seg)
        zones.append(zone)
    return zones


def parse_controller_data(payload: bytes, proto: int) -> dict:
    """Decode a REQUEST_CONTROLLER_DATA response.

    Tries the documented layout variants and returns the first parse that
    consumes the block exactly. Raises ProtocolError with a readable reason if
    none do - that is the signal to use --debug / --leds.
    """
    attempts = []
    for matrix in ("structured", "raw"):
        for display_names in (True, False):
            r = Reader(payload)
            try:
                wanted = r.u32()                      # data_size, duplicated
                device = {"type": r.i32(), "name": r.bstring()}
                if proto >= 1:
                    device["vendor"] = r.bstring()
                device["description"] = r.bstring()
                device["version"] = r.bstring()
                device["serial"] = r.bstring()
                device["location"] = r.bstring()

                mode_count = r.u16()
                active = r.u32()
                device["modes"] = _parse_modes(r, proto, mode_count)
                device["active_mode"] = active

                zone_count = r.u16()
                device["zones"] = _parse_zones(r, proto, zone_count, matrix)

                led_count = r.u16()
                device["leds"] = [{"name": r.bstring()} for _ in range(led_count)]

                if display_names and r.left() >= 2:
                    try:
                        names = r.u16()
                        for _ in range(names):
                            r.bstring()
                    except ProtocolError:
                        pass

                if r.left() != 0:
                    raise ProtocolError(f"{r.left()} bytes left over")
                device["_parse"] = {"proto": proto, "matrix": matrix,
                                    "display_names": display_names,
                                    "data_size": wanted}
                return device
            except ProtocolError as exc:
                attempts.append(f"matrix={matrix} display_names={display_names}: {exc}")

    raise ProtocolError(
        "could not decode controller data (" + "; ".join(attempts) + "). "
        "Run with --debug to dump it, or pass --leds N to skip parsing."
    )


class OpenRGBClient:
    """Minimal blocking client for the OpenRGB SDK server."""

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT, timeout: float = 3.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.protocol = 0

    # -- plumbing ---------------------------------------------------------
    def connect(self, client_name: str = "rgbagentflightindicator") -> None:
        try:
            self.sock = socket.create_connection((self.host, self.port), self.timeout)
        except OSError as exc:
            raise BackendUnavailable(
                f"OpenRGB SDK server not reachable on {self.host}:{self.port} ({exc}). "
                "Start OpenRGB and enable Settings -> SDK Server -> Start Server."
            ) from exc
        self.sock.settimeout(self.timeout)
        try:
            self.protocol = self.request_protocol_version()
        except ProtocolError:
            self.protocol = 0
        self.set_client_name(client_name)

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def send(self, packet_id: int, body: bytes = b"", device: int = 0) -> None:
        if self.sock is None:
            raise BackendUnavailable("not connected")
        header = MAGIC + struct.pack("<III", device, packet_id, len(body))
        self.sock.sendall(header + body)

    def recv_exact(self, n: int) -> bytes:
        if self.sock is None:
            raise BackendUnavailable("not connected")
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ProtocolError("connection closed by the OpenRGB server")
            buf += chunk
        return buf

    def request(self, packet_id: int, body: bytes = b"", device: int = 0) -> bytes:
        """Send a request and return the response body."""
        self.send(packet_id, body, device)
        magic, dev, reply_id, size = struct.unpack("<4sIII", self.recv_exact(16))
        if magic != MAGIC:
            raise ProtocolError(f"bad magic in reply: {magic!r}")
        return self.recv_exact(size)

    # -- commands ---------------------------------------------------------
    def request_protocol_version(self) -> int:
        body = self.request(REQUEST_PROTOCOL_VERSION)
        return struct.unpack("<I", body[:4])[0]

    def set_client_name(self, name: str) -> None:
        # a bare NUL-terminated string, not a length-prefixed one
        self.send(SET_CLIENT_NAME, name.encode() + b"\0")

    def controller_count(self) -> int:
        body = self.request(REQUEST_CONTROLLER_COUNT)
        return struct.unpack("<I", body[:4])[0]

    def controller_data(self, index: int) -> dict:
        body = b""
        if self.protocol >= 1:
            body = struct.pack("<I", min(self.protocol, 6))
        raw = self.request(REQUEST_CONTROLLER_DATA, body, device=index)
        return parse_controller_data(raw, self.protocol)

    def set_custom_mode(self, index: int) -> None:
        self.send(SETCUSTOMMODE, b"", device=index)

    def update_leds(self, index: int, colours: Sequence[RGB]) -> None:
        colors = b"".join(bytes((r, g, b, 0)) for r, g, b in colours)
        body = struct.pack("<IH", 4 + 2 + len(colors), len(colours)) + colors
        self.send(UPDATELEDS, body, device=index)


class OpenRGBBackend(Backend):
    name = "openrgb"
    min_interval = 0.02

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                 device: int = 0, leds: int | None = None, debug: bool = False):
        self.host = host
        self.port = port
        self.device_index = device
        self.forced_leds = leds
        self.debug = debug
        self.client: OpenRGBClient | None = None
        self._lamps: list[Lamp] = []
        self.device_name = f"openrgb device {device}"

    @classmethod
    def available(cls) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", DEFAULT_PORT), 0.5):
                return True
        except OSError:
            return False

    def open(self) -> None:
        self.client = OpenRGBClient(self.host, self.port)
        self.client.connect()

        if self.device_index >= self.client.controller_count():
            raise BackendUnavailable(
                f"OpenRGB device index {self.device_index} does not exist "
                "(run `rgi detect` to list them)"
            )

        zones: list[tuple[str, int]] = []
        if self.forced_leds:
            count = self.forced_leds
        else:
            device = self.client.controller_data(self.device_index)
            self.device_name = device.get("name") or self.device_name
            zones = [(z["name"], z["leds_count"]) for z in device.get("zones", [])
                     if z.get("leds_count")]
            count = len(device.get("leds", [])) or sum(n for _, n in zones)

        if not count:
            raise ProtocolError(f"{self.device_name}: server reported no LEDs")

        self._lamps = self._build_lamps(count, zones)
        self.client.set_custom_mode(self.device_index)

        if self.debug:
            print(f"[openrgb] {self.device_name}: {count} LEDs, "
                  f"{len(zones)} zones, protocol {self.client.protocol}")
            for lamp in self._lamps[:16]:
                print(f"            lamp {lamp.index:>4}  {lamp.group:<12} {lamp.label}")

    def _build_lamps(self, count: int, zones: list[tuple[str, int]]) -> list[Lamp]:
        """Label lamps from zone names where we can, so lanes can find a key row.

        OpenRGB zone names for keyboards are usually the key legends ("1", "Esc")
        or a layout area ("Number Row"), and a zone covers a contiguous run of
        LEDs, so a zone of N named keys maps onto the next N LED indices.
        """
        labels: dict[int, tuple[str, str]] = {}
        index = 0
        for name, n in zones:
            for offset in range(n):
                labels[index] = (name, "zone")
                index += 1

        lamps = []
        for i in range(count):
            label, group = labels.get(i, (f"led{i}", "key"))
            if _looks_like_number_row(label):
                group = "number-row"
            lamps.append(Lamp(index=i, label=label, group=group))
        return lamps

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None

    def lamps(self) -> list[Lamp]:
        return list(self._lamps)

    def write(self, colours: Sequence[RGB]) -> None:
        if self.client is None:
            raise BackendUnavailable("backend is not open")
        self.client.update_leds(self.device_index, colours)


_NUMBER_ROW_LABELS = {"`", "~", "-", "_", "=", "+"} | {str(d) for d in range(10)}


def _looks_like_number_row(label: str) -> bool:
    return label.strip().lower() in _NUMBER_ROW_LABELS
