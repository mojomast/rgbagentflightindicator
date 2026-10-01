"""EVision packet framing - no hardware needed.

The fixture in ``test_capability_packet_is_the_one_the_board_answered`` is the
exact byte string sent to a real Magic Refiner MK 17 (320F:501D), which replied
with an ``aa 55`` capability payload reporting 126 lamps. If it ever changes,
the framing has changed.
"""

import unittest

from rgi.backends.evision import (
    CAPABILITY_MAGIC, CMD_DYNAMIC_COLORS, MAX_CHUNK, REPORT_ID, build,
)


class TestFraming(unittest.TestCase):
    def test_report_shape(self):
        pkt = build(CMD_DYNAMIC_COLORS, b"\x01\x02\x03", offset=0x0100)
        self.assertEqual(len(pkt), 64)
        self.assertEqual(pkt[0], REPORT_ID)
        self.assertEqual(pkt[3], CMD_DYNAMIC_COLORS)
        self.assertEqual(pkt[4], 3)                      # payload size
        self.assertEqual(pkt[5] | (pkt[6] << 8), 0x0100)  # little-endian offset
        self.assertEqual(pkt[7], 0)
        self.assertEqual(pkt[8:11], b"\x01\x02\x03")

    def test_checksum_is_the_sum_of_the_body(self):
        pkt = build(CMD_DYNAMIC_COLORS, b"\xff\x10", offset=54)
        checksum = pkt[1] | (pkt[2] << 8)
        self.assertEqual(checksum, sum(pkt[3:64]) & 0xFFFF)

    def test_checksum_wraps_at_16_bits(self):
        pkt = build(CMD_DYNAMIC_COLORS, b"\xff" * MAX_CHUNK)
        self.assertEqual(pkt[1] | (pkt[2] << 8), sum(pkt[3:64]) & 0xFFFF)

    def test_capability_packet_is_the_one_the_board_answered(self):
        pkt = build(0x03, b"\0" * 7, 0)
        self.assertEqual(pkt[:8], bytes([0x04, 0x0A, 0x00, 0x03, 0x07, 0x00, 0x00, 0x00]))
        self.assertEqual(CAPABILITY_MAGIC, (0xAA, 0x55))

    def test_payload_limit(self):
        build(CMD_DYNAMIC_COLORS, b"\0" * MAX_CHUNK)          # exactly the limit
        with self.assertRaises(ValueError):
            build(CMD_DYNAMIC_COLORS, b"\0" * (MAX_CHUNK + 1))


class TestChunking(unittest.TestCase):
    def test_126_lamps_needs_seven_packets(self):
        payload = 126 * 3
        offsets = list(range(0, payload, MAX_CHUNK))
        self.assertEqual(offsets, [0, 56, 112, 168, 224, 280, 336])
        self.assertEqual(len(offsets), 7)
        # the last chunk is short, and the offsets are what the board expects
        self.assertEqual(payload - offsets[-1], 42)

    def test_lamp_count_comes_from_the_capability_payload(self):
        # the board answered: aa 55 00 00 0d 7e 50  -> payload[5] = 0x7e = 126
        capabilities = bytes.fromhex("aa5500000d7e50")
        self.assertEqual(tuple(capabilities[:2]), CAPABILITY_MAGIC)
        self.assertEqual(capabilities[5], 126)


if __name__ == "__main__":
    unittest.main()
