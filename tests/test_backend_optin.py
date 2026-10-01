"""Auto-detection never opens a backend that must be asked for by name.

The rule exists because of what happens when it does not: the sinowealth
driver's frames are *accepted* by a 258A:0049 board that exposes only the FF00
vendor page, so nothing looks wrong - the keys stay dark, and sustained writes
were measured to drop keystrokes (2026-10-01). A status light is not allowed to
cost you typing, and "the board is plugged in" is not consent to be driven by a
protocol nobody has confirmed on it.
"""

from __future__ import annotations

import unittest
from unittest import mock

from rgi import backends
from rgi.backends import OPT_IN, auto_backends, opt_in_backends
from rgi.cli import pick_backend


class OptInTest(unittest.TestCase):
    def present(self, *names):
        """Pretend exactly these backends are plugged in."""
        return mock.patch.object(backends, "available_backends",
                                 return_value=[(name, name in names)
                                               for name in
                                               ("dummy", "openrgb", "sinowealth",
                                                "evision", "wled", "sysfs")])

    def test_sinowealth_is_opt_in(self):
        self.assertIn("sinowealth", OPT_IN)

    def test_a_detected_opt_in_backend_is_not_auto_opened(self):
        with self.present("sinowealth", "evision"):
            self.assertNotIn("sinowealth", auto_backends())
            self.assertIn("evision", auto_backends())
            self.assertEqual(opt_in_backends(), ["sinowealth"])

    def test_the_dummy_backend_is_never_auto_opened(self):
        with self.present("dummy"):
            self.assertEqual(auto_backends(), [])

    def test_nothing_available_still_resolves_to_the_dummy(self):
        with self.present():
            self.assertEqual(pick_backend(None), "dummy")

    def test_auto_picks_a_real_keyboard_when_one_is_detected(self):
        with self.present("sinowealth"):
            # the only board present is opt-in, so auto must not fall back to it
            self.assertEqual(pick_backend(None), "dummy")
        with self.present("evision", "sinowealth"):
            self.assertEqual(pick_backend("auto"), "evision")

    def test_an_explicit_name_is_always_honoured(self):
        with self.present("sinowealth"):
            self.assertEqual(pick_backend("sinowealth"), "sinowealth")
            self.assertEqual(pick_backend(["sinowealth"]), "sinowealth")

    def test_detect_marks_an_opt_in_backend_rather_than_hiding_it(self):
        import argparse
        import io
        from contextlib import redirect_stdout

        from rgi.cli import cmd_detect

        with self.present("sinowealth"):
            out = io.StringIO()
            with redirect_stdout(out):
                cmd_detect(argparse.Namespace(backend=None))
            text = out.getvalue()
        self.assertIn("sinowealth", text)
        self.assertIn("YES*", text)
        self.assertIn("--backend", text)


class RegistryTest(unittest.TestCase):
    def test_the_registry_still_lists_the_opt_in_backend(self):
        """Opt-in is about automatic opening, not about existence."""
        names = [name for name, _ in backends.available_backends()]
        self.assertIn("sinowealth", names)


if __name__ == "__main__":
    unittest.main()
