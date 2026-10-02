"""OpenRGB protocol 6 (OpenRGB 1.0): framing, parse and the backend.

No OpenRGB and no network beyond localhost: a stdlib TCP server stands in for
the SDK server, records every packet and answers with the documented release-1.0
layout. The byte builders in this file are the independent half of the test -
they write the wire format field by field, so a parser that mis-reads a field
(or a client that addresses the wrong device) fails against them.
"""

from __future__ import annotations

import socketserver
import struct
import threading
import time
import unittest

from rgi.backends.base import BackendUnavailable, Lamp
from rgi.backends.openrgb import (
    ACK, MAGIC, OpenRGBBackend, OpenRGBClient, ProtocolError,
    REQUEST_CONTROLLER_COUNT, REQUEST_CONTROLLER_DATA, REQUEST_PROTOCOL_VERSION,
    SETCUSTOMMODE, UPDATELEDS, ZONE_TYPE_SINGLE, parse_controller_data,
)

SET_SERVER_NAME = 51          # the SDK server announces itself after the handshake
SET_CLIENT_NAME = 50


def wait_for(predicate, timeout: float = 3.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


# ---------------------------------------------------------------------------
# wire builders, one per documented block
# ---------------------------------------------------------------------------

def bstring(text: str) -> bytes:
    raw = text.encode() + b"\0"
    return struct.pack("<H", len(raw)) + raw


def rgb(r: int, g: int, b: int) -> bytes:
    # RGBColor is a little-endian u32: red, green, blue, pad
    return bytes((r, g, b, 0))


def matrix(rows: list[list[int]]) -> bytes:
    height = len(rows)
    width = len(rows[0]) if height else 0
    cells = b"".join(struct.pack("<I", cell) for row in rows for cell in row)
    return struct.pack("<HII", 8 + len(cells), height, width) + cells


def mode(proto: int = 6, name: str = "Direct", *, colors=(),
         flags: int = 0, speed_min: int = 0, speed_max: int = 100,
         brightness_min: int = 0, brightness_max: int = 100, speed: int = 0,
         brightness: int = 100, direction: int = 0, color_mode: int = 0) -> bytes:
    out = bstring(name)
    if proto < 6:
        out += struct.pack("<i", 0)                # mode_value, gone at v6
    out += struct.pack("<I", flags)
    out += struct.pack("<II", speed_min, speed_max)
    if proto >= 3:
        out += struct.pack("<II", brightness_min, brightness_max)
    out += struct.pack("<II", 0, 0)                # colors_min, colors_max
    out += struct.pack("<I", speed)
    if proto >= 3:
        out += struct.pack("<I", brightness)
    out += struct.pack("<II", direction, color_mode)
    out += struct.pack("<H", len(colors))
    return out + b"".join(rgb(*c) for c in colors)


def segment(name: str = "Segment", *, proto: int = 6, type: int = 1,
            start: int = 0, leds_count: int = 1, cells=None, flags: int = 0) -> bytes:
    out = bstring(name) + struct.pack("<iII", type, start, leds_count)
    if proto >= 6:
        out += matrix(cells) if cells else struct.pack("<H", 0)
        out += struct.pack("<I", flags)
    return out


def zone(name: str = "Zone", *, proto: int = 6, type: int = 1,
         leds_min: int = 1, leds_max: int = 64, leds_count: int = 4,
         cells=None, segments=(), flags: int = 0, zone_modes=(), active_mode: int = 0,
         display_name: str = "") -> bytes:
    out = bstring(name) + struct.pack("<iIII", type, leds_min, leds_max, leds_count)
    out += matrix(cells) if cells else struct.pack("<H", 0)
    if proto >= 4:
        segments = list(segments)
        out += struct.pack("<H", len(segments)) + b"".join(segments)
    if proto >= 5:
        out += struct.pack("<I", flags)
    if proto >= 6:
        zone_modes = list(zone_modes)
        out += struct.pack("<i", active_mode)
        out += struct.pack("<H", len(zone_modes)) + b"".join(zone_modes)
        out += bstring(display_name)
    return out


def device(proto: int = 6, *, name: str = "Test Board", vendor: str = "Vendor",
           led_names=("led0", "led1", "led2", "led3"), display_names=None,
           colors=((255, 0, 0),), zones=None, modes=None, active_mode: int = 0,
           flags: int = 7, display_name: str = "Test Board",
           configuration: str = '{"layout": "test"}') -> bytes:
    if modes is None:
        modes = [mode(proto, "Direct")]
    if zones is None:
        zones = [zone(proto=proto, leds_count=len(led_names))]
    body = struct.pack("<i", 1) + bstring(name)
    if proto >= 1:
        body += bstring(vendor)
    body += bstring("description") + bstring("1.0") + bstring("serial") + bstring("location")
    body += struct.pack("<H", len(modes)) + struct.pack("<i", active_mode)
    body += b"".join(modes)
    body += struct.pack("<H", len(zones)) + b"".join(zones)
    body += struct.pack("<H", len(led_names))
    for led_name in led_names:
        body += bstring(led_name)
        if proto < 6:
            body += struct.pack("<I", 0)           # led_value, gone at v6
    body += struct.pack("<H", len(colors)) + b"".join(rgb(*c) for c in colors)
    if proto >= 5:
        names = list(display_names or [])
        body += struct.pack("<H", len(names)) + b"".join(bstring(n) for n in names)
        body += struct.pack("<I", flags)
    if proto >= 6:
        body += bstring(display_name)
        raw = configuration.encode() + b"\0"
        body += struct.pack("<I", len(raw)) + raw
    return struct.pack("<I", len(body) + 4) + body


def keyboard_device() -> bytes:
    """Seven LEDs: a 2x3 matrix of named keys plus one zone-only LED."""
    return device(
        name="Mock Keyboard", display_name="Mock Keyboard",
        led_names=("k0", "k1", "k2", "k3", "k4", "k5", "k6"),
        display_names=("Esc", "`", "1", "2", "3", "4", "Underglow"),
        colors=((10, 20, 30),),
        zones=[
            zone("Key Grid", display_name="Keys", type=2, leds_count=6,
                 cells=[[0, 1, 2], [3, 4, 5]], flags=1),
            zone("Underglow", display_name="Base", type=1, leds_count=1),
        ],
    )


def full_device() -> bytes:
    """A device exercising every v6-only field at once."""
    return device(
        name="Full Board", vendor="Vendor", flags=5, active_mode=1,
        led_names=("k0", "k1", "k2", "k3", "k4"),
        display_names=("Esc", "A", "B", "C", "Logo"),
        colors=((255, 0, 0), (0, 255, 0)),
        modes=[mode(6, "Direct", colors=[(1, 2, 3)])],
        display_name="Full Board (display)", configuration='{"a": 1}',
        zones=[
            zone("Keys", display_name="Key Grid", type=2, leds_min=1, leds_max=8,
                 leds_count=4, cells=[[0, 1], [2, 3]], flags=9, active_mode=1,
                 zone_modes=[mode(6, "Zone Direct")],
                 segments=[
                     segment("Left", start=0, leds_count=2, cells=[[0, 1]], flags=3),
                     segment("Right", start=2, leds_count=2),
                 ]),
            zone("Logo Zone", display_name="Logo", type=0, leds_count=1),
        ],
    )


# ---------------------------------------------------------------------------
# the mock SDK server
# ---------------------------------------------------------------------------

class _MockOpenRGBServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request, client_address):   # a hung test stays quiet
        pass


