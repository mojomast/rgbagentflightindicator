"""Real local HTTP and stdio MCP checks; no keyboard, tunnel or network needed."""

import asyncio
import contextlib
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
import socket
import sys
import threading
import unittest
from unittest.mock import patch

from rgi.backends.dummy import DummyBackend
from rgi.daemon import Daemon, Device, Lanes, make_server
from rgi.mcp_server import PanelClient, PanelError

HAS_MCP = importlib.util.find_spec("mcp") is not None


@contextlib.contextmanager
def running_server(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class PanelCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = DummyBackend(count=3)
        cls.backend.open()
        cls.lanes = Lanes(count=2)
        device = Device(cls.backend, pool=[0, 1], label="dummy")
        cls.daemon = Daemon([device], cls.lanes, quiet=False)
        cls.server = make_server("127.0.0.1", 0, cls.daemon, "test-token")
        cls.running = running_server(cls.server)
        cls.running.__enter__()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.running.__exit__(None, None, None)
        cls.backend.close()

    def setUp(self):
        self.lanes.clear()
        self.credential = patch("rgi.mcp_server.resolve_token", return_value="test-token")
        self.credential.start()
        self.addCleanup(self.credential.stop)
        self.client = PanelClient(self.base, namespace="test")


class TestPanelClient(PanelCase):
    def test_full_lifecycle_and_idempotence(self):
        first = self.client.session_start("task-1", "Private test")
        self.assertEqual(first["slot"], 0)
        self.assertEqual(self.client.session_start("task-1", "Retry"), first)
        self.client.session_set_state("task-1", "working")
        self.assertEqual(self.client.panel_status()["sessions"][0]["state"], "working")
        self.client.session_set_state("task-1", "done")
        self.assertEqual(self.client.panel_status()["sessions"][0]["state"], "done")
        self.assertEqual(self.client.panel_status()["free"], [1])
        self.client.session_end("task-1")
        self.client.session_end("task-1")
        self.assertEqual(self.client.panel_status()["sessions"], [])
        self.assertEqual(self.client.panel_status()["free"], [0, 1])

    def test_status_does_not_expose_other_agents_or_metadata(self):
        self.lanes.claim("opencode-secret", "opencode", "Other task", "other-host")
        self.lanes.set_info("opencode-secret", {"private": "private-detail"})
        self.client.session_start("mine", "My task")
        sid = self.client.prefix + "mine"
        self.lanes.set_info(sid, {"private": "private-detail"})
        status = self.client.panel_status()
        self.assertEqual([s["session_id"] for s in status["sessions"]], ["mine"])
        self.assertNotIn("private-detail", json.dumps(status))
        self.assertNotIn("Other task", json.dumps(status))
        self.assertNotIn("other-host", json.dumps(status))

    def test_capacity_does_not_evict_blocked_or_done_lanes(self):
        self.lanes.claim("blocked", "opencode", "blocked", None)
        self.lanes.set_state("blocked", "blocked")
        self.lanes.claim("done", "opencode", "done", None)
        self.lanes.set_state("done", "done")
        with self.assertRaises(PanelError) as result:
            self.client.session_start("overflow", "Overflow")
        self.assertEqual(result.exception.code, "lane_unavailable")
        self.assertEqual(set(self.lanes.slot), {"blocked", "done"})

    def test_explicit_slot_does_not_steal(self):
        self.lanes.claim("other", "opencode", "Other", None, want=0)
        with self.assertRaises(PanelError):
            self.client.session_start("mine", "Mine", slot=0)
        self.assertEqual(set(self.lanes.slot), {"other"})
        self.assertEqual(self.client.session_start("mine", "Mine", slot=1)["slot"], 1)

    def test_repeated_claim_cannot_silently_move_a_lane(self):
        self.client.session_start("mine", "Mine", slot=0)
        with self.assertRaises(PanelError):
            self.client.session_start("mine", "Mine", slot=1)
        self.assertEqual(self.lanes.slot[self.client.prefix + "mine"], 0)

    def test_reclaim_after_daemon_restart(self):
        self.client.session_start("mine", "Mine", slot=1)
        self.lanes.clear()
        with self.assertRaises(PanelError) as result:
            self.client.session_set_state("mine", "working")
        self.assertEqual(result.exception.code, "session_not_found")
        self.client.session_start("mine", "Mine", slot=1)
        self.client.session_set_state("mine", "working")
        self.assertEqual(self.client.panel_status()["sessions"][0]["slot"], 1)

    def test_namespace_cannot_mutate_another_agents_session(self):
        self.lanes.claim("other", "opencode", "Other", None)
        self.lanes.set_state("other", "working")
        with self.assertRaises(PanelError):
            self.client.session_set_state("other", "done")
        self.client.session_end("other")
        self.assertEqual(self.lanes.state["other"], "working")

    def test_other_adapter_namespace_is_separate(self):
        self.client.session_start("same", "First")
        other = PanelClient(self.base, namespace="another")
        other.session_start("same", "Second")
        self.assertEqual(self.client.panel_status()["sessions"][0]["label"], "First")
        self.assertEqual(other.panel_status()["sessions"][0]["label"], "Second")

    def test_bad_auth_does_not_disclose_credential(self):
        with patch("rgi.mcp_server.resolve_token", return_value="private-value"):
            with self.assertRaises(PanelError) as result:
                self.client.panel_status()
        self.assertEqual(result.exception.code, "panel_auth_failed")
        self.assertNotIn("private-value", str(result.exception))

    def test_invalid_header_does_not_disclose_credential(self):
        with patch("rgi.mcp_server.resolve_token", return_value="private-value\nbad"):
            with self.assertRaises(PanelError) as result:
                self.client.panel_status()
        self.assertNotIn("private-value", str(result.exception))

    def test_rotated_credential_is_read_on_next_call(self):
        with patch("rgi.mcp_server.resolve_token", side_effect=["wrong", "test-token"]):
            with self.assertRaises(PanelError):
                self.client.panel_status()
            self.assertTrue(self.client.panel_status()["online"])

    def test_inherited_proxy_does_not_receive_local_requests(self):
        with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""}):
            client = PanelClient(self.base)
            self.assertTrue(client.panel_status()["online"])

    def test_invalid_sessions_labels_states_and_slots_are_refused(self):
        for sid in ("", "a/b", "a?x", "a\n", "x" * 97):
            with self.subTest(sid=sid), self.assertRaises(ValueError):
                self.client.session_end(sid)
        for label in ("", " ", "bad\nlabel", "x" * 81):
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.client.session_start("test", label)
        for slot in (True, -1, 256, "1"):
            with self.subTest(slot=slot), self.assertRaises(ValueError):
                self.client.session_start("test", "Test", slot=slot)
        with self.assertRaises(ValueError):
            self.client.session_set_state("test", "complete")
        self.assertEqual(self.lanes.slot, {})


