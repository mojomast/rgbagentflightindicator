"""The AgentAPI watcher: scripted SSE, reconnect/reconcile, and junk tolerance.

These tests run a real localhost HTTP mock of AgentAPI (``/status`` plus a
scripted ``/events`` stream) and the repo's real mock panel
(:mod:`tests.mock_panel`), so the watcher's HTTP and the reporter's lane
lifecycle are both exercised, with no network beyond localhost.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from rgi.report import Reporter
from rgi.watchers.agentapi import (
    AgentApiWatcher,
    Event,
    iter_events,
    lane_name,
    normalize_url,
    parse_event,
    session_id_of,
    status_of,
    usage_fields,
)
from tests.mock_panel import MockPanel

GATE = "WAIT"          # a scripted pause the test releases by hand


def sse(name: str, data: dict, *, eid: int | None = None,
        retry: int | None = None) -> str:
    """One Server-Sent Event frame, the way AgentAPI writes it."""
    lines = []
    if eid is not None:
        lines.append(f"id: {eid}")
    if retry is not None:
        lines.append(f"retry: {retry}")
    lines.append(f"event: {name}")
    lines.append("data: " + json.dumps(data))
    return "\n".join(lines) + "\n\n"


class MockAgentAPI:
    """A tiny AgentAPI stand-in: ``GET /status`` and a scripted ``GET /events``.

    The script is replayed, from the top, to every new ``/events`` connection -
    which is exactly what the real one does to let a client reconstruct the
    current state. ``GATE`` inside the script blocks that connection until
    :meth:`release`, so a test can watch an intermediate state.
    """

    def __init__(self, *, status: str = "stable", script: list | None = None,
                 session_id: str = "", agent_type: str = "claude", port: int = 0):
        self.status = status
        self.script = list(script or [])
        self.session_id = session_id
        self.agent_type = agent_type
        self.online = True
        self.requests: list[str] = []
        self.lock = threading.Lock()
        self._gate = threading.Event()
        self._kick = threading.Event()
        self.server = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "MockAgentAPI":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def stop(self) -> None:
        self.online = False
        self._gate.set()
        self._kick.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def pause(self) -> None:
        """Make /status and /events fail, and drop any live stream."""
        self.online = False
        self._kick.set()

    def resume(self) -> None:
        self.online = True
        self._kick.clear()

    def release(self) -> None:
        self._gate.set()

    def set_script(self, script: list) -> None:
        with self.lock:
            self.script = list(script)

    def _handler(self):
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _json(self, code: int, body: dict) -> None:
                raw = json.dumps(body).encode()
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except OSError:
                    pass

            def do_GET(self):                        # noqa: N802
                with mock.lock:
                    mock.requests.append(self.path)
                path = self.path.split("?", 1)[0]
                if not mock.online:
                    self._json(503, {"error": "offline"})
                    return
                if path == "/status":
                    body = {"status": mock.status, "agent_type": mock.agent_type,
                            "transport": "pty"}
                    if mock.session_id:
                        body["session_id"] = mock.session_id
                    self._json(200, body)
                    return
                if path == "/events":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    try:
                        with mock.lock:
                            frames = list(mock.script)
                        for frame in frames:
                            if frame == GATE:
                                mock._gate.wait(timeout=10)
                                continue
                            self.wfile.write(frame.encode("utf-8"))
                            self.wfile.flush()
                        while mock.online and not mock._kick.wait(0.05):
                            pass
                    except OSError:
                        pass
                    return
                self._json(404, {"error": "unknown"})

        return Handler


class AgentApiTestBase(unittest.TestCase):
    def setUp(self):
        self.panel = MockPanel(token="tok", lanes=8).__enter__()
        self.addCleanup(self.panel.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs: list[str] = []
        self.watchers: list[AgentApiWatcher] = []
        self.reporters: list[Reporter] = []

    def tearDown(self):
        for watcher in self.watchers:
            watcher.stop()
        for reporter in self.reporters:
            try:
                reporter.close()
            except Exception:
                pass

    def watcher_for(self, mock: MockAgentAPI, *, name: str | None = None,
                    label: str | None = None, **kwargs):
        name = name or lane_name(mock.url)
        reporter = Reporter("agentapi", name, label=label, url=self.panel.url,
                            token="tok", ident="test-machine",
                            state_dir=os.path.join(self.tmp.name, "state"),
                            log=self.logs.append)
        self.reporters.append(reporter)
        kwargs.setdefault("log", self.logs.append)
        watcher = AgentApiWatcher(mock.url, reporter=reporter, label=label,
                                  poll_s=0.05, done_grace=0.15,
                                  backoff=0.05, http_timeout=0.5, **kwargs)
        self.watchers.append(watcher)
        watcher.start()
        return watcher, reporter

    def wait_for(self, predicate, message: str = "condition not met",
                 timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail(message)


class ScriptedTurnTest(AgentApiTestBase):
    def test_a_running_then_completed_turn_claims_and_lands_with_usage(self):
        mock = MockAgentAPI(script=[
            sse("status_change", {"status": "running", "agent_type": "claude"}),
            GATE,
            sse("turn_completed", {
                "usage": {"input_tokens": 1200, "output_tokens": 340,
                          "total_tokens": 1540, "cache_read_input_tokens": 90}}),
            sse("status_change", {"status": "stable", "agent_type": "claude"}),
        ]).__enter__()
        self.addCleanup(mock.stop)
        watcher, reporter = self.watcher_for(mock)
        key = reporter.session_key

        self.wait_for(lambda: self.panel.state_of(key) == "working",
                      "the running event did not claim a working lane")
        claims = self.panel.requests_for("/session/start")
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0][2]["sessionID"], key)
        self.assertEqual(claims[0][2]["agent"], "agentapi")
        self.assertEqual(claims[0][2]["ident"], "test-machine")

        mock.release()
        self.wait_for(lambda: self.panel.state_of(key) == "done",
                      "turn_completed did not land the lane")
        self.assertEqual(self.panel.info_of(key).get("tokens"),
                         {"input": 1200, "output": 340, "cache_read": 90,
                          "total": 1540})
        self.wait_for(lambda: self.panel.state_of(key) == "idle",
                      "done did not settle to idle after the grace")

    def test_a_stable_instance_shows_idle_and_keeps_its_lane(self):
        mock = MockAgentAPI(script=[
            sse("status_change", {"status": "stable", "agent_type": "aider"}),
        ]).__enter__()
        self.addCleanup(mock.stop)
        watcher, reporter = self.watcher_for(mock)
        key = reporter.session_key

        self.wait_for(lambda: self.panel.state_of(key) == "idle",
                      "a stable instance did not claim an idle lane")
        time.sleep(0.2)
        # The lamp means the session, not the turn: it does not vanish.
        self.assertEqual(self.panel.state_of(key), "idle")
        self.assertIn(key, self.panel.sessions)
        self.assertEqual(self.panel.info_of(key).get("agent_type"), "claude")


class ReconnectTest(AgentApiTestBase):
    def test_connection_loss_changes_nothing_and_reconnect_reclaims(self):
        mock = MockAgentAPI(status="running", script=[
            sse("status_change", {"status": "running"}),
        ]).__enter__()
        self.addCleanup(mock.stop)
        watcher, reporter = self.watcher_for(mock)
        key = reporter.session_key
        self.wait_for(lambda: self.panel.state_of(key) == "working")
        starts_before = len(self.panel.requests_for("/session/start"))

        mock.pause()
        self.wait_for(lambda: any("did not answer" in line for line in self.logs),
                      "the connection loss was not logged")
        time.sleep(0.2)
        self.assertEqual(self.panel.state_of(key), "working")   # state untouched
        self.assertIn(key, self.panel.sessions)                 # and not freed

        # The panel restarted while AgentAPI was away: reconnect must re-claim.
        with self.panel.lock:
            self.panel.sessions.clear()
        mock.resume()
        self.wait_for(lambda: self.panel.state_of(key) == "working",
                      "reconnect did not re-claim and reconcile the lane")
        self.assertGreater(len(self.panel.requests_for("/session/start")),
                           starts_before)

        # The turn finished while we were disconnected: /status stable -> done.
        mock.status = "stable"
        mock.set_script([sse("status_change", {"status": "stable"})])
        mock.pause()
        mock.resume()
        self.wait_for(lambda: self.panel.state_of(key) == "done",
                      "reconcile did not land a turn that ended while away")
        self.wait_for(lambda: self.panel.state_of(key) == "idle")


class IdentityTest(AgentApiTestBase):
    def test_a_session_id_is_a_lane_and_a_restart_gets_a_new_lane(self):
        mock = MockAgentAPI(session_id="one", script=[
            sse("status_change", {"status": "stable", "session_id": "one"}),
        ]).__enter__()
        self.addCleanup(mock.stop)
        watcher, reporter = self.watcher_for(mock)
        base = lane_name(mock.url)
        first = f"agentapi:{base}:one"
        self.wait_for(lambda: first in self.panel.sessions,
                      "the session id did not become part of the lane key")
        self.assertEqual(self.panel.state_of(first), "idle")

        # AgentAPI restarts with a fresh session: a fresh lane, not the old one.
        mock.session_id = "two"
        mock.set_script([sse("status_change", {"status": "stable", "session_id": "two"})])
        mock.pause()
        mock.resume()
        second = f"agentapi:{base}:two"
        self.wait_for(lambda: second in self.panel.sessions,
                      "the new session did not get its own lane")
        self.assertNotIn(first, self.panel.sessions)

    def test_an_explicit_name_overrides_the_host_port(self):
        mock = MockAgentAPI(script=[sse("status_change", {"status": "stable"})]).__enter__()
        self.addCleanup(mock.stop)
        watcher, reporter = self.watcher_for(mock, name="goose-box")
        key = "agentapi:goose-box"
        self.assertEqual(reporter.session_key, key)
        self.wait_for(lambda: key in self.panel.sessions)


class ToleranceTest(AgentApiTestBase):
    def test_malformed_and_unknown_events_do_not_stop_the_stream(self):
        mock = MockAgentAPI(script=[
            ": a keepalive comment\n\n",
            sse("status_change", {"status": "running"}),
            "event: status_change\ndata: {this is not json\n\n",
            "event: an_event_from_the_future\ndata: {\"hello\": \"world\"}\n\n",
            "id: 12\nretry: 1000\nevent: status_change\ndata:\n\n",
            GATE,
            "data: {\"type\": \"status_change\", \"data\": {\"status\": \"stable\"}}\n\n",
        ]).__enter__()
        self.addCleanup(mock.stop)
        watcher, reporter = self.watcher_for(mock)
        key = reporter.session_key

        self.wait_for(lambda: self.panel.state_of(key) == "working",
                      "the good event before the junk was lost")
        self.assertTrue(watcher._thread.is_alive())
        mock.release()
        self.wait_for(lambda: self.panel.state_of(key) == "done",
                      "the NDJSON-shaped event was not understood")
        self.wait_for(lambda: self.panel.state_of(key) == "idle")

    def test_agent_errors_warn_or_redden_and_recover(self):
        mock = MockAgentAPI(status="running", script=[
            sse("status_change", {"status": "running"}),
            GATE,
            sse("agent_error", {"level": "warning", "message": "retrying"}),
            sse("status_change", {"status": "stable"}),
            sse("agent_error", {"level": "error", "message": "out of credit"}),
        ]).__enter__()
        self.addCleanup(mock.stop)
        watcher, reporter = self.watcher_for(mock)
        key = reporter.session_key

        self.wait_for(lambda: self.panel.state_of(key) == "working")
        mock.release()
        self.wait_for(lambda: self.panel.state_of(key) == "error",
                      "an error-level agent_error did not redden the lane")
        self.assertEqual(self.panel.info_of(key).get("warning"), "retrying")


class ParserTest(unittest.TestCase):
    def test_lane_name_defaults_to_the_urls_host_and_port(self):
        self.assertEqual(lane_name("http://127.0.0.1:3284"), "127.0.0.1:3284")
        self.assertEqual(lane_name("box:80/"), "box:80")
        self.assertEqual(lane_name("http://box", "explicit"), "explicit")

    def test_normalize_url_adds_a_scheme_and_strips_slashes(self):
        self.assertEqual(normalize_url("127.0.0.1:3284/"),
                         "http://127.0.0.1:3284")
        self.assertEqual(normalize_url(""), "http://127.0.0.1:3284")
        self.assertEqual(normalize_url("http://box:1"), "http://box:1")

    def test_iter_events_reads_sse_frames_and_skips_the_rest(self):
        lines = [
            "event: status_change", 'data: {"status": "running"}', "",
            ": a comment", "id: 3", "retry: 500",
            "event: message_update", 'data: {"message": "hi"}', "", "",
        ]
        self.assertEqual(list(iter_events(lines)), [
            ("status_change", '{"status": "running"}'),
            ("message_update", '{"message": "hi"}'),
        ])

    def test_iter_events_reads_ndjson_and_multiline_data(self):
        lines = [
            '{"type": "status_change", "status": "running"}',
            "event: note", "data: line one", "data: line two", "",
        ]
        self.assertEqual(list(iter_events(lines)), [
            ("", '{"type": "status_change", "status": "running"}'),
            ("note", "line one\nline two"),
        ])

    def test_parse_event_handles_both_wire_shapes(self):
        self.assertEqual(parse_event("status_change", '{"status": "running"}'),
                         Event("status_change", {"status": "running"}))
        self.assertEqual(
            parse_event("", '{"event": "turn_completed", "data": {"usage": {"input_tokens": 3}}}'),
            Event("turn_completed", {"usage": {"input_tokens": 3}}))
        self.assertEqual(
            parse_event("", '{"type": "message_update", "message": "hi"}'),
            Event("message_update", {"type": "message_update", "message": "hi"}))

    def test_parse_event_ignores_junk(self):
        self.assertIsNone(parse_event("", "{"))
        self.assertIsNone(parse_event("", "[1, 2, 3]"))
        self.assertEqual(parse_event("status_change", "{oops"),
                         Event("status_change", {}))

    def test_status_words_coalesce(self):
        self.assertEqual(status_of({"status": "running"}), "running")
        self.assertEqual(status_of({"state": "busy"}), "running")
        self.assertEqual(status_of({"status": "STABLE"}), "stable")
        self.assertEqual(status_of({"status": {"state": "idle"}}), "stable")
        self.assertIsNone(status_of({"status": "something-else"}))
        self.assertIsNone(status_of({}))

    def test_session_ids_from_every_spelling(self):
        self.assertEqual(session_id_of({"session_id": "abc"}), "abc")
        self.assertEqual(session_id_of({"sessionID": 42}), "42")
        self.assertEqual(session_id_of({"session": "s"}), "s")
        self.assertEqual(session_id_of({}), "")

    def test_usage_fields_coalesce(self):
        self.assertEqual(
            usage_fields({"usage": {"prompt_tokens": 10, "completion_tokens": 4}}),
            {"input": 10, "output": 4})
        self.assertEqual(
            usage_fields({"tokens": {"input_tokens": 5, "output_tokens": 2,
                                     "cache_read": 7, "cost": 0.25}}),
            {"input": 5, "output": 2, "cache_read": 7, "cost": 0.25})
        self.assertEqual(usage_fields({"total_tokens": 3}), {"total": 3})
        self.assertEqual(usage_fields({"message": "no numbers here"}), {})


if __name__ == "__main__":
    unittest.main()