class MockOpenRGB:
    """A recording stand-in for one OpenRGB SDK server.

    ``hold_acks`` makes UpdateLEDs ACKs wait until the next packet arrives, so
    a test can put an ACK in front of a reply deliberately. ``count_reply``
    overrides the controller-count body (for malformed-reply tests).
    """

    def __init__(self, *, version: int = 6, ids=(0,), count: int | None = None,
                 blocks: dict | None = None, block: bytes | None = None,
                 count_reply: bytes | None = None, acks: bool = True,
                 hold_acks: bool = False, server_string: str | None = "mock server"):
        self.version = version
        self.ids = list(ids)
        self.count = count
        self.blocks = dict(blocks or {})
        if block is not None:
            for controller_id in self.ids:
                self.blocks.setdefault(controller_id, block)
        self.count_reply = count_reply
        self.acks = acks
        self.hold_acks = hold_acks
        self.server_string = server_string
        self.requests: list[tuple[int, int, bytes]] = []
        self._held: list[tuple[int, int]] = []
        self.lock = threading.Lock()
        self.server = _MockOpenRGBServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)

    # -- observation -------------------------------------------------------
    def sent(self, packet_id: int | None = None) -> list[tuple[int, int, bytes]]:
        with self.lock:
            out = list(self.requests)
        return [r for r in out if packet_id is None or r[1] == packet_id]

    def wait_packet(self, packet_id: int, timeout: float = 3.0) -> tuple[int, int, bytes]:
        if not wait_for(lambda: bool(self.sent(packet_id)), timeout):
            got = [pkt for _, pkt, _ in self.sent()]
            raise AssertionError(f"no packet {packet_id} arrived; got {got}")
        return self.sent(packet_id)[-1]

    # -- the wire ----------------------------------------------------------
    @staticmethod
    def _read_exact(sock, n: int) -> bytes | None:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    @staticmethod
    def _send(sock, device: int, packet_id: int, body: bytes = b"") -> None:
        sock.sendall(MAGIC + struct.pack("<III", device, packet_id, len(body)) + body)

    def _send_ack(self, sock, device: int, packet_id: int, status: int = 0) -> None:
        if self.version >= 6 and self.acks:
            self._send(sock, device, ACK, struct.pack("<II", packet_id, status))

    def _handler(self):
        mock = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    while True:
                        header = mock._read_exact(self.request, 16)
                        if header is None:
                            return
                        magic, dev, packet_id, size = struct.unpack("<4sIII", header)
                        if magic != MAGIC:
                            return
                        body = mock._read_exact(self.request, size) if size else b""
                        if body is None:
                            return
                        with mock.lock:
                            mock.requests.append((dev, packet_id, body))
                        mock._dispatch(self.request, dev, packet_id, body)
                except OSError:
                    return

        return Handler

    def _dispatch(self, sock, dev: int, packet_id: int, body: bytes) -> None:
        # a held ACK goes out ahead of the next reply, like a real server that
        # finished the previous controller packet while this one was in flight
        with self.lock:
            held, self._held = self._held, []
        for acked_dev, acked_pkt in held:
            self._send_ack(sock, acked_dev, acked_pkt)

        if packet_id == REQUEST_PROTOCOL_VERSION:
            if self.version >= 1:
                self._send(sock, 0, REQUEST_PROTOCOL_VERSION,
                           struct.pack("<I", self.version))
            if self.server_string is not None and self.version >= 6:
                self._send(sock, 0, SET_SERVER_NAME,
                           self.server_string.encode() + b"\0")
            return

        if packet_id == REQUEST_CONTROLLER_COUNT:
            if self.count_reply is not None:
                self._send(sock, 0, REQUEST_CONTROLLER_COUNT, self.count_reply)
                return
            count = len(self.ids) if self.count is None else self.count
            if self.version >= 6:
                reply = struct.pack("<I", count)
                reply += struct.pack(f"<{count}I", *self.ids[:count])
            else:
                reply = struct.pack("<I", count)
            self._send(sock, 0, REQUEST_CONTROLLER_COUNT, reply)
            return

        if packet_id == REQUEST_CONTROLLER_DATA:
            self._send(sock, dev, REQUEST_CONTROLLER_DATA,
                       self.blocks.get(dev, device()))
            return

        if packet_id in (UPDATELEDS, SETCUSTOMMODE, SET_CLIENT_NAME):
            if self.hold_acks and packet_id == UPDATELEDS:
                with self.lock:
                    self._held.append((dev, packet_id))
            else:
                self._send_ack(sock, dev, packet_id)
            return


