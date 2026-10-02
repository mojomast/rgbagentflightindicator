"""``rgi hook --source``: the generic shim must always exit 0.

A hook is a child process whose exit code a harness may read as a decision, so
this path must never raise, never print to stdout, and never block for long.
"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from rgi import hooks


class _Handler(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        self.seen.append({
            "path": self.path,
            "token": self.headers.get("X-LED-Token"),
            "body": json.loads(body or b"{}"),
        })
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


class HookSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        _Handler.seen = []

    def test_payload_is_posted_to_the_source_endpoint(self):
        payload = {"hook_event_name": "PreToolUse", "session_id": "s-1"}
        code = hooks.run_source("codex", url=self.base, token="secret",
                                payload=payload)
        self.assertEqual(code, 0)
        self.assertEqual(len(_Handler.seen), 1)
        self.assertEqual(_Handler.seen[0]["path"], "/hook/codex")
        self.assertEqual(_Handler.seen[0]["token"], "secret")
        self.assertEqual(_Handler.seen[0]["body"], payload)

    def test_an_unreachable_panel_still_exits_zero(self):
        code = hooks.run_source("cursor", url="http://127.0.0.1:1",
                                token="x", payload={"a": 1}, timeout=0.3)
        self.assertEqual(code, 0)

    def test_an_empty_payload_sends_nothing(self):
        code = hooks.run_source("q", url=self.base, token="t", payload={})
        self.assertEqual(code, 0)
        self.assertEqual(_Handler.seen, [])


if __name__ == "__main__":
    unittest.main()
