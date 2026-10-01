"""Structural checks for the Pi coding agent extension.

The extension (plugin/pi/extension.ts) runs inside Pi's jiti-based extension
host. These tests never load Pi, never execute the extension and never touch
the network: they read the source, assert the interface that was researched
against @earendil-works/pi-coding-agent 0.99.2, and - only when a Node runtime
is on PATH - ask Node to parse the file with `node --check`. Each test method
names the check it performs, so the unittest output is the list of checks that
ran.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTENSION = ROOT / "plugin" / "pi" / "extension.ts"

# The researched interface (0.99.2): agent_start begins a run; agent_settled is
# the final settlement - agent_end may be followed by retries, compaction or
# queued work, so it must not mark completion; ui_prompt_start/ui_prompt_end
# bracket a blocking confirm/select dialog, the only approval mechanism the
# released API has; session_start/session_shutdown are the lifecycle boundaries
# cleanup hangs off.
EVENTS = (
    "agent_start",
    "agent_settled",
    "ui_prompt_start",
    "ui_prompt_end",
    "session_start",
    "session_shutdown",
)

# Credential shapes the scanner and review both care about. A real token in a
# header is refused separately below.
TOKEN_LITERAL = re.compile(
    r"(?:sk|pk|rk)-[A-Za-z0-9_-]{12,}"     # provider-style keys
    r"|\bgh[pousr]_[A-Za-z0-9]{16,}"       # GitHub tokens
    r"|\b(?:AKIA|ASIA)[A-Z0-9]{12,}"       # AWS keys
    r"|\b[a-f0-9]{32,}\b",                 # bare hex secrets
    re.IGNORECASE,
)
HEADER_LITERAL = re.compile(r"X-LED-Token[^\n]*[:=]\s*[\"'][^\"']+[\"']")


class TestPiExtension(unittest.TestCase):
    def source(self) -> str:
        self.assertTrue(
            EXTENSION.is_file(),
            f"{EXTENSION.relative_to(ROOT)} does not exist",
        )
        return EXTENSION.read_text(encoding="utf-8")

    def test_extension_file_exists_and_exports_a_factory(self):
        """check: the file exists and default-exports a Pi extension factory."""
        source = self.source()
        self.assertGreater(len(source), 0, "the extension is empty")
        self.assertIn("export default function", source)
        self.assertIn("ExtensionAPI", source)

    def test_names_the_verified_package_and_version(self):
        """check: the released package and the verified version are recorded."""
        source = self.source()
        self.assertIn("@earendil-works/pi-coding-agent", source)
        self.assertIn("0.99.2", source)

    def test_registers_each_researched_event_exactly_once(self):
        """check: every event is registered, and no event is registered twice."""
        source = self.source()
        for event in EVENTS:
            with self.subTest(event=event):
                self.assertEqual(
                    source.count(f'pi.on("{event}"'),
                    1,
                    f"{event} must be registered exactly once",
                )
        self.assertEqual(
            source.count("pi.on("),
            len(EVENTS),
            "every pi.on() call site must be one of the researched events",
        )

    def test_does_not_report_completion_on_agent_end(self):
        """check: agent_end is not registered or used to mark a lane done."""
        source = self.source()
        self.assertNotIn('pi.on("agent_end"', source)
        self.assertIn('pi.on("agent_settled"', source)

    def test_registers_a_dispose_cleanup_path(self):
        """check: a dispose function removes every listener and is returned."""
        source = self.source()
        self.assertRegex(source, r"function dispose\(")
        self.assertIn("return dispose;", source)
        self.assertIn('pi.on("session_shutdown"', source)
        self.assertIn("unsubscribers.splice(0)", source)
        self.assertIn("clearInterval(", source)

    def test_intervals_are_cleared(self):
        """check: setInterval calls and clearInterval calls are balanced."""
        source = self.source()
        started = source.count("setInterval(")
        cleared = source.count("clearInterval(")
        self.assertGreaterEqual(started, 1, "expected the liveness heartbeat interval")
        self.assertEqual(
            started,
            cleared,
            f"{started} setInterval call(s) but {cleared} clearInterval call(s)",
        )

    def test_panel_timeout_is_bounded(self):
        """check: every fetch uses a bounded AbortSignal.timeout."""
        source = self.source()
        self.assertIn("AbortSignal.timeout(TIMEOUT_MS)", source)
        match = re.search(r"TIMEOUT_MS\s*=\s*(\d+)", source)
        self.assertIsNotNone(match, "no numeric TIMEOUT_MS bound declared")
        bound = int(match.group(1))
        self.assertGreater(bound, 0)
        self.assertLessEqual(bound, 5000, "the panel timeout must stay short")

    def test_never_contains_a_literal_token(self):
        """check: the token is read from the environment or config, never inlined."""
        source = self.source()
        self.assertIn("RGI_TOKEN", source)
        self.assertIn('"token"', source)
        self.assertIsNone(TOKEN_LITERAL.search(source), "a token-like literal is inlined")
        self.assertIsNone(HEADER_LITERAL.search(source), "a literal is sent as X-LED-Token")

    @unittest.skipUnless(shutil.which("node"), "node is not on PATH")
    def test_parses_with_node_check(self):
        """check: subprocess `node --check` parses the TypeScript file."""
        result = subprocess.run(
            [shutil.which("node"), "--check", str(EXTENSION)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, f"node --check failed:\n{result.stderr}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
