"""While the human types, the panel writes nothing at all.

Some firmware drops keypresses while it is busy repainting. The old quiet mode
only stopped the blink animation: a lane landing during a typing burst still
triggered an immediate write, which is exactly when the dropped keys were
noticed. These tests hold the stronger rule - a typing burst means silence, and
everything that changed in it goes out as one frame when typing pauses.

``ms_since_input`` is mocked here; on the machine it is Windows-only.
"""

import unittest
from unittest import mock

from rgi.backends.dummy import DummyBackend
from rgi.daemon import Daemon, Device, Lanes

QUIET_MS = 1500
TYPING = 10.0        # a keystroke landed 10 ms ago
IDLE = 60_000.0      # nothing for a minute


class QuietCase(unittest.TestCase):
    def setUp(self):
        self.backend = DummyBackend(count=6)
        self.backend.open()
        self.lanes = Lanes(count=3)
        self.device = Device(self.backend, pool=[0, 1, 2], label="dummy")
        self.daemon = Daemon([self.device], self.lanes, quiet=True,
                             quiet_ms=QUIET_MS)

    def tick(self, ms_since_input):
        with mock.patch("rgi.daemon.ms_since_input", return_value=ms_since_input):
            self.daemon.tick()

    def land(self, state="done"):
        """Claim a lane, start it working, then land it - all while typing."""
        self.lanes.claim("s", "opencode", None, None)
        self.lanes.set_state("s", "working")
        self.tick(TYPING)
        self.lanes.set_state("s", state)
        self.tick(TYPING)


class TestHoldWhileTyping(QuietCase):
    def test_a_lane_landing_mid_typing_writes_nothing(self):
        self.tick(TYPING)
        self.land("done")
        self.assertEqual(self.backend.frames, [])

    def test_the_latest_state_paints_once_when_typing_stops(self):
        self.tick(TYPING)
        self.land("done")
        self.tick(IDLE)
        self.assertEqual(len(self.backend.frames), 1)
        # `done` blinks; the one frame is one half of that blink
        self.assertIn(self.backend.frames[0][0], ((255, 255, 255), (0, 0, 0)))

    def test_many_changes_in_one_burst_coalesce_into_one_write(self):
        self.tick(TYPING)
        self.lanes.claim("s", "opencode", None, None)
        for state in ("working", "done", "working", "blocked"):
            self.lanes.set_state("s", state)
            self.tick(TYPING)
        self.assertEqual(self.backend.frames, [])
        self.tick(IDLE)
        self.assertEqual(len(self.backend.frames), 1)
        # a blocked lane blinks red; whichever half, only red or off is allowed
        self.assertIn(self.backend.frames[0][0], ((255, 0, 0), (0, 0, 0)))

    def test_input_arriving_between_render_and_write_holds_the_frame(self):
        # The first check says idle, the check right before the write says a
        # keystroke just arrived: the write must be dropped, not raced.
        with mock.patch("rgi.daemon.ms_since_input",
                        side_effect=[IDLE, TYPING]):
            self.daemon.tick()
        self.assertEqual(self.backend.frames, [])


class TestWithoutQuiet(QuietCase):
    def test_frames_paint_immediately_when_no_one_is_typing(self):
        self.tick(IDLE)
        self.assertEqual(len(self.backend.frames), 1)   # initial all-off frame
        self.lanes.claim("s", "opencode", None, None)
        self.lanes.set_state("s", "working")
        self.tick(IDLE)
        self.assertEqual(len(self.backend.frames), 2)
        self.assertEqual(self.backend.frames[-1][0], (0, 255, 0))

    def test_quiet_off_writes_during_a_typing_burst(self):
        self.daemon.quiet = False
        self.land("done")
        self.assertGreaterEqual(len(self.backend.frames), 1)


if __name__ == "__main__":
    unittest.main()
