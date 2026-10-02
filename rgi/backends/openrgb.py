"""OpenRGB backend - one implementation, hundreds of keyboards.

OpenRGB (https://openrgb.org) already speaks the vendor protocol of a huge range
of boards. Rather than reimplementing each vendor's HID dialect, we talk to its
SDK server over TCP, which is a small, documented binary protocol:

    header  = "ORGB" + u32 device + u32 packet id + u32 body length  (little endian)
    port    = 6742 by default

Enable it in OpenRGB under Settings -> SDK Server -> "Start Server", then:

    rgi detect --backend openrgb          # what it sees
    rgi daemon --backend openrgb

This client targets the OpenRGB 1.0 SDK (protocol 6). The negotiation sends our
highest version in packet 40 and keeps ``min(server, client)``; at protocol 6
controllers are addressed by the **unique IDs** from the controller-count reply
rather than by their position, every controller packet is acknowledged with
packet 10, and the controller-data block carries matrix maps, per-LED display
names, a controller colour list, flags and a display name. The parse below
follows that documented field order (OpenRGBSDK.md, release 1.0) in a single
pass and refuses any block that does not end exactly where the last field does -
a wrong guess about the wire would silently paint the wrong LEDs, so drift is an
error, not a heuristic.

Receives match packet IDs: a v6 server ACKs controller packets and may send
server notifications, so the next packet on the socket is never assumed to be
the reply to the last request. ACKs are drained opportunistically so a
long-running painter does not fill the socket with them.

Below protocol 6 the same client falls back to index addressing and the older
block layout, so it also works against pre-1.0 servers.
"""

from __future__ import annotations

import select
import socket
import struct
import time
from typing import Sequence

from .base import RGB, Backend, BackendUnavailable, Lamp

MAGIC = b"ORGB"
DEFAULT_PORT = 6742

#: highest SDK protocol this client understands
CLIENT_PROTOCOL = 6

# packet ids we use
REQUEST_CONTROLLER_COUNT = 0
REQUEST_CONTROLLER_DATA = 1
ACK = 10
REQUEST_PROTOCOL_VERSION = 40
SET_CLIENT_NAME = 50
UPDATELEDS = 1050
SETCUSTOMMODE = 1100

# zone types (RGBController::zone_type); a SINGLE zone is one addressable colour
ZONE_TYPE_SINGLE = 0
ZONE_TYPE_LINEAR = 1
ZONE_TYPE_MATRIX = 2

# a matrix-map cell that does not carry an LED
MATRIX_EMPTY = 0xFFFFFFFF

# device types that mean "keyboard" (RGBController::device_type)
DEVICE_TYPE_KEYBOARD = 1


class ProtocolError(RuntimeError):
    pass


class ProtocolTimeout(ProtocolError):
    """No reply arrived in time (a protocol-0 server never answers packet 40)."""


