"""Where the client finds the panel: --url, then the environment, then a file.

The file exists because a running OpenCode cannot acquire a new environment
variable: exporting RGI_URL after the fact leaves every already-running process
pointing at localhost.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from rgi import cli

NO_URL_ENV = {k: v for k, v in os.environ.items() if k not in ("RGI_URL", "LEDD_URL")}


class ResolveUrlTest(unittest.TestCase):
    def test_explicit_beats_everything_and_loses_its_trailing_slash(self):
        with mock.patch.dict(os.environ, {"RGI_URL": "http://env:1"}, clear=False):
            self.assertEqual(cli.resolve_url("http://explicit:2/"), "http://explicit:2")

    def test_the_environment_is_second(self):
        with mock.patch.dict(os.environ, {"RGI_URL": "http://env:1/"}, clear=False):
            self.assertEqual(cli.resolve_url(None), "http://env:1")

    def test_an_old_install_may_still_be_using_ledd_url(self):
        with mock.patch.dict(os.environ, NO_URL_ENV, clear=True):
            os.environ["LEDD_URL"] = "http://old:3"
            self.assertEqual(cli.resolve_url(None), "http://old:3")

    def test_the_file_beats_the_default_and_loses_to_the_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "url")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("http://file:4/\n")
            with mock.patch.object(cli, "URL_FILE", path):
                with mock.patch.dict(os.environ, NO_URL_ENV, clear=True):
                    self.assertEqual(cli.resolve_url(None), "http://file:4")
                with mock.patch.dict(os.environ, {"RGI_URL": "http://env:1"}, clear=False):
                    self.assertEqual(cli.resolve_url(None), "http://env:1")

    def test_an_empty_file_is_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "url")
            open(path, "w").close()
            with mock.patch.object(cli, "URL_FILE", path):
                with mock.patch.dict(os.environ, NO_URL_ENV, clear=True):
                    self.assertEqual(cli.resolve_url(None), cli.DEFAULT_URL)

    def test_a_missing_file_is_no_file(self):
        missing = os.path.join(tempfile.gettempdir(), "rgi-url-that-is-not-there")
        with mock.patch.object(cli, "URL_FILE", missing):
            with mock.patch.dict(os.environ, NO_URL_ENV, clear=True):
                self.assertEqual(cli.resolve_url(None), cli.DEFAULT_URL)


if __name__ == "__main__":
    unittest.main()