# ---------------------------------------------------------------------------
# the parse
# ---------------------------------------------------------------------------

class TestControllerDataParse(unittest.TestCase):
    def test_v6_full_parse(self):
        block = full_device()
        dev = parse_controller_data(block, 6)
        self.assertEqual(dev["type"], 1)
        self.assertEqual(dev["name"], "Full Board")
        self.assertEqual(dev["vendor"], "Vendor")
        self.assertEqual(dev["description"], "description")
        self.assertEqual(dev["version"], "1.0")
        self.assertEqual(dev["serial"], "serial")
        self.assertEqual(dev["location"], "location")

        self.assertEqual(dev["active_mode"], 1)
        self.assertEqual([m["name"] for m in dev["modes"]], ["Direct"])
        self.assertEqual(dev["modes"][0]["colors"], [(1, 2, 3)])
        self.assertEqual(dev["modes"][0]["brightness"], 100)

        zone0, zone1 = dev["zones"]
        self.assertEqual(zone0["name"], "Keys")
        self.assertEqual(zone0["type"], 2)
        self.assertEqual((zone0["leds_min"], zone0["leds_max"], zone0["leds_count"]),
                         (1, 8, 4))
        self.assertEqual(zone0["matrix"], [[0, 1], [2, 3]])
        self.assertEqual(zone0["flags"], 9)
        self.assertEqual(zone0["active_mode"], 1)
        self.assertEqual([m["name"] for m in zone0["modes"]], ["Zone Direct"])
        self.assertEqual(zone0["display_name"], "Key Grid")
        seg0, seg1 = zone0["segments"]
        self.assertEqual((seg0["name"], seg0["start"], seg0["leds_count"]),
                         ("Left", 0, 2))
        self.assertEqual(seg0["matrix"], [[0, 1]])
        self.assertEqual(seg0["flags"], 3)
        self.assertEqual((seg1["name"], seg1["start"], seg1["leds_count"]),
                         ("Right", 2, 2))
        self.assertIsNone(seg1["matrix"])
        self.assertEqual(seg1["flags"], 0)
        self.assertIsNone(zone1["matrix"])
        self.assertEqual(zone1["segments"], [])
        self.assertEqual(zone1["display_name"], "Logo")

        self.assertEqual([l["name"] for l in dev["leds"]],
                         ["k0", "k1", "k2", "k3", "k4"])
        self.assertEqual(dev["colors"], [(255, 0, 0), (0, 255, 0)])
        self.assertEqual(dev["led_display_names"], ["Esc", "A", "B", "C", "Logo"])
        self.assertEqual(dev["flags"], 5)
        self.assertEqual(dev["display_name"], "Full Board (display)")
        self.assertEqual(dev["configuration"], '{"a": 1}')
        self.assertEqual(dev["_parse"], {"proto": 6, "data_size": len(block)})

    def test_v6_modes_have_no_value_field(self):
        dev = parse_controller_data(full_device(), 6)
        self.assertNotIn("value", dev["modes"][0])
        self.assertNotIn("value", dev["zones"][0]["modes"][0])

    def test_v5_layout_still_parses(self):
        block = device(
            proto=5, led_names=("k0", "k1"), display_names=("A", "B"),
            modes=[mode(5, "Direct")], flags=6,
            zones=[zone("Zone", proto=5, leds_count=2, flags=4,
                        segments=[segment("S", proto=5, leds_count=2)])],
        )
        dev = parse_controller_data(block, 5)
        self.assertEqual(dev["modes"][0]["value"], 0)
        self.assertEqual(dev["modes"][0]["brightness"], 100)
        self.assertEqual([l["value"] for l in dev["leds"]], [0, 0])
        self.assertEqual(dev["led_display_names"], ["A", "B"])
        self.assertEqual(dev["flags"], 6)
        self.assertNotIn("display_name", dev)
        zone0 = dev["zones"][0]
        self.assertEqual(zone0["flags"], 4)
        self.assertNotIn("modes", zone0)
        self.assertNotIn("display_name", zone0)
        self.assertIsNone(zone0["segments"][0]["matrix"])
        self.assertEqual(zone0["segments"][0]["name"], "S")
        self.assertEqual(dev["colors"], [(255, 0, 0)])

    def test_protocol_0_has_no_vendor_field(self):
        block = device(proto=0, led_names=("k0",), modes=[mode(0, "Off")])
        dev = parse_controller_data(block, 0)
        self.assertNotIn("vendor", dev)
        self.assertEqual(dev["modes"][0]["value"], 0)
        self.assertNotIn("brightness", dev["modes"][0])
        # num_colors is written at every protocol version, including 0
        self.assertEqual(dev["colors"], [(255, 0, 0)])

    def test_num_colors_is_parsed_even_without_display_names(self):
        block = device(display_names=None, colors=((1, 2, 3), (4, 5, 6)))
        dev = parse_controller_data(block, 6)
        self.assertEqual(dev["colors"], [(1, 2, 3), (4, 5, 6)])
        self.assertEqual(dev["led_display_names"], [])

    def test_trailing_bytes_are_refused_as_drift(self):
        blown = full_device() + b"\x00"
        blown = struct.pack("<I", len(blown)) + blown[4:]     # keep data_size honest
        with self.assertRaises(ProtocolError) as ctx:
            parse_controller_data(blown, 6)
        message = str(ctx.exception)
        self.assertIn("protocol drift", message)
        self.assertIn("1 bytes left over", message)

    def test_truncated_block_is_refused(self):
        with self.assertRaises(ProtocolError) as ctx:
            parse_controller_data(full_device()[:-4], 6)
        self.assertIn("could not decode", str(ctx.exception))

    def test_matrix_length_mismatch_is_refused(self):
        bad_zone = (
            bstring("Bad") + struct.pack("<iIII", 2, 1, 4, 2)
            + struct.pack("<H", 12)                  # claims 12 bytes...
            + struct.pack("<II", 1, 2)               # ...but 1x2 cells need 16
            + struct.pack("<II", 0, 1)
            + struct.pack("<H", 0)                   # no segments
            + struct.pack("<I", 0)                   # zone flags
            + struct.pack("<i", 0) + struct.pack("<H", 0)   # active mode, no modes
            + bstring("")
        )
        with self.assertRaises(ProtocolError) as ctx:
            parse_controller_data(device(zones=[bad_zone]), 6)
        self.assertIn("matrix map declares 12", str(ctx.exception))