class TestLocalBoundary(unittest.TestCase):
    def test_rejects_nonlocal_or_credentialed_origins(self):
        urls = ["http://example.com", "http://localhost:8730", "http://0.0.0.0:8730",
                "http://127.0.0.1:8730/path", "http://127.0.0.1:8730?query",
                "http://127.0.0.1:8730#fragment", "http://user:pass@127.0.0.1:8730",
                "http://127.0.0.1:bad", "file:///status"]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                PanelClient(url)
        PanelClient("http://[::1]:8730")

    def test_configuration_bounds(self):
        for timeout in (0, 11, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                PanelClient(timeout=timeout)
        for namespace in ("", "x:y", "x" * 33):
            with self.subTest(namespace=namespace), self.assertRaises(ValueError):
                PanelClient(namespace=namespace)

    def test_offline_write_has_unknown_outcome_and_is_not_retried(self):
        with socket.socket() as offline:
            offline.bind(("127.0.0.1", 0))
            client = PanelClient(f"http://127.0.0.1:{offline.getsockname()[1]}", timeout=0.1)
            with patch("rgi.mcp_server.resolve_token", return_value=None):
                with self.assertRaises(PanelError) as result:
                    client.session_set_state("test", "working")
        self.assertEqual(result.exception.code, "panel_unreachable")
        self.assertFalse(result.exception.retryable)
        self.assertIn("unknown", str(result.exception))

    def test_redirect_is_not_followed(self):
        destinations = []

        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == "/status":
                    self.send_response(302)
                    self.send_header("Location", "/destination")
                    self.end_headers()
                else:
                    destinations.append(self.path)
                    self.send_response(200)
                    self.end_headers()

        with running_server(ThreadingHTTPServer(("127.0.0.1", 0), Redirect)) as server:
            client = PanelClient(f"http://127.0.0.1:{server.server_address[1]}")
            with patch("rgi.mcp_server.resolve_token", return_value="test-token"):
                with self.assertRaises(PanelError):
                    client.panel_status()
        self.assertEqual(destinations, [])


@unittest.skipUnless(HAS_MCP, "install the openai extra to run MCP transport tests")
class TestStdioMCP(PanelCase):
    def connect(self, callback):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def run():
            params = StdioServerParameters(command=sys.executable,
                args=["-m", "rgi", "mcp", "--url", self.base, "--namespace", "test"],
                env={**os.environ, "RGI_TOKEN": "test-token"})
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=datetime.timedelta(seconds=5)) as session:
                    await session.initialize()
                    await callback(session)
        asyncio.run(run())

    def test_discovery_and_full_lifecycle_over_real_stdio(self):
        async def exercise(session):
            tools = (await session.list_tools()).tools
            self.assertEqual({t.name for t in tools},
                             {"panel_status", "session_start", "session_set_state", "session_end"})
            self.assertTrue(next(t for t in tools if t.name == "panel_status").annotations.readOnlyHint)
            self.assertTrue(all(t.outputSchema and not t.annotations.openWorldHint for t in tools))
            result = await session.call_tool("session_start", {"session_id": "stdio", "label": "Stdio test"})
            self.assertFalse(result.isError)
            self.assertEqual(result.structuredContent["slot"], 0)
            for state in ("working", "blocked", "working", "done"):
                result = await session.call_tool("session_set_state", {"session_id": "stdio", "state": state})
                self.assertFalse(result.isError)
                status = await session.call_tool("panel_status", {})
                self.assertEqual(status.structuredContent["sessions"][0]["state"], state)
            self.assertEqual(len(self.lanes.slot), 1)
            result = await session.call_tool("session_end", {"session_id": "stdio"})
            self.assertFalse(result.isError)
            self.assertEqual(self.lanes.slot, {})
        self.connect(exercise)

    def test_schema_validation_unknown_tools_and_missing_session(self):
        async def exercise(session):
            bad_calls = [("session_start", {"session_id": "bad/id", "label": "Bad"}),
                         ("session_start", {"session_id": "test", "label": "Test", "slot": True}),
                         ("session_set_state", {"session_id": "test", "state": "unknown"}),
                         ("panel_status", {"url": "http://example.com"}),
                         ("session_set_state", {"session_id": "missing", "state": "done"}),
                         ("clear", {})]
            for name, args in bad_calls:
                with self.subTest(tool=name, args=args):
                    result = await session.call_tool(name, args)
                    self.assertTrue(result.isError)
            self.assertEqual(self.lanes.slot, {})
        self.connect(exercise)


if __name__ == "__main__":
    unittest.main()
