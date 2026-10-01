"""OpenRGB protocol: packet framing and the controller-data parse.

There is no server here, so these tests build the byte stream the way OpenRGB
does and check we decode it - including the two layout variants the parser has
to tell apart (matrix maps written structured vs as a raw block).
"""

import struct
import unittest

from rgi.backends.openrgb import (
    MAGIC, OpenRGBClient, ProtocolError, parse_controller_data,
)


def bstring(text: str) -> bytes:
    raw = text.encode() + b"\0"
    return struct.pack("<H", len(raw)) + raw


def build_device(proto: int = 6, leds: int = 4, zones: int = 1,
                 matrix: bool = False) -> bytes:
    """A controller-data block, laid out per OpenRGBSDK.md."""
    body = struct.pack("<i", 1) + bstring("Test Board")
    if proto >= 1:
        body += bstring("Vendor")
    body += bstring("description") + bstring("1.0") + bstring("serial") + bstring("location")

    body += struct.pack("<H", 1)            # mode_count
    body += struct.pack("<I", 0)            # active mode
    body += bstring("Direct")
    if proto < 6:
        body += struct.pack("<i", 0)        # mode_value
    body += struct.pack("<II", 0, 0)        # flags, speed_min
    body += struct.pack("<I", 0)            # speed_max
    if proto >= 3:
        body += struct.pack("<II", 0, 100)  # brightness_min/max
    body += struct.pack("<II", 0, 0)        # colors_min/max
    body += struct.pack("<I", 0)            # speed
    if proto >= 3:
        body += struct.pack("<I", 100)      # brightness
    body += struct.pack("<II", 0, 0)        # direction, color_mode
    body += struct.pack("<H", 0)            # mode colours

    body += struct.pack("<H", zones)        # zone_count
    for z in range(zones):
        body += bstring(f"Number Row {z}")
        body += struct.pack("<i", 1)        # zone type
        body += struct.pack("<III", 1, 1, leds)   # leds_min, leds_max, leds_count
        if matrix:
            cells = b"\0\0\0\0" * leds
            body += struct.pack("<H", 1)    # matrix present
            body += struct.pack("<II", 1, leds) + cells
        else:
            body += struct.pack("<H", 0)    # no matrix
        if proto >= 4:
            body += struct.pack("<H", 0)    # no segments

    body += struct.pack("<H", leds)         # led_count
    for i in range(leds):
        body += bstring(f"Key {i}")

    return struct.pack("<I", len(body)) + body


class TestControllerData(unittest.TestCase):
    def test_parses_documented_layout(self):
        device = parse_controller_data(build_device(), proto=6)
        self.assertEqual(device["name"], "Test Board")
        self.assertEqual(device["vendor"], "Vendor")
        self.assertEqual(len(device["leds"]), 4)
        self.assertEqual(device["zones"][0]["name"], "Number Row 0")
        self.assertEqual(device["zones"][0]["leds_count"], 4)
        self.assertEqual(device["modes"][0]["name"], "Direct")
        self.assertEqual(device["_parse"]["matrix"], "structured")

    def test_parses_a_structured_matrix_map(self):
        device = parse_controller_data(build_device(matrix=True), proto=6)
        self.assertEqual(len(device["leds"]), 4)

    def test_protocol_0_has_no_vendor_field(self):
        device = parse_controller_data(build_device(proto=0), proto=0)
        self.assertNotIn("vendor", device)
        self.assertEqual(len(device["leds"]), 4)

    def test_protocol_2_has_no_brightness_fields(self):
        device = parse_controller_data(build_device(proto=2), proto=2)
        self.assertEqual(len(device["leds"]), 4)
        self.assertNotIn("brightness", device["modes"][0])

    def test_garbage_is_refused_with_a_useful_message(self):
        with self.assertRaises(ProtocolError) as ctx:
            parse_controller_data(b"\x00" * 12, proto=6)
        self.assertIn("could not decode", str(ctx.exception))


class TestPackets(unittest.TestCase):
    def setUp(self):
        self.sent: list[bytes] = []
        self.client = OpenRGBClient()
        self.client.sock = type("S", (), {
            "sendall": lambda _self, data: self.sent.append(data),
            "settimeout": lambda _self, t: None,
            "close": lambda _self: None,
        })()

    def test_header_format(self):
        self.client.send(1100, b"", device=2)
        magic, device, packet, size = struct.unpack("<4sIII", self.sent[0])
        self.assertEqual(magic, MAGIC)
        self.assertEqual((device, packet, size), (2, 1100, 0))

    def test_client_name_is_a_bare_nul_terminated_string(self):
        self.client.set_client_name("rgi")
        self.assertEqual(self.sent[0][16:], b"rgi\0")

    def test_update_leds_body(self):
        self.client.update_leds(0, [(1, 2, 3), (4, 5, 6)])
        body = self.sent[0][16:]
        data_size, count = struct.unpack("<IH", body[:6])
        self.assertEqual(count, 2)
        self.assertEqual(data_size, len(body))
        # 4 bytes per colour on the wire, RGB order
        self.assertEqual(body[6:], bytes([1, 2, 3, 0, 4, 5, 6, 0]))


if __name__ == "__main__":
    unittest.main()