# ---------------------------------------------------------------------------
# the client
# ---------------------------------------------------------------------------

class ClientTestCase(unittest.TestCase):
    def setUp(self):
        self.mock = MockOpenRGB(version=6, block=keyboard_device())
        self.addCleanup(self.mock.stop)
        self.client = OpenRGBClient(port=self.mock.port, timeout=1.0)
        self.addCleanup(self.client.close)


class TestHandshake(ClientTestCase):
    def test_asks_for_six_and_keeps_minimum(self):
        self.client.connect()
        self.assertEqual(self.client.protocol, 6)
        dev, pkt, body = self.mock.wait_packet(REQUEST_PROTOCOL_VERSION)
        self.assertEqual((dev, pkt), (0, REQUEST_PROTOCOL_VERSION))
        self.assertEqual(body, struct.pack("<I", 6))

    def test_client_caps_a_newer_server(self):
        self.mock.version = 7
        self.client.connect()
        self.assertEqual(self.client.protocol, 6)

    def test_older_server_negotiates_down(self):
        mock = MockOpenRGB(version=4, ids=(0, 1), block=keyboard_device())
        self.addCleanup(mock.stop)
        client = OpenRGBClient(port=mock.port, timeout=1.0)
        self.addCleanup(client.close)
        client.connect()
        self.assertEqual(client.protocol, 4)

    def test_protocol_0_server_times_out_and_falls_back_to_indexes(self):
        mock = MockOpenRGB(version=0, ids=(0,), block=device(proto=0))
        self.addCleanup(mock.stop)
        client = OpenRGBClient(port=mock.port, timeout=0.25)
        self.addCleanup(client.close)
        client.connect()
        self.assertEqual(client.protocol, 0)
        self.assertEqual(client.controller_ids(), [0])
        self.assertEqual(client.controller_data(0)["name"], "Test Board")
        self.assertEqual(mock.wait_packet(REQUEST_CONTROLLER_DATA)[2], b"")

    def test_client_name_is_a_bare_nul_terminated_string(self):
        self.client.connect(client_name="rgi")
        self.assertEqual(self.mock.wait_packet(SET_CLIENT_NAME)[2], b"rgi\0")


