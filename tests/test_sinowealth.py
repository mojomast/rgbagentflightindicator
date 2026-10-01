"""Sinowealth frame packing - no hardware needed.

The offsets here are the whole reason this board works at all; if they ever
move, the panel silently paints the wrong keys.
"""

import unittest

from rgi.backends.sinowealth import (
    BLOCK, B_START, FRAME_LEN, G_START, HEADER_PERKEY_1, R_START,
    SinowealthBackend, new_frame,
)


class TestFrames(unittest.TestCase):
    def setUp(self):
        self.backend = SinowealthBackend()          # never opened: build() is pure

    def test_planar_offsets(self):
        self.assertEqual((B_START, G_START, R_START), (29, 155, 281))
        self.assertEqual(G_START - B_START, BLOCK)
        self.assertEqual(R_START - G_START, BLOCK)

    def test_frame_is_the_documented_length(self):
        frame = new_frame(HEADER_PERKEY_1)
        self.assertEqual(len(frame), FRAME_LEN)
        self.assertEqual(bytes(frame[:8]), HEADER_PERKEY_1)

    def test_colours_land_in_the_right_planes(self):
        colours = [(0, 0, 0)] * BLOCK
        colours[1] = (10, 20, 30)                   # the "1" key
        frame = self.backend.build(colours, HEADER_PERKEY_1)
        self.assertEqual(frame[R_START + 1], 10)
        self.assertEqual(frame[G_START + 1], 20)
        self.assertEqual(frame[B_START + 1], 30)

    def test_every_slot_gets_its_own_bytes(self):
        colours = [(0, 0, 0)] * BLOCK
        for i in range(BLOCK):
            colours[i] = (i % 256, (i * 2) % 256, (i * 3) % 256)
        frame = self.backend.build(colours, HEADER_PERKEY_1)
        for i in range(BLOCK):
            self.assertEqual(frame[R_START + i], i % 256, f"slot {i}")

    def test_nothing_outside_the_planes_is_touched(self):
        colours = [(255, 255, 255)] * BLOCK
        frame = self.backend.build(colours, HEADER_PERKEY_1)
        self.assertEqual(bytes(frame[:29]), HEADER_PERKEY_1 + bytes(21))
        self.assertEqual(bytes(frame[407:]), bytes(FRAME_LEN - 407))

    def test_number_row_is_the_indicator_pool(self):
        lamps = [
            type("L", (), {"index": i, "label": "", "group": "number-row" if i < 13 else "u"})()
            for i in range(BLOCK)
        ]
        pool = SinowealthBackend.default_lanes.__get__(  # type: ignore[attr-defined]
            type("B", (), {"lamps": lambda self: lamps})()) (12)
        self.assertEqual(pool, list(range(12)))


if __name__ == "__main__":
    unittest.main()
