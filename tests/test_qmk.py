"""QMK packet construction and VialRGB LED-map labelling - no hardware needed.

There is no QMK keyboard attached to the machine this was written on, so these
tests are deliberately about the bytes: framing, HSV conversion, chunking, the
exact VIA/VialRGB payloads, and the pure labelling of a synthetic LED map. What
they cannot prove is that a real board accepts any of it - that needs hardware,
and the docs say so.
"""

import unittest
from unittest import mock

from rgi.backends import qmk
from rgi.backends.base import Lamp
from rgi.backends.qmk import (
    LED_FLAG_INDICATOR, LED_FLAG_UNDERGLOW, RGB_MATRIX_SOLID_COLOR, VIA_GET,
    VIA_LIGHTING_CHANNEL_RGB_MATRIX, VIA_SET, VIALRGB_GET_INFO,
    VIALRGB_GET_LED_INFO, VIALRGB_GET_NUMBER_LEDS, VIALRGB_GET_SUPPORTED,
    VIALRGB_SET_MODE, QmkBackend, lamps_from_led_map, rgb_to_hsv_qmk,
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


class FakeBoardTransport:
    """A raw HID transport that speaks Vial/VialRGB from a synthetic LED map.

    `entries` is one `(x, y, flags, row, col)` tuple per LED, with None where
    the board should not answer. `map_answers=False` models firmware without
    LED-map support; `vialrgb=False` models stock VIA firmware.
    """

    def __init__(self, entries=(), vialrgb=True, count=None, map_answers=True):
        self.entries = list(entries)
        self.vialrgb = vialrgb
        self.count = len(self.entries) if count is None else count
        self.map_answers = map_answers
        self.sent: list[list[int]] = []

    def request(self, payload, timeout=0.6):
        self.sent.append(list(payload))
        body = list(payload)
        command = body[0]
        sub = body[1] if len(body) > 1 else None

        if command == VIA_GET and sub == VIALRGB_GET_INFO:
            if not self.vialrgb:
                return None
            return bytes([VIA_GET, VIALRGB_GET_INFO, 0x01, 0x00, 0xFF])
        if command == VIA_GET and sub == VIALRGB_GET_SUPPORTED:
            # effect id 1 (direct) in the supported list
            return bytes([VIA_GET, VIALRGB_GET_SUPPORTED, 0x01, 0x00] + [0xFF] * 6)
        if command == VIA_GET and sub == VIALRGB_GET_NUMBER_LEDS:
            return bytes([VIA_GET, VIALRGB_GET_NUMBER_LEDS,
                          self.count & 0xFF, (self.count >> 8) & 0xFF])
        if command == VIA_GET and sub == VIALRGB_GET_LED_INFO:
            if not self.map_answers:
                return None
            index = body[2] | ((body[3] if len(body) > 3 else 0) << 8)
            if index >= len(self.entries) or self.entries[index] is None:
                return None
            return bytes([VIA_GET, VIALRGB_GET_LED_INFO, *self.entries[index]])
        if command == VIA_GET and sub == 0x03:              # VIA rgb_matrix colour
            return None if self.vialrgb else bytes([VIA_GET, 0x03, 0x04, 0x00])
        return None

    def close(self):
        pass


class FakeBoard(QmkBackend):
    """A QmkBackend whose "hardware" is a transport injected by the test."""

    @classmethod
    def candidates(cls):
        return [{"path": b"fake", "vendor_id": 0x1234, "product_id": 0x5678}]

    @staticmethod
    def _report_len(device):
        return qmk.REPORT_LEN_DEFAULT


def open_backend(transport) -> QmkBackend:
    backend = FakeBoard()
    with mock.patch.object(qmk, "RawHid", lambda path, report_len: transport):
        backend.open()
    return backend


def tkl_ish_map():
    """A number row, a few alpha keys, underglow, an indicator, and a 0xFF pair.

    "TKL-ish" only in that the top row is a full number row of 13 keys; the
    interesting shapes are the non-number-row key, the two flagged edge LEDs
    and the LED whose row/col are both 0xFF without being flagged.
    """
    entries = [(i * 16, 0, 0x04, 0, i) for i in range(13)]      # number row
    entries += [(8, 16, 0x04, 1, 0),                            # alpha row
                (40, 16, 0x04, 1, 1),
                (72, 16, 0x04, 1, 2)]
    entries += [(0, 64, LED_FLAG_UNDERGLOW, 0xFF, 0xFF),        # underglow
                (224, 64, LED_FLAG_UNDERGLOW, 0xFF, 0xFF)]
    entries += [(112, 40, LED_FLAG_INDICATOR, 0xFF, 0xFF)]      # indicator
    entries += [(160, 48, 0x04, 0xFF, 0xFF)]                    # 0xFF pair
    return entries


class TestHsv(unittest.TestCase):
    def test_qmk_hsv_scale(self):
        # hue 0-255 is 0-360 degrees: green (120 deg) is 85
        self.assertEqual(rgb_to_hsv_qmk((0, 255, 0)), (85, 255, 255))
        self.assertEqual(rgb_to_hsv_qmk((255, 0, 0)), (0, 255, 255))
        self.assertEqual(rgb_to_hsv_qmk((0, 0, 255)), (170, 255, 255))

    def test_white_has_no_saturation_and_black_no_value(self):
        self.assertEqual(rgb_to_hsv_qmk((255, 255, 255)), (0, 0, 255))
        self.assertEqual(rgb_to_hsv_qmk((0, 0, 0)), (0, 0, 0))


class TestLampsFromLedMap(unittest.TestCase):
    """The pure labelling helper: synthetic maps, no transport at all."""

    def test_number_row_gets_the_standard_legends_in_x_order(self):
        entries = tkl_ish_map()
        lamps = lamps_from_led_map(entries, len(entries))
        self.assertEqual([lamp.label for lamp in lamps[:13]],
                         ["`", "1", "2", "3", "4", "5", "6", "7", "8", "9",
                          "0", "-", "="])
        self.assertTrue(all(lamp.group == "number-row" for lamp in lamps[:13]))
        self.assertTrue(all(lamp.y == 0.0 for lamp in lamps[:13]))
        self.assertAlmostEqual(lamps[0].x, 0.0)
        self.assertAlmostEqual(lamps[1].x, 16 / 224)

    def test_legends_follow_x_order_not_index_order(self):
        entries = [(32, 10, 0x04, 0, 7), (16, 10, 0x04, 0, 8), (0, 10, 0x04, 0, 9)]
        lamps = lamps_from_led_map(entries, 3)
        self.assertEqual([lamp.label for lamp in lamps], ["2", "1", "`"])

    def test_other_matrix_keys_are_labelled_row_col(self):
        entries = tkl_ish_map()
        lamps = lamps_from_led_map(entries, len(entries))
        self.assertEqual([lamp.label for lamp in lamps[13:16]],
                         ["1:0", "1:1", "1:2"])
        self.assertTrue(all(lamp.group == "key" for lamp in lamps[13:16]))

    def test_underglow_and_indicator_are_named_by_flag(self):
        entries = tkl_ish_map()
        lamps = lamps_from_led_map(entries, len(entries))
        self.assertEqual([lamp.label for lamp in lamps[16:18]],
                         ["underglow", "underglow"])
        self.assertTrue(all(lamp.group == "underglow" for lamp in lamps[16:18]))
        self.assertEqual(lamps[18].label, "indicator")
        self.assertEqual(lamps[18].group, "indicator")

    def test_an_unflagged_led_outside_the_matrix_is_a_plain_key(self):
        entries = tkl_ish_map()
        lamps = lamps_from_led_map(entries, len(entries))
        self.assertEqual(lamps[19].label, "key")
        self.assertEqual(lamps[19].group, "key")

    def test_positions_are_normalised_keyboard_units(self):
        entries = tkl_ish_map()
        lamps = lamps_from_led_map(entries, len(entries))
        self.assertEqual((lamps[16].x, lamps[16].y), (0.0, 1.0))
        self.assertEqual((lamps[17].x, lamps[17].y), (1.0, 1.0))
        self.assertAlmostEqual(lamps[18].x, 112 / 224)
        self.assertAlmostEqual(lamps[18].y, 40 / 64)

    def test_without_matrix_positions_the_smallest_y_row_is_the_number_row(self):
        entries = [(0, 8, 0x04, 0xFF, 0xFF),
                   (20, 8, 0x04, 0xFF, 0xFF),
                   (0, 30, 0x00, 0xFF, 0xFF)]
        lamps = lamps_from_led_map(entries, 3)
        self.assertEqual([lamp.label for lamp in lamps], ["`", "1", "key"])
        self.assertEqual([lamp.group for lamp in lamps],
                         ["number-row", "number-row", "key"])

    def test_top_row_leds_past_the_legends_keep_their_matrix_name(self):
        entries = [(i * 16, 0, 0x04, 0, i) for i in range(14)]
        lamps = lamps_from_led_map(entries, 14)
        self.assertEqual(lamps[13].label, "0:13")
        self.assertEqual(lamps[13].group, "number-row")

    def test_missing_entries_fall_back_to_unmapped(self):
        entries = [(0, 0, 0x04, 0, 0)]
        lamps = lamps_from_led_map(entries, 3)
        self.assertEqual(lamps[0].label, "`")
        self.assertEqual([lamps[i].group for i in (1, 2)],
                         ["unmapped", "unmapped"])
        self.assertEqual([lamps[i].label for i in (1, 2)], ["1", "2"])

    def test_all_junk_returns_none_so_the_caller_can_fall_back(self):
        self.assertIsNone(lamps_from_led_map([None, None], 2))
        self.assertIsNone(lamps_from_led_map([(255, 255, 0, 0, 0)], 1))
        self.assertIsNone(lamps_from_led_map([], 3))

    def test_an_unreadable_led_falls_back_alone(self):
        entries = [(0, 0, 0x04, 0, 0), None, (32, 0, 0x04, 0, 1)]
        lamps = lamps_from_led_map(entries, 3)
        self.assertEqual(lamps[1], Lamp(index=1, label="1", group="unmapped"))
        self.assertEqual([lamps[0].label, lamps[2].label], ["`", "1"])


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


class TestVialRgbLedMap(unittest.TestCase):
    """open() consumes the VialRGB LED map through a fake transport."""

    def test_open_labels_every_led_from_the_map(self):
        transport = FakeBoardTransport(tkl_ish_map())
        backend = open_backend(transport)
        self.assertEqual(backend.mode, "vialrgb")
        self.assertTrue(backend.per_lamp)
        self.assertEqual(len(backend.lamps()), len(tkl_ish_map()))
        self.assertEqual([lamp.group for lamp in backend.lamps()[:13]],
                         ["number-row"] * 13)
        labels = [lamp.label for lamp in backend.lamps()]
        self.assertEqual(labels[12], "=")
        self.assertEqual(labels[13:16], ["1:0", "1:1", "1:2"])
        self.assertEqual(labels[16:18], ["underglow", "underglow"])
        self.assertEqual(labels[18], "indicator")
        self.assertEqual(labels[19], "key")
        self.assertEqual([lamp.group for lamp in backend.lamps()[16:]],
                         ["underglow", "underglow", "indicator", "key"])

    def test_open_queries_get_led_info_once_per_led(self):
        transport = FakeBoardTransport(tkl_ish_map())
        backend = open_backend(transport)
        queries = [payload for payload in transport.sent
                   if payload[:2] == [VIA_GET, VIALRGB_GET_LED_INFO]]
        self.assertEqual(len(queries), len(backend.lamps()))
        self.assertEqual(queries[0][2:4], [0, 0])            # index 0
        self.assertEqual(queries[-1][2:4], [19, 0])          # index 19

    def test_open_enters_direct_mode_before_reading_the_map(self):
        transport = FakeBoardTransport(tkl_ish_map())
        open_backend(transport)
        mode_index = transport.sent.index([VIA_SET, VIALRGB_SET_MODE, 1, 0, 0, 0, 0, 0])
        first_map_read = next(i for i, payload in enumerate(transport.sent)
                              if payload[:2] == [VIA_GET, VIALRGB_GET_LED_INFO])
        self.assertLess(mode_index, first_map_read)

    def test_unsupported_map_falls_back_to_unmapped_labels(self):
        entries = tkl_ish_map()
        transport = FakeBoardTransport(entries, map_answers=False)
        backend = open_backend(transport)
        self.assertTrue(backend.per_lamp)
        self.assertEqual([(lamp.label, lamp.group) for lamp in backend.lamps()],
                         [(str(i), "unmapped") for i in range(len(entries))])

    def test_junk_map_replies_fall_back_to_unmapped_labels(self):
        entries = [(255, 255, 255, 255, 255)] * 3
        transport = FakeBoardTransport(entries)
        backend = open_backend(transport)
        self.assertEqual([(lamp.label, lamp.group) for lamp in backend.lamps()],
                         [("0", "unmapped"), ("1", "unmapped"), ("2", "unmapped")])

    def test_a_short_led_info_reply_is_treated_as_missing(self):
        class ShortTransport(Recorder):
            def request(self, payload, timeout=0.6):
                self.sent.append(list(payload))
                if list(payload[:2]) == [VIA_GET, VIALRGB_GET_LED_INFO]:
                    return b"\x08\x44\x00"                   # truncated
                return None

        backend = QmkBackend()
        backend.transport = ShortTransport()
        self.assertIsNone(backend._vialrgb_led_info(0))


class TestStockViaOpen(unittest.TestCase):
    """Stock VIA keeps today's one-colour, whole-board behaviour exactly."""

    def test_open_keeps_whole_board_lamps_and_never_reads_a_map(self):
        transport = FakeBoardTransport(vialrgb=False)
        backend = open_backend(transport)
        self.assertEqual(backend.mode, "via")
        self.assertFalse(backend.per_lamp)
        self.assertEqual(len(backend.lamps()), 12)
        self.assertTrue(all(lamp.label == "whole-board"
                            and lamp.group == "whole-board"
                            for lamp in backend.lamps()))
        self.assertFalse(any(payload[:2] == [VIA_GET, VIALRGB_GET_LED_INFO]
                             for payload in transport.sent))

    def test_via_open_selects_the_solid_colour_effect(self):
        transport = FakeBoardTransport(vialrgb=False)
        open_backend(transport)
        self.assertIn([VIA_SET, VIA_LIGHTING_CHANNEL_RGB_MATRIX,
                       qmk.VIA_RGB_MATRIX_EFFECT, RGB_MATRIX_SOLID_COLOR],
                      transport.sent)


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