class TestControllerIds(ClientTestCase):
    def test_v6_addresses_devices_by_unique_id(self):
        self.mock.ids = [7, 42]
        self.mock.blocks = {7: device(led_names=("a",)), 42: device(led_names=("b",))}
        self.client.connect()

        self.assertEqual(self.client.controller_ids(), [7, 42])
        self.assertEqual(self.client.controller_count(), 2)

        parsed = self.client.controller_data(42)
        self.assertEqual(parsed["leds"][0]["name"], "b")
        dev, pkt, body = self.mock.wait_packet(REQUEST_CONTROLLER_DATA)
        self.assertEqual((dev, pkt), (42, REQUEST_CONTROLLER_DATA))
        self.assertEqual(body, struct.pack("<I", 6))

    def test_short_v6_count_reply_is_refused(self):
        self.mock.count_reply = struct.pack("<I", 2)       # says 2, carries no IDs
        self.client.connect()
        with self.assertRaises(ProtocolError) as ctx:
            self.client.controller_ids()
        self.assertIn("protocol 6", str(ctx.exception))

    def test_old_count_reply_is_only_the_count(self):
        mock = MockOpenRGB(version=5, ids=(0, 1), block=device(proto=5))
        self.addCleanup(mock.stop)
        client = OpenRGBClient(port=mock.port, timeout=1.0)
        self.addCleanup(client.close)
        client.connect()
        self.assertEqual(client.controller_ids(), [0, 1])
        client.controller_data(1)
        self.assertEqual(mock.wait_packet(REQUEST_CONTROLLER_DATA)[0], 1)


