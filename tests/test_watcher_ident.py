"""The machine names its lanes: where that name comes from, and that it is used.

Every lane a watcher claims gets the same name - the machine's - so a laptop's
sessions are not labelled by whichever harness happens to run them. A human can
name a machine with one file, and an agent the human named overrides it later.
"""

from __future__ import annotations

import os
import socket
import tempfile
import unittest
from unittest import mock

from rgi.watchers import opencode

NO_IDENT_ENV = {k: v for k, v in os.environ.items() if k != "RGI_IDENT"}


class ResolveIdentTest(unittest.TestCase):
    def test_explicit_beats_everything(self):
        with mock.patch.dict(os.environ, {"RGI_IDENT": "env"}, clear=False):
            self.assertEqual(opencode.resolve_ident("explicit"), "explicit")

    def test_the_environment_is_second(self):
        with mock.patch.dict(os.environ, {"RGI_IDENT": "env"}, clear=False):
            self.assertEqual(opencode.resolve_ident(None), "env")

    def test_the_file_beats_the_hostname_and_loses_to_the_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "name")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("brrr\n")
            with mock.patch.object(opencode, "NAME_PATH", path):
                with mock.patch.dict(os.environ, NO_IDENT_ENV, clear=True):
                    self.assertEqual(opencode.resolve_ident(None), "brrr")
                with mock.patch.dict(os.environ, {"RGI_IDENT": "env"}, clear=False):
                    self.assertEqual(opencode.resolve_ident(None), "env")

    def test_the_hostname_is_the_default_in_its_short_form(self):
        missing = os.path.join(tempfile.gettempdir(), "rgi-name-that-is-not-there")
        with mock.patch.object(opencode, "NAME_PATH", missing):
            with mock.patch.dict(os.environ, NO_IDENT_ENV, clear=True):
                with mock.patch.object(opencode.socket, "gethostname",
                                       return_value="long.example.com"):
                    self.assertEqual(opencode.resolve_ident(None), "long")

    def test_a_blank_name_is_ignored_at_every_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "name")
            open(path, "w").close()
            with mock.patch.object(opencode, "NAME_PATH", path):
                with mock.patch.dict(os.environ, {"RGI_IDENT": "   "}, clear=False):
                    with mock.patch.object(opencode.socket, "gethostname",
                                           return_value="host"):
                        self.assertEqual(opencode.resolve_ident("  "), "host")

    def test_the_watcher_takes_its_name_from_the_same_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "name")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("brrr")
            with mock.patch.object(opencode, "NAME_PATH", path):
                with mock.patch.dict(os.environ, NO_IDENT_ENV, clear=True):
                    watcher = opencode.Watcher(url="http://127.0.0.1:1")
                    self.assertEqual(watcher.ident, "brrr")
                    self.assertEqual(watcher.host, socket.gethostname())


if __name__ == "__main__":
    unittest.main()
