"""Sinowealth frame packing - no hardware needed.

The offsets here are the whole reason this board works at all; if they ever
move, the panel silently paints the wrong keys.
"""

import unittest
from unittest import mock

from rgi.backends import sinowealth
from rgi.backends.sinowealth import (
    BLOCK, B_START, FRAME_LEN, G_START, HEADER_PERKEY_1, R_START,
    INIT_2, UNLOCK, SinowealthBackend, game_commit, new_frame,
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

    def test_detect_can_describe_the_board_without_claiming_it(self):
        """Opening sends the unlock and the mode commit, which would take the
        device away from a daemon already driving it - so lamps() must work on a
        backend that was never opened."""
        backend = SinowealthBackend()
        self.assertIsNone(backend.cmd)
        self.assertIsNone(backend.data)
        lamps = backend.lamps()
        self.assertEqual(len(lamps), BLOCK)
        self.assertEqual(lamps[1].label, "1")
        self.assertEqual(lamps[1].group, "number-row")

    def test_number_row_is_the_indicator_pool(self):
        lamps = [
            type("L", (), {"index": i, "label": "", "group": "number-row" if i < 13 else "u"})()
            for i in range(BLOCK)
        ]
        pool = SinowealthBackend.default_lanes.__get__(  # type: ignore[attr-defined]
            type("B", (), {"lamps": lambda self: lamps})()) (12)
        self.assertEqual(pool, list(range(12)))


class FakeDevice:
    """A hid.device() that captures feature reports instead of sending them."""

    def __init__(self):
        self.sent: list[bytes] = []

    def open_path(self, path):
        self.path = path

    def set_nonblocking(self, flag):
        pass

    def send_feature_report(self, payload):
        self.sent.append(bytes(payload))
        return len(payload)

    def close(self):
        pass


class FakeHid:
    def __init__(self):
        self.devices: list[FakeDevice] = []

    def device(self):
        device = FakeDevice()
        self.devices.append(device)
        return device


class OpenSequenceTest(unittest.TestCase):
    """open() must enter per-key mode the way the vendor tool does.

    A commit with the mode bytes but zeroed state drops the board back to its
    stock effect (measured 2026-10-01: every write accepted, keys dark), so the
    exact vendor bytes are asserted here, not just their length.
    """

    def open(self):
        fake = FakeHid()
        backend = SinowealthBackend()
        with mock.patch.object(SinowealthBackend, "find_paths",
                               return_value=("cmd-path", "data-path")), \
             mock.patch.object(sinowealth, "_hid", return_value=fake):
            backend.open()
        return backend, fake

    def test_open_unlocks_then_enters_per_key_mode(self):
        _, fake = self.open()
        cmd_sent = fake.devices[0].sent
        data_sent = fake.devices[1].sent
        self.assertEqual(cmd_sent, [UNLOCK, INIT_2])
        self.assertEqual(len(data_sent), 1)
        self.assertEqual(data_sent[0], game_commit())

    def test_the_game_commit_carries_the_mode_and_the_magic(self):
        commit = game_commit()
        self.assertEqual(len(commit), FRAME_LEN)
        self.assertEqual(commit[:8], bytes([0x06, 0x03, 0xB6,
                                            0x00, 0x00, 0x00, 0x00, 0x00]))
        self.assertEqual(commit[14:16], bytes([0x5A, 0xA5]))
        self.assertEqual(commit[21], 0x15)          # per-key, not stock (0x00)

    def test_a_plain_zero_commit_is_not_what_open_sends(self):
        _, fake = self.open()
        commit = fake.devices[1].sent[0]
        self.assertNotEqual(commit, bytes(FRAME_LEN)[:8] + bytes(FRAME_LEN - 8))


if __name__ == "__main__":
    unittest.main()