class TestPackets(ClientTestCase):
    def test_update_leds_body_and_target_device(self):
        self.client.connect()
        self.client.update_leds(42, [(1, 2, 3), (4, 5, 6)])
        dev, pkt, body = self.mock.wait_packet(UPDATELEDS)
        self.assertEqual((dev, pkt), (42, UPDATELEDS))
        data_size, count = struct.unpack_from("<IH", body, 0)
        self.assertEqual(count, 2)
        self.assertEqual(data_size, len(body))            # data_size includes itself
        self.assertEqual(data_size, 6 + 4 * 2)
        # 4 bytes per colour on the wire: RGB order, alpha 0
        self.assertEqual(body[6:], bytes([1, 2, 3, 0, 4, 5, 6, 0]))


class TestAckDraining(unittest.TestCase):
    def test_acks_are_matched_and_drained(self):
        mock = MockOpenRGB(version=6, block=keyboard_device(), hold_acks=True)
        self.addCleanup(mock.stop)
        client = OpenRGBClient(port=mock.port, timeout=1.0)
        self.addCleanup(client.close)
        client.connect()

        client.update_leds(0, [(9, 8, 7)])
        # the ACK for the frame is held back and flushed ahead of the next
        # reply: a client that assumed the next packet was its own would read
        # the ACK as the controller count
        self.assertEqual(client.controller_ids(), [0])
        self.assertEqual(client.last_ack, (UPDATELEDS, 0))
        self.assertGreaterEqual(client.acks_drained, 1)
        # the server name packet sent after the handshake must also be skipped
        self.assertIn(SET_SERVER_NAME, client.stray_packets)

    def test_no_ack_is_waited_for_below_v6(self):
        mock = MockOpenRGB(version=5, ids=(0,), block=device(proto=5))
        self.addCleanup(mock.stop)
        client = OpenRGBClient(port=mock.port, timeout=1.0)
        self.addCleanup(client.close)
        client.connect()
        client.update_leds(0, [(1, 1, 1)])
        self.assertEqual(client.acks_drained, 0)
        self.assertEqual(client.controller_ids(), [0])


# ---------------------------------------------------------------------------
# the backend
# ---------------------------------------------------------------------------

