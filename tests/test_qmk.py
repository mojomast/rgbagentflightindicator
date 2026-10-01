"""QMK packet construction - no hardware needed.

There is no QMK keyboard attached to the machine this was written on, so these
tests are deliberately about the bytes: framing, HSV conversion, chunking, and
the exact VIA/VialRGB payloads. What they cannot prove is that a real board
accepts them - that needs hardware, and the docs say so.
"""

import unittest

from rgi.backends.base import Lamp
from rgi.backends.qmk import (
    RGB_MATRIX_SOLID_COLOR, VIA_LIGHTING_CHANNEL_RGB_MATRIX, VIA_SET, QmkBackend,
    rgb_to_hsv_qmk,
)


class Recorder:
    """Stands in for the raw HID transport and remembers what was sent."""

    def __init__(self):
        self.sent: list[list[int]] = []

    def request(self, payload, timeout=0.6):
        self.sent.append(list(payload))
        return None

    def close(self):
        pass


class TestHsv(unittest.TestCase):
    def test_qmk_hsv_scale(self):
        # hue 0-255 is 0-360 degrees: green (120 deg) is 85
        self.assertEqual(rgb_to_hsv_qmk((0, 255, 0)), (85, 255, 255))
        self.assertEqual(rgb_to_hsv_qmk((255, 0, 0)), (0, 255, 255))
        self.assertEqual(rgb_to_hsv_qmk((0, 0, 255)), (170, 255, 255))

    def test_white_has_no_saturation_and_black_no_value(self):
        self.assertEqual(rgb_to_hsv_qmk((255, 255, 255)), (0, 0, 255))
        self.assertEqual(rgb_to_hsv_qmk((0, 0, 0)), (0, 0, 0))


class QmkCase(unittest.TestCase):
    def setUp(self):
        self.backend = QmkBackend()
        self.recorder = Recorder()
        self.backend.transport = self.recorder


class TestVialRgb(QmkCase):
    """The per-key path: Vial firmware with VIALRGB_ENABLE."""

    def setUp(self):
        super().setUp()
        self.backend.mode = "vialrgb"
        self.backend.per_lamp = True
        self.backend._lamps = [Lamp(index=i, label=f"led{i}") for i in range(12)]

    def test_direct_mode_is_selected_first(self):
        self.backend._enter_vialrgb_direct()
        self.assertEqual(self.recorder.sent[0][:3], [VIA_SET, 0x41, 0x01])

    def test_colours_are_chunked_at_nine_leds(self):
        self.backend.write([(0, 255, 0)] * 12)
        self.assertEqual(len(self.recorder.sent), 2)
        self.assertEqual(self.recorder.sent[0][:5], [VIA_SET, 0x42, 0, 0, 9])
        self.assertEqual(self.recorder.sent[1][:5], [VIA_SET, 0x42, 9, 0, 3])

    def test_green_is_sent_as_qmk_hsv(self):
        self.backend.write([(0, 255, 0)] * 1)
        self.assertEqual(self.recorder.sent[0][5:8], [85, 255, 255])

    def test_off_is_value_zero_not_a_missing_led(self):
        self.backend.write([(255, 255, 255), (0, 0, 0)])
        payload = self.recorder.sent[0]
        self.assertEqual(payload[4], 2)                      # both LEDs sent
        self.assertEqual(payload[8:11], [0, 0, 0])           # the dark one

    def test_an_uneven_last_chunk_carries_the_right_count(self):
        self.backend.write([(255, 0, 0)] * 10)
        self.assertEqual(self.recorder.sent[0][4], 9)
        self.assertEqual(self.recorder.sent[1][:5], [VIA_SET, 0x42, 9, 0, 1])


class TestViaWholeBoard(QmkCase):
    """The fallback: stock QMK + VIA, one colour for the entire board."""

    def setUp(self):
        super().setUp()
        self.backend.mode = "via"
        self.backend.per_lamp = False
        self.backend._lamps = [Lamp(index=i, label="whole-board",
                                    group="whole-board") for i in range(12)]

    def test_a_colour_sets_effect_then_brightness_then_colour(self):
        self.backend.write([(0, 255, 0)] * 12)
        payloads = [p[:4] for p in self.recorder.sent]
        self.assertIn([VIA_SET, VIA_LIGHTING_CHANNEL_RGB_MATRIX, 0x02, RGB_MATRIX_SOLID_COLOR],
                      payloads)
        self.assertIn([VIA_SET, VIA_LIGHTING_CHANNEL_RGB_MATRIX, 0x01, 255], payloads)
        self.assertIn([VIA_SET, VIA_LIGHTING_CHANNEL_RGB_MATRIX, 0x04, 85], payloads)
        # effect must come before anything that relies on it
        self.assertEqual(self.recorder.sent[0][2], 0x02)

    def test_black_turns_the_board_down_rather_than_setting_a_colour(self):
        self.backend.write([(0, 0, 0)] * 12)
        self.assertEqual(self.recorder.sent[-1][:4],
                         [VIA_SET, VIA_LIGHTING_CHANNEL_RGB_MATRIX, 0x01, 0])
        self.assertEqual(len(self.recorder.sent), 1)

    def test_the_whole_board_gets_one_colour_not_twelve(self):
        self.backend.write([(0, 255, 0)] * 12)
        colours = [p for p in self.recorder.sent
                   if p[0] == VIA_SET and p[1] == VIA_LIGHTING_CHANNEL_RGB_MATRIX
                   and p[2] == 0x04]
        self.assertEqual(len(colours), 1)


class TestDetectionPayloads(unittest.TestCase):
    def test_probes_are_read_only_commands(self):
        """Detection must never be a write: no set, no save, no reset."""
        backend = QmkBackend()
        recorder = Recorder()
        backend.transport = recorder
        backend._probe_vialrgb()
        backend._vialrgb_led_count()
        backend._probe_via_lighting()
        backend._probe_signalrgb()
        for payload in recorder.sent:
            self.assertNotEqual(payload[0], VIA_SET,
                                "detection sent a write command")


if __name__ == "__main__":
    unittest.main()
