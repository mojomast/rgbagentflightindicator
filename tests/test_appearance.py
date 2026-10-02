"""Appearance rendering: the defaults must reproduce the old hardcoded panel,
and the config-driven vocabulary must stay simple enough to mirror in JS."""

import json
import os
import unittest

from rgi.appearance import Appearance, StateSpec
from rgi.webconfig import default_config

FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "rgi", "webui", "tests", "fixtures", "effects.json")


class DefaultsTest(unittest.TestCase):
    def setUp(self):
        self.app = Appearance.default()

    def test_working_is_green_and_steady(self):
        self.assertEqual(self.app.render("working", 0.0), (0, 255, 0))
        self.assertEqual(self.app.render("working", 1.7), (0, 255, 0))

    def test_done_blinks_for_ten_cycles_then_holds_white(self):
        self.assertEqual(self.app.render("done", 0.0), (255, 255, 255))
        self.assertEqual(self.app.render("done", 0.28), (0, 0, 0))
        self.assertEqual(self.app.render("done", 0.56), (255, 255, 255))
        # 10 cycles x 0.56 s = 5.6 s, after which it holds
        self.assertEqual(self.app.render("done", 5.7), (255, 255, 255))

    def test_blocked_blinks_forever(self):
        self.assertEqual(self.app.render("blocked", 0.0), (255, 0, 0))
        self.assertEqual(self.app.render("blocked", 0.28), (0, 0, 0))
        seen = {self.app.render("blocked", 100.0 + step * 0.07) for step in range(9)}
        self.assertIn((255, 0, 0), seen)
        self.assertIn((0, 0, 0), seen)

    def test_idle_is_dim_and_off_is_black(self):
        self.assertEqual(self.app.render("idle", 0.0), (40, 40, 40))
        self.assertEqual(self.app.render("off", 0.0), (0, 0, 0))

    def test_quiet_holds_the_steady_colour(self):
        self.assertEqual(self.app.render("blocked", 0.28, quiet=True), (255, 0, 0))
        self.assertEqual(self.app.render("done", 0.28, quiet=True), (255, 255, 255))

    def test_unknown_state_falls_back_to_idle(self):
        self.assertEqual(self.app.render("stopping", 0.0), (40, 40, 40))


class ConfigTest(unittest.TestCase):
    def test_custom_colour_pattern_and_brightness(self):
        config = default_config()
        config["appearance"]["states"]["working"] = {
            "color": "#004400", "pattern": "steady", "brightness": 128}
        app = Appearance.from_config(config)
        # 0x44 * 128/255 = 34.2 -> round-half-up 34
        self.assertEqual(app.render("working", 0.0), (0, 34, 0))

    def test_then_off_ends_dark(self):
        config = default_config()
        config["appearance"]["states"]["done"] = {
            "color": "#ffffff", "pattern": "blink", "period_ms": 400,
            "duty": 0.5, "cycles": 2, "then": "off", "brightness": 255}
        app = Appearance.from_config(config)
        self.assertEqual(app.render("done", 0.0), (255, 255, 255))
        self.assertEqual(app.render("done", 0.9), (0, 0, 0))

    def test_breathe_moves_between_dark_and_full(self):
        config = default_config()
        config["appearance"]["states"]["idle"] = {
            "color": "#ffffff", "pattern": "breathe", "period_ms": 1000,
            "duty": 0.5, "brightness": 255}
        app = Appearance.from_config(config)
        self.assertEqual(app.render("idle", 0.0)[0], 1)
        self.assertGreater(app.render("idle", 0.5)[0], 200)

    def test_lamp_overrides_are_by_device_and_lamp(self):
        config = default_config()
        config["lamp_overrides"] = [
            {"device": "dummy", "lamp": 3, "mode": "static", "color": "#00aaff"}]
        app = Appearance.from_config(config)
        self.assertEqual(app.lamp_override("dummy", 3), (0, 170, 255))
        self.assertIsNone(app.lamp_override("other", 3))

    def test_broken_values_fall_back_instead_of_raising(self):
        config = default_config()
        config["appearance"]["blink"] = {"period_ms": "soon", "duty": "high"}
        config["appearance"]["states"]["working"] = {"color": "nope"}
        app = Appearance.from_config(config)
        self.assertEqual(app.render("working", 0.0), (0, 0, 0))
        self.assertEqual(app.render("done", 0.28), (0, 0, 0))       # default period/duty
        self.assertEqual(app.render("done", 0.56), (255, 255, 255))

    def test_out_of_range_numbers_clamp(self):
        config = default_config()
        config["appearance"]["states"]["idle"] = {
            "color": "#ffffff", "pattern": "steady", "brightness": 999}
        app = Appearance.from_config(config)
        self.assertEqual(app.render("idle", 0.0), (255, 255, 255))


class FixtureTest(unittest.TestCase):
    """The shared fixture must exist and match Python; the JS mirror reads it too."""

    def test_shared_effects_fixture_matches(self):
        if not os.path.exists(FIXTURE):
            self.skipTest("fixture not generated yet")
        with open(FIXTURE, encoding="utf-8") as fh:
            groups = json.load(fh)
        self.assertTrue(groups)
        for group in groups:
            app = Appearance.from_config(group["config"])
            for case in group["cases"]:
                got = list(app.render(case["state"], case["elapsed"], case["quiet"]))
                self.assertEqual(got, case["expect"],
                                 f"{case['name']}: {got} != {case['expect']}")


if __name__ == "__main__":
    unittest.main()