class TestBackend(unittest.TestCase):
    def make(self, *, version: int = 6, ids=(11, 22), device_index: int = 1,
             block: bytes | None = None, **kwargs) -> OpenRGBBackend:
        block = keyboard_device() if block is None else block
        self.mock = MockOpenRGB(version=version, ids=ids,
                                blocks={i: block for i in ids})
        self.addCleanup(self.mock.stop)
        backend = OpenRGBBackend(port=self.mock.port, device=device_index, **kwargs)
        self.addCleanup(backend.close)
        return backend

    def test_open_resolves_index_to_unique_id_and_addresses_it(self):
        backend = self.make()
        backend.open()
        self.assertEqual(backend.device_id, 22)
        self.assertEqual(backend.device_name, "Mock Keyboard")
        dev, pkt, body = self.mock.wait_packet(REQUEST_CONTROLLER_DATA)
        self.assertEqual((dev, pkt), (22, REQUEST_CONTROLLER_DATA))
        self.assertEqual(body, struct.pack("<I", 6))
        self.assertEqual(self.mock.wait_packet(SETCUSTOMMODE)[0], 22)

        backend.write([(0, 0, 0)] * len(backend.lamps()))
        self.assertEqual(self.mock.wait_packet(UPDATELEDS)[0], 22)

    def test_missing_device_is_refused(self):
        backend = self.make(ids=(11,), device_index=3)
        with self.assertRaises(BackendUnavailable):
            backend.open()

    def test_lamps_come_from_matrix_map_and_display_names(self):
        backend = self.make()
        backend.open()
        lamps = backend.lamps()
        self.assertEqual([l.index for l in lamps], list(range(7)))
        self.assertEqual(lamps[2], Lamp(index=2, label="1", group="number-row",
                                        x=2.0, y=0.0))
        self.assertEqual(lamps[0].label, "Esc")
        self.assertEqual((lamps[0].group, lamps[0].x, lamps[0].y), ("key", 0.0, 0.0))
        self.assertEqual(lamps[5].label, "4")
        self.assertEqual(lamps[5].group, "number-row")
        self.assertEqual(lamps[6].label, "Underglow")
        self.assertEqual(lamps[6].group, "zone")
        self.assertIsNone(lamps[6].x)
        self.assertIsNone(lamps[6].y)
        self.assertEqual(backend.default_lanes(4), [1, 2, 3, 4])
        self.assertTrue(backend.per_lamp)

    def test_zone_order_and_led_names_label_a_zoneless_device(self):
        block = device(led_names=("k0", "k1", "k2"), display_names=None,
                       zones=[zone("Main", leds_count=3)])
        backend = self.make(block=block)
        backend.open()
        lamps = backend.lamps()
        self.assertEqual([l.label for l in lamps], ["k0", "k1", "k2"])
        self.assertEqual({l.group for l in lamps}, {"zone"})
        self.assertTrue(all(l.x is None and l.y is None for l in lamps))

    def test_unnamed_uncovered_leds_get_a_placeholder_label(self):
        block = device(led_names=("", ""), display_names=None, zones=[])
        backend = self.make(block=block)
        backend.open()
        self.assertEqual([l.label for l in backend.lamps()], ["led0", "led1"])

    def test_single_colour_zone_is_reported_as_single_colour(self):
        block = device(led_names=("light",),
                       zones=[zone("Light", type=ZONE_TYPE_SINGLE, leds_count=1)])
        backend = self.make(block=block)
        backend.open()
        self.assertFalse(backend.per_lamp)

    def test_forced_led_count_skips_parsing_and_uses_the_resolved_id(self):
        backend = self.make(leds=4, device_index=0)
        backend.open()
        self.assertEqual(len(backend.lamps()), 4)
        self.assertEqual(backend.lamps()[0].label, "led0")
        self.assertEqual(self.mock.sent(REQUEST_CONTROLLER_DATA), [])
        backend.write([(1, 2, 3)] * 4)
        self.assertEqual(self.mock.wait_packet(UPDATELEDS)[0], 11)   # ids[0]

    def test_write_requires_one_colour_per_lamp(self):
        backend = self.make()
        backend.open()
        with self.assertRaises(ProtocolError):
            backend.write([(0, 0, 0)])


if __name__ == "__main__":
    unittest.main()
