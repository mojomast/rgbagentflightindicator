"""Auto-detection never opens a backend that must be asked for by name.

The rule exists because of what happens when it does not: in October 2026 the
sinowealth driver's mode commit was a zero-filled frame, which the firmware
accepted and then ignored - the keys stayed dark, and sustained writes were
measured to drop keystrokes. A status light is not allowed to cost you typing,
and "the board is plugged in" is not consent to be driven by an unconfirmed
protocol.

The machinery is tested with a stand-in, so it holds for whatever backend needs
it next. Sinowealth itself graduated back to auto once the vendor's game commit
was recovered and verified on hardware; `test_sinowealth.py` holds that sequence.
"""

from __future__ import annotations

import unittest
from unittest import mock

from rgi import backends
from rgi.backends import OPT_IN, auto_backends, opt_in_backends
from rgi.cli import pick_backend

PRESENT = ("dummy", "openrgb", "sinowealth", "evision", "wled", "sysfs")


class OptInMechanismTest(unittest.TestCase):
    def present(self, *names, opt_in=frozenset({"sinowealth"})):
        """Pretend exactly these backends are plugged in, with this opt-in set."""
        available = mock.patch.object(
            backends, "available_backends",
            return_value=[(name, name in names) for name in PRESENT])
        # cli.py binds OPT_IN at import, so both references are patched.
        optin = mock.patch.object(backends, "OPT_IN", opt_in)
        clioptin = mock.patch("rgi.cli.OPT_IN", opt_in)
        return available, optin, clioptin

    def test_an_opt_in_backend_is_listed_but_not_auto_opened(self):
        available, optin, clioptin = self.present("sinowealth", "evision")
        with available, optin, clioptin:
            self.assertNotIn("sinowealth", auto_backends())
            self.assertIn("evision", auto_backends())
            self.assertEqual(opt_in_backends(), ["sinowealth"])

    def test_the_dummy_backend_is_never_auto_opened(self):
        available, optin, clioptin = self.present("dummy")
        with available, optin, clioptin:
            self.assertEqual(auto_backends(), [])

    def test_nothing_available_still_resolves_to_the_dummy(self):
        available, optin, clioptin = self.present()
        with available, optin, clioptin:
            self.assertEqual(pick_backend(None), "dummy")

    def test_auto_never_falls_back_to_an_opt_in_backend(self):
        available, optin, clioptin = self.present("sinowealth")
        with available, optin, clioptin:
            # the only board present is opt-in, so auto must not take it
            self.assertEqual(pick_backend(None), "dummy")
        available, optin, clioptin = self.present("evision", "sinowealth")
        with available, optin, clioptin:
            self.assertEqual(pick_backend("auto"), "evision")

    def test_an_explicit_name_is_always_honoured(self):
        available, optin, clioptin = self.present("sinowealth")
        with available, optin, clioptin:
            self.assertEqual(pick_backend("sinowealth"), "sinowealth")
            self.assertEqual(pick_backend(["sinowealth"]), "sinowealth")

    def test_detect_marks_an_opt_in_backend_rather_than_hiding_it(self):
        import argparse
        import io
        from contextlib import redirect_stdout

        from rgi.cli import cmd_detect

        available, optin, clioptin = self.present("sinowealth")
        with available, optin, clioptin:
            out = io.StringIO()
            with redirect_stdout(out):
                cmd_detect(argparse.Namespace(backend=None))
            text = out.getvalue()
        self.assertIn("sinowealth", text)
        self.assertIn("YES*", text)
        self.assertIn("--backend", text)


class GraduatedTest(unittest.TestCase):
    def test_sinowealth_is_no_longer_opt_in(self):
        """The game commit is recovered and verified; auto may open it again."""
        self.assertNotIn("sinowealth", OPT_IN)

    def test_the_registry_still_lists_every_backend(self):
        names = [name for name, _ in backends.available_backends()]
        self.assertIn("sinowealth", names)


if __name__ == "__main__":
    unittest.main()