class Reader:
    """Little-endian cursor over a controller-data block."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def left(self) -> int:
        return len(self.data) - self.pos

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise ProtocolError(
                f"wanted {n} bytes at offset {self.pos}, only {self.left()} left")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def u16(self) -> int:
        return struct.unpack("<H", self.take(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.take(4))[0]

    def _string(self, n: int) -> str:
        if n == 0:
            return ""
        return self.take(n).split(b"\0", 1)[0].decode("utf-8", "replace")

    def bstring(self) -> str:
        return self._string(self.u16())

    def bstring32(self) -> str:
        # controller ``configuration`` is the one string with a u32 length
        return self._string(self.u32())

    def rgb(self) -> RGB:
        # RGBColor is a little-endian unsigned int: r, g, b, pad on the wire
        raw = self.take(4)
        return (raw[0], raw[1], raw[2])


def _parse_matrix(r: Reader, declared_len: int) -> list[list[int]]:
    """Read a matrix map after its u16 byte length was consumed.

    ``declared_len`` covers the height/width header as well as the cells, so it
    must equal ``8 + 4 * height * width``; anything else is drift.
    """
    height = r.u32()
    width = r.u32()
    wanted = 8 + 4 * height * width
    if declared_len != wanted:
        raise ProtocolError(
            f"matrix map declares {declared_len} bytes but {height}x{width} "
            f"cells need {wanted}")
    return [[r.u32() for _ in range(width)] for _ in range(height)]


def _parse_mode(r: Reader, proto: int) -> dict:
    mode = {"name": r.bstring()}
    if proto < 6:
        # mode_value was removed at protocol 6: the server tracks it for us
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
    mode["colors"] = [r.rgb() for _ in range(r.u16())]
    return mode


def _parse_segment(r: Reader, proto: int) -> dict:
    seg = {"name": r.bstring(), "type": r.i32(), "start": r.u32(),
           "leds_count": r.u32(), "matrix": None, "flags": 0}
    if proto >= 6:
        matrix_len = r.u16()
        if matrix_len:
            seg["matrix"] = _parse_matrix(r, matrix_len)
        seg["flags"] = r.u32()
    return seg


def _parse_zone(r: Reader, proto: int) -> dict:
    zone = {"name": r.bstring(), "type": r.i32()}
    zone["leds_min"] = r.u32()
    zone["leds_max"] = r.u32()
    zone["leds_count"] = r.u32()
    zone["matrix"] = None
    matrix_len = r.u16()
    if matrix_len:
        zone["matrix"] = _parse_matrix(r, matrix_len)
    zone["segments"] = []
    if proto >= 4:
        zone["segments"] = [_parse_segment(r, proto) for _ in range(r.u16())]
    zone["flags"] = r.u32() if proto >= 5 else 0
    if proto >= 6:
        zone["active_mode"] = r.i32()
        zone["modes"] = [_parse_mode(r, proto) for _ in range(r.u16())]
        zone["display_name"] = r.bstring()
    return zone


def parse_controller_data(payload: bytes, proto: int) -> dict:
    """Decode a REQUEST_CONTROLLER_DATA response, protocol ``proto``.

    The order is the documented OpenRGB release-1.0 layout: device strings,
    modes (no ``value`` at v6), active_mode, zones (matrix map, segments from
    v4, flags from v5, per-zone modes/display name at v6), LEDs, the controller
    colour list (always present), LED display names and controller flags from
    v5, and display name/configuration at v6. The parse must consume the block
    exactly; anything left over (or a short read) raises ProtocolError, which is
    the signal to use --debug / --leds rather than paint a wrong guess.
    """
    r = Reader(payload)
    try:
        data_size = r.u32()
        if data_size != len(payload):
            raise ProtocolError(
                f"data_size says {data_size} bytes but the reply carries "
                f"{len(payload)}")

        device = {"type": r.i32(), "name": r.bstring()}
        if proto >= 1:
            device["vendor"] = r.bstring()
        device["description"] = r.bstring()
        device["version"] = r.bstring()
        device["serial"] = r.bstring()
        device["location"] = r.bstring()

        mode_count = r.u16()
        device["active_mode"] = r.i32()
        device["modes"] = [_parse_mode(r, proto) for _ in range(mode_count)]

        device["zones"] = [_parse_zone(r, proto) for _ in range(r.u16())]

        led_count = r.u16()
        device["leds"] = []
        for _ in range(led_count):
            led = {"name": r.bstring()}
            if proto < 6:
                led["value"] = r.u32()
            device["leds"].append(led)

        # the controller colour list is written at every protocol version
        device["colors"] = [r.rgb() for _ in range(r.u16())]

        device["led_display_names"] = []
        if proto >= 5:
            device["led_display_names"] = [r.bstring() for _ in range(r.u16())]
        device["flags"] = r.u32() if proto >= 5 else 0
        if proto >= 6:
            device["display_name"] = r.bstring()
            device["configuration"] = r.bstring32()

        if r.left():
            raise ProtocolError(
                f"protocol drift: {r.left()} bytes left over at offset "
                f"{r.pos} after parsing a protocol-{proto} controller block")
        device["_parse"] = {"proto": proto, "data_size": data_size}
        return device
    except ProtocolError as exc:
        raise ProtocolError(
            f"could not decode controller data at protocol {proto}: {exc}. "
            "Run `rgi detect --backend openrgb --debug` to dump the raw block, "
            "or pass --leds N to skip parsing.") from exc


class OpenRGBClient:
    """Minimal blocking client for the OpenRGB SDK server.

    Only the packets this backend needs are implemented. Replies are matched
    by packet ID: ACKs (packet 10) and server notifications are skipped rather
    than mistaken for the answer to the last request.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                 timeout: float = 3.0, client_protocol: int = CLIENT_PROTOCOL):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.client_protocol = min(int(client_protocol), CLIENT_PROTOCOL)
        self.sock: socket.socket | None = None
        self.protocol = 0
        self.last_ack: tuple[int, int] | None = None    # (acked packet id, status)
        self.acks_drained = 0
        self.stray_packets: list[int] = []              # ids skipped, newest last

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
            self.request_protocol_version()
            self.set_client_name(client_name)
        except Exception:
            self.close()
            raise

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
        try:
            self.sock.sendall(header + body)
        except ProtocolTimeout:
            raise
        except OSError as exc:
            raise BackendUnavailable(f"OpenRGB connection lost: {exc}") from exc

    def recv_exact(self, n: int) -> bytes:
        if self.sock is None:
            raise BackendUnavailable("not connected")
        buf = b""
        while len(buf) < n:
            try:
                chunk = self.sock.recv(n - len(buf))
            except (socket.timeout, TimeoutError):
                raise
            except OSError as exc:
                raise BackendUnavailable(f"OpenRGB connection lost: {exc}") from exc
            if not chunk:
                raise ProtocolError("connection closed by the OpenRGB server")
            buf += chunk
        return buf

    def recv_packet(self) -> tuple[int, int, bytes]:
        """Read one (device, packet id, body); no assumptions about ordering."""
        magic, device, packet_id, size = struct.unpack("<4sIII", self.recv_exact(16))
        if magic != MAGIC:
            raise ProtocolError(f"bad magic in reply: {magic!r}")
        return device, packet_id, self.recv_exact(size)

    def _note_ack(self, device: int, body: bytes) -> None:
        self.acks_drained += 1
        if len(body) >= 8:
            self.last_ack = struct.unpack_from("<II", body, 0)

    def _note_stray(self, packet_id: int) -> None:
        if len(self.stray_packets) < 32:
            self.stray_packets.append(packet_id)

    def _await(self, packet_id: int) -> bytes:
        """Read until ``packet_id`` arrives, skipping ACKs and notifications."""
        deadline = time.monotonic() + max(self.timeout, 0.05)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProtocolTimeout(
                    f"no reply to packet {packet_id} within {self.timeout:g}s")
            if self.sock is None:
                raise BackendUnavailable("not connected")
            self.sock.settimeout(remaining)
            try:
                device, pid, body = self.recv_packet()
            except (socket.timeout, TimeoutError):
                raise ProtocolTimeout(
                    f"no reply to packet {packet_id} within {self.timeout:g}s") from None
            if pid == ACK:
                self._note_ack(device, body)
                continue
            if pid != packet_id:
                self._note_stray(pid)
                continue
            return body

    def request(self, packet_id: int, body: bytes = b"", device: int = 0) -> bytes:
        """Send a request and return the reply with the matching packet id."""
        self.send(packet_id, body, device)
        try:
            return self._await(packet_id)
        finally:
            if self.sock is not None:
                try:
                    self.sock.settimeout(self.timeout)
                except OSError:
                    pass

    def drain(self, limit: int = 64) -> int:
        """Consume packets already waiting (v6 ACKs, server notifications).

        Painting is fire-and-forget, but a v6 server acknowledges every frame;
        without a read they would pile up in the socket buffer. Only packets
        already buffered are read. Returns how many were consumed.
        """
        if self.sock is None:
            return 0
        consumed = 0
        for _ in range(limit):
            try:
                readable, _, _ = select.select([self.sock], [], [], 0)
            except (OSError, ValueError, TypeError):
                break
            if not readable:
                break
            try:
                device, pid, body = self.recv_packet()
            except (OSError, ProtocolError, BackendUnavailable):
                break
            consumed += 1
            if pid == ACK:
                self._note_ack(device, body)
            else:
                self._note_stray(pid)
        return consumed

    # -- commands ---------------------------------------------------------
    def request_protocol_version(self) -> int:
        """Negotiate the protocol and remember ``min(server, client)``.

        A protocol-0 server never answers packet 40; after the timeout the
        connection is still valid at protocol 0.
        """
        self.send(REQUEST_PROTOCOL_VERSION, struct.pack("<I", self.client_protocol))
        try:
            body = self._await(REQUEST_PROTOCOL_VERSION)
        except ProtocolTimeout:
            self.protocol = 0
            return 0
        if len(body) < 4:
            raise ProtocolError(f"short protocol-version reply ({len(body)} bytes)")
        server = struct.unpack_from("<I", body, 0)[0]
        self.protocol = min(server, self.client_protocol)
        return self.protocol

    def set_client_name(self, name: str) -> None:
        # a bare NUL-terminated string, not a length-prefixed one
        self.send(SET_CLIENT_NAME, name.encode() + b"\0")

    def controller_ids(self) -> list[int]:
        """Addresses of every controller: unique IDs at v6, indexes below.

        At protocol 6+ the count reply is ``count`` followed by one u32 unique
        ID per controller; those IDs - not list positions - are what every
        later packet must carry.
        """
        body = self.request(REQUEST_CONTROLLER_COUNT)
        if len(body) < 4:
            raise ProtocolError(f"short controller-count reply ({len(body)} bytes)")
        count = struct.unpack_from("<I", body, 0)[0]
        if self.protocol >= 6:
            wanted = 4 + 4 * count
            if len(body) != wanted:
                raise ProtocolError(
                    f"controller-count reply is {len(body)} bytes; protocol 6 "
                    f"needs {wanted} for {count} controllers")
            return list(struct.unpack_from(f"<{count}I", body, 4))
        if len(body) != 4:
            raise ProtocolError(
                f"controller-count reply is {len(body)} bytes; expected 4 at "
                f"protocol {self.protocol}")
        return list(range(count))

    def controller_count(self) -> int:
        return len(self.controller_ids())

    def controller_data(self, device: int) -> dict:
        """Fetch and parse one controller by wire address (unique ID at v6)."""
        body = b""
        if self.protocol >= 1:
            body = struct.pack("<I", min(self.protocol, CLIENT_PROTOCOL))
        raw = self.request(REQUEST_CONTROLLER_DATA, body, device=device)
        return parse_controller_data(raw, self.protocol)

    def set_custom_mode(self, device: int) -> None:
        self.send(SETCUSTOMMODE, b"", device=device)
        self.drain()

    def update_leds(self, device: int, colours: Sequence[RGB]) -> None:
        colours = list(colours)
        colors = b"".join(bytes((r, g, b, 0)) for r, g, b in colours)
        body = struct.pack("<IH", 6 + len(colors), len(colours)) + colors
        self.send(UPDATELEDS, body, device=device)
        self.drain()


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
        #: wire address (unique ID at protocol 6, index below)
        self.device_id: int = device

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
        self.client.drain()             # server name sent after the handshake

        device_ids = self.client.controller_ids()
        if not device_ids or not 0 <= self.device_index < len(device_ids):
            raise BackendUnavailable(
                f"OpenRGB device index {self.device_index} does not exist "
                f"({len(device_ids)} controller(s); run `rgi detect` to list them)")
        # v6 addresses devices by unique ID; below v6 the ID is the index
        self.device_id = device_ids[self.device_index]

        device = None
        zones: list[dict] = []
        if self.forced_leds:
            count = self.forced_leds
        else:
            device = self.client.controller_data(self.device_id)
            self.device_name = (
                device.get("display_name") or device.get("name") or self.device_name)
            zones = list(device.get("zones", []))
            count = len(device.get("leds", [])) or sum(
                int(z.get("leds_count") or 0) for z in zones)

        if not count:
            raise ProtocolError(f"{self.device_name}: server reported no LEDs")

        self._lamps = self._build_lamps(count, device)
        # honest about what the device can show: a controller whose only zone
        # is a single-colour zone cannot carry one lane per LED
        self.per_lamp = not (
            not self.forced_leds
            and len(zones) == 1
            and zones[0].get("type") == ZONE_TYPE_SINGLE
        )
        self.client.set_custom_mode(self.device_id)

        if self.debug:
            print(f"[openrgb] {self.device_name}: {count} LEDs, "
                  f"{len(zones)} zones, protocol {self.client.protocol}, "
                  f"device id {self.device_id}, "
                  f"{'per-lamp' if self.per_lamp else 'single colour'}")
            for lamp in self._lamps[:16]:
                print(f"            lamp {lamp.index:>4}  {lamp.group:<12} {lamp.label}")

    def _build_lamps(self, count: int, device: dict | None) -> list[Lamp]:
        """One lamp per addressable LED, in LED-index order.

        The daemon indexes a frame by ``Lamp.index`` and writes lamps() order,
        so lamp ``i`` must be controller LED ``i``. Matrix maps and per-LED
        display names supply the label, group and physical x/y where OpenRGB
        has them; otherwise zones are applied in zone order, the way OpenRGB
        lays zones over the LED array, and per-LED names are used as labels.
        """
        names: list[str] = []
        slots: dict[int, tuple[str, str, float | None, float | None]] = {}

        if device:
            led_names = [led.get("name") or "" for led in device.get("leds", [])]
            display = list(device.get("led_display_names") or [])
            names = [
                (display[i] if i < len(display) else "")
                or (led_names[i] if i < len(led_names) else "")
                for i in range(count)
            ]

            running = 0
            for zone in device.get("zones", []):
                zone_label = zone.get("display_name") or zone.get("name") or ""
                matrix = zone.get("matrix")
                if matrix:
                    for row, cells in enumerate(matrix):
                        for col, cell in enumerate(cells):
                            if cell == MATRIX_EMPTY or not 0 <= cell < count:
                                continue
                            label = names[cell] or f"led{cell}"
                            group = ("number-row"
                                     if _looks_like_number_row(label) else "key")
                            slots[cell] = (label, group, float(col), float(row))
                    running += int(zone.get("leds_count") or 0)
                else:
                    for _ in range(int(zone.get("leds_count") or 0)):
                        if running >= count:
                            break
                        label = names[running] or zone_label or f"led{running}"
                        slots[running] = (label, "zone", None, None)
                        running += 1

        lamps = []
        for i in range(count):
            label, group, x, y = slots.get(
                i, (names[i] if i < len(names) and names[i] else f"led{i}",
                    "key", None, None))
            if group != "number-row" and _looks_like_number_row(label):
                group = "number-row"
            lamps.append(Lamp(index=i, label=label, group=group, x=x, y=y))
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
        if len(colours) != len(self._lamps):
            raise ProtocolError(
                f"expected {len(self._lamps)} colours (one per lamp), "
                f"got {len(colours)}")
        self.client.update_leds(self.device_id, colours)


_NUMBER_ROW_LABELS = {"`", "~", "-", "_", "=", "+"} | {str(d) for d in range(10)}


def _looks_like_number_row(label: str) -> bool:
    return label.strip().lower() in _NUMBER_ROW_LABELS
