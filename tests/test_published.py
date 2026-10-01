"""The files the panel publishes, and the promise the watcher file makes.

The watcher is served as one runnable file so a machine with nothing installed
can still report tokens, context, subagents and running shells. That only works
while it imports nothing outside the standard library, which is what this pins
down: the day someone adds a dependency, this test says so instead of a remote
machine failing to start.
"""

from __future__ import annotations

import unittest

from rgi import files

# every import the published watcher is allowed to make
STDLIB_ONLY = {
    "__future__", "argparse", "ctypes", "json", "os", "shutil", "socket",
    "subprocess", "time", "urllib",
}


class PublishedWatcherTest(unittest.TestCase):
    def watcher(self) -> str:
        text = files.read_published("rgi-watch.py")
        self.assertIsNotNone(text, "rgi-watch.py is not published")
        return text or ""

    def test_it_is_published(self):
        entry = [f for f in files.published_files() if f["name"] == "rgi-watch.py"]
        self.assertEqual(len(entry), 1)
        self.assertNotIn("error", entry[0])
        self.assertGreater(entry[0].get("bytes", 0), 1000)

    def test_it_can_be_run_as_a_script(self):
        text = self.watcher()
        self.assertIn("def main(", text)
        self.assertIn('if __name__ == "__main__":', text)

    def test_it_imports_nothing_but_the_standard_library(self):
        for line in self.watcher().splitlines():
            stripped = line.strip()
            if stripped.startswith("import "):
                root = stripped.split()[1].split(".")[0].rstrip(",")
            elif stripped.startswith("from ") and " import " in stripped:
                root = stripped.split()[1].split(".")[0]
            else:
                continue
            self.assertIn(root, STDLIB_ONLY,
                          f"the published watcher must stay stdlib-only: {stripped}")

    def test_it_resolves_the_address_and_token_like_the_plugin(self):
        text = self.watcher()
        self.assertIn("RGI_URL", text)
        self.assertIn("LEDD_URL", text)
        self.assertIn('"url"', text)              # ~/.config/rgi/url is read
        self.assertIn("RGI_TOKEN", text)
        self.assertIn('"token"', text)            # ~/.config/rgi/token is read


if __name__ == "__main__":
    unittest.main()
