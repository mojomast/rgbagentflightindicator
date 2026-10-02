"""Config file behaviour: defaults, merging, atomic writes, validation.

The config is the only place the web UI persists anything, so the rules that
matter are: a broken or missing file never takes the panel down, unknown keys
survive a round trip, and a write is all-or-nothing.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from rgi import webconfig as wc


class DefaultsTest(unittest.TestCase):
    def test_defaults_are_complete(self):
        config = wc.default_config()
        for key in ("settings", "appearance", "devices", "layouts", "lanes",
                    "lamp_overrides", "agents", "endpoints"):
            self.assertIn(key, config)
        for state in wc.STATE_NAMES:
            self.assertIn(state, config["appearance"]["states"])

    def test_deep_fill_fills_missing_and_keeps_unknown(self):
        raw = {"appearance": {"states": {"working": {"color": "#123456"}}},
               "future_key": 7}
        merged = wc.deep_fill(wc.default_config(), raw)
        self.assertEqual(merged["future_key"], 7)
        self.assertEqual(merged["appearance"]["states"]["working"]["color"], "#123456")
        self.assertEqual(merged["appearance"]["states"]["done"]["pattern"], "blink")
        self.assertEqual(merged["settings"]["quiet_ms"], 1500)

    def test_deep_merge_prefers_the_patch(self):
        merged = wc.deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"b": 9}})
        self.assertEqual(merged, {"a": {"b": 9, "c": 2}})

    def test_parse_colour(self):
        self.assertEqual(wc.parse_colour("#abc"), (170, 187, 204))
        self.assertEqual(wc.parse_colour("#00FF00"), (0, 255, 0))
        self.assertIsNone(wc.parse_colour("green"))
        self.assertIsNone(wc.parse_colour("#12345"))


class LoadSaveTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "config.json")

    def test_missing_file_is_defaults(self):
        config = wc.load_config(self.path)
        self.assertEqual(config["revision"], 0)
        self.assertEqual(config["appearance"]["states"]["working"]["color"], "#00ff00")

    def test_broken_json_reads_as_defaults(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        config = wc.load_config(self.path)
        self.assertEqual(config["appearance"]["states"]["working"]["color"], "#00ff00")

    def test_legacy_lanes_are_imported_once(self):
        legacy = os.path.join(self.dir.name, "lanes.json")
        with open(legacy, "w", encoding="utf-8") as fh:
            json.dump({"hermes-3": 5, "opencode": "bad"}, fh)
        with mock.patch.object(wc, "LEGACY_LANES_PATH", legacy):
            config = wc.load_config(self.path)
        overrides = config["lanes"]["overrides"]
        self.assertEqual(len(overrides), 1)
        self.assertEqual(overrides[0]["match"]["ident"], "hermes-3")
        self.assertEqual(overrides[0]["lane"], 5)

    def test_save_is_atomic_and_preserves_unknown_fields(self):
        existing_raw = {
            "schema_version": 1,
            "revision": 4,
            "future": {"kept": True},
            "appearance": wc.default_config()["appearance"],
        }
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(existing_raw, fh)
        config = wc.load_config(self.path)
        config["appearance"]["states"]["idle"]["color"] = "#112233"
        doc = wc.save_config(config, path=self.path, existing=existing_raw)
        self.assertEqual(doc["revision"], 5)
        with open(self.path, encoding="utf-8") as fh:
            on_disk = json.load(fh)
        self.assertTrue(on_disk["future"]["kept"])
        self.assertEqual(on_disk["appearance"]["states"]["idle"]["color"], "#112233")
        self.assertTrue(os.path.exists(self.path + ".bak.1"))
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        history = os.listdir(os.path.join(self.dir.name, "history"))
        self.assertIn("0005.json", history)

    def test_a_newer_schema_is_refused_not_downgraded(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"schema_version": 99}, fh)
        with self.assertRaises(wc.ConfigTooNew):
            wc.load_raw(self.path)


class ValidateTest(unittest.TestCase):
    def setUp(self):
        self.config = wc.default_config()

    def errors(self):
        return wc.validate_config(self.config)[0]

    def warnings(self):
        return wc.validate_config(self.config)[1]

    def test_defaults_validate_without_errors(self):
        self.assertEqual(self.errors(), [])

    def test_bad_colour_is_an_error(self):
        self.config["appearance"]["states"]["working"]["color"] = "green"
        self.assertTrue(any(e["path"] == "appearance.states.working.color"
                            for e in self.errors()))

    def test_bad_port_is_an_error(self):
        self.config["settings"]["port"] = 99999
        self.assertTrue(any(e["path"] == "settings.port" for e in self.errors()))

    def test_lane_beyond_count_is_an_error(self):
        self.config["lanes"]["overrides"] = [
            {"id": "x", "match": {"ident": "x"}, "lane": 99, "enabled": True}]
        self.assertTrue(any(e["path"] == "lanes.overrides[0].lane"
                            for e in self.errors()))

    def test_duplicate_layout_lamp_is_an_error(self):
        self.config["layouts"]["l"] = {"format_version": 1, "keys": [
            {"lamp": 1, "label": "a"}, {"lamp": 1, "label": "b"}]}
        self.assertTrue(any("appears twice" in e["message"] for e in self.errors()))

    def test_device_layout_must_exist(self):
        self.config["devices"]["dummy"] = {"layout": "missing"}
        self.assertTrue(any(e["path"] == "devices.dummy.layout" for e in self.errors()))

    def test_duplicate_ids_are_errors(self):
        self.config["agents"] = [
            {"id": "a", "match": {"agent": "a"}},
            {"id": "a", "match": {"agent": "b"}}]
        self.assertTrue(any("duplicate" in e["message"] for e in self.errors()))

    def test_duplicate_match_is_only_a_warning(self):
        self.config["lanes"]["overrides"] = [
            {"id": "a", "match": {"ident": "same"}, "lane": 1, "enabled": True},
            {"id": "b", "match": {"ident": "same"}, "lane": 2, "enabled": True}]
        errors, warnings = wc.validate_config(self.config)
        self.assertEqual(errors, [])
        self.assertTrue(any("more than one" in item["message"] for item in warnings))


class CompileTest(unittest.TestCase):
    def test_disabled_rules_are_skipped_and_later_rules_win(self):
        config = wc.default_config()
        config["lanes"]["overrides"] = [
            {"id": "a", "match": {"ident": "alpha"}, "lane": 1, "enabled": True},
            {"id": "b", "match": {"agent": "beta"}, "lane": 2, "enabled": False},
            {"id": "c", "match": {"agent": "beta"}, "lane": 4, "enabled": True},
            {"id": "d", "match": {"ident": "alpha", "agent": "gamma"}, "lane": 7, "enabled": True},
        ]
        policy = wc.compile_lane_map(config)
        self.assertEqual(policy["alpha"], 7)
        self.assertEqual(policy["gamma"], 7)
        self.assertEqual(policy["beta"], 4)

    def test_lamp_overrides_parse_colours_per_device(self):
        config = wc.default_config()
        config["lamp_overrides"] = [
            {"device": "dummy", "lamp": 3, "mode": "static", "color": "#ff0000"},
            {"device": "dummy", "lamp": 4, "mode": "static", "color": "nope"},
        ]
        overrides = wc.lamp_overrides_for(config)
        self.assertEqual(overrides["dummy"][3], (255, 0, 0))
        self.assertNotIn(4, overrides["dummy"])


if __name__ == "__main__":
    unittest.main()
