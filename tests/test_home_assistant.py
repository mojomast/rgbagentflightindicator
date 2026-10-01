"""The Home Assistant bridge, driven against two real localhost HTTP servers.

``tests/mock_panel.py`` is the RGI daemon; ``MockHA`` below is the documented
Home Assistant REST surface (``GET /api/``, ``POST /api/states/<entity_id>``,
``POST /api/events/<event_type>``) with a required bearer token. Nothing here
touches a network beyond 127.0.0.1, and no Home Assistant install is needed.

The interesting cases are the ones that used to be bugs elsewhere: an identical
poll must not re-publish, a transition must fire exactly one event, a rejected
token must never reach a log, and an unreachable Home Assistant must not raise.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from collections import namedtuple
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from rgi.integrations.home_assistant import (
    MAX_SESSIONS, HomeAssistantBridge, main, resolve_credentials, summary,
)
from rgi.report import Reporter
from tests.mock_panel import MockPanel

HA_TOKEN = "ha-token-for-tests"
PANEL_TOKEN = "tok"
ENTITY = "sensor.rgi_panel"
EVENT = "rgi_status_changed"

Record = namedtuple("Record", "method path headers payload")


class MockHA:
    """A stand-in for Home Assistant's authenticated REST API, over real HTTP.

    Records every request, including ones it rejects, so a test can assert on
    headers and bodies. ``stop()``/``restart()`` reuse the same port, which is
    what a Home Assistant restart looks like from the bridge.
    """

    def __init__(self, token: str = HA_TOKEN, port: int = 0):
        self.token = token
        self.requests: list[Record] = []
        self.entities: dict[str, dict] = {}
        self.events: list[tuple[str, dict]] = []
        self.lock = threading.Lock()
        self.port = port
        self.url = ""
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.start()

    # -- lifecycle ---------------------------------------------------------
    def start(self, port: int | None = None) -> "MockHA":
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", self.port if port is None else port), self._handler())
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self) -> "MockHA":
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        return self

    def restart(self) -> "MockHA":
        return self.start(self.port)

    # -- inspection --------------------------------------------------------
    def records(self, method: str | None = None, path: str | None = None) -> list[Record]:
        with self.lock:
            found = list(self.requests)
        if method is not None:
            found = [r for r in found if r.method == method]
        if path is not None:
            found = [r for r in found if r.path == path]
        return found

    # -- the HTTP side -----------------------------------------------------
    def _handler(self):
        ha = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):        # keep the test output clean
                pass

            def _send(self, code: int, body: dict) -> None:
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _read(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                if not length:
                    return {}
                try:
                    return json.loads(self.rfile.read(length))
                except ValueError:
                    return {}

            def _record(self, payload) -> None:
                with ha.lock:
                    ha.requests.append(Record(self.command, self.path,
                                              dict(self.headers.items()), payload))

            def _authorised(self) -> bool:
                return self.headers.get("Authorization") == f"Bearer {ha.token}"

            def do_GET(self):                    # noqa: N802
                self._record(None)
                if not self._authorised():
                    self._send(401, {"message": "Unauthorized"})
                    return
                if self.path.rstrip("/") == "/api":
                    self._send(200, {"message": "API running."})
                    return
                self._send(404, {"message": "Not found"})

            def do_POST(self):                   # noqa: N802
                payload = self._read()
                self._record(payload)
                if not self._authorised():
                    self._send(401, {"message": "Unauthorized"})
                    return
                if self.path.startswith("/api/states/"):
                    entity_id = self.path[len("/api/states/"):]
                    body = {"entity_id": entity_id, "state": payload.get("state"),
                            "attributes": payload.get("attributes") or {}}
                    with ha.lock:
                        ha.entities[entity_id] = body
                    self._send(200, body)
                    return
                if self.path.startswith("/api/events/"):
                    event_type = self.path[len("/api/events/"):]
                    with ha.lock:
                        ha.events.append((event_type, payload))
                    self._send(200, {"message": f"Event {event_type} fired."})
                    return
                self._send(404, {"message": "Not found"})

        return Handler


class BridgeTest(unittest.TestCase):
    """One mocked panel and one mocked Home Assistant, fresh per test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs: list[str] = []
        self.panel = MockPanel(token=PANEL_TOKEN)
        self.panel.thread.start()
        self.addCleanup(self.panel.stop)
        self.ha = MockHA()
        self.addCleanup(self.ha.stop)
        self.config = os.path.join(self.tmp.name, "home-assistant.json")
        patcher = mock.patch.dict(os.environ, {
            "RGI_URL": self.panel.url, "RGI_TOKEN": PANEL_TOKEN,
            "RGI_IDENT": "test-machine", "RGI_STATE_DIR": self.tmp.name,
            "RGI_HA_URL": self.ha.url, "RGI_HA_TOKEN": HA_TOKEN,
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- helpers -----------------------------------------------------------
    def bridge(self, **kwargs) -> HomeAssistantBridge:
        options = {"log": self.logs.append, "timeout": 1.0,
                   "config_path": self.config}
        options.update(kwargs)
        bridge = HomeAssistantBridge(**options)
        self.addCleanup(bridge.close)
        return bridge

    def reporter(self, session: str = "one", label: str = "Fix tests") -> Reporter:
        reporter = Reporter("opencode", session, label=label)
        self.addCleanup(reporter.close)
        return reporter

    def entity(self) -> dict:
        self.assertIn(ENTITY, self.ha.entities)
        return self.ha.entities[ENTITY]

    # -- the aggregate -----------------------------------------------------
    def test_aggregate_state_and_bounded_attributes(self):
        reporter = self.reporter()
        self.assertTrue(reporter.start())
        self.assertTrue(reporter.working())
        with self.panel.lock:
            self.panel.sessions["other:waiting"] = {
                "slot": 1, "key": "F2", "state": "blocked", "label": "Waiting",
                "agent": "other", "ident": "other-agent", "host": "somewhere",
                "info": {"prompt": "SECRET-PROMPT", "transcript": "SECRET-TEXT"},
            }
        bridge = self.bridge()
        self.assertTrue(bridge.poll_once())

        body = self.entity()
        self.assertEqual(body["state"], "attention")
        attributes = body["attributes"]
        self.assertEqual(attributes["total"], 2)
        self.assertEqual(attributes["counts"], {
            "idle": 0, "working": 1, "blocked": 1, "done": 0, "error": 0, "other": 0})
        self.assertEqual([s["state"] for s in attributes["sessions"]],
                         ["working", "blocked"])
        for entry in attributes["sessions"]:
            self.assertEqual(set(entry), {"key", "name", "state", "slot", "label"})
        self.assertEqual(attributes["sessions"][0]["key"], "opencode:one")
        self.assertEqual(attributes["sessions"][0]["label"], "Fix tests")
        self.assertEqual(attributes["sessions"][1]["name"], "Waiting")
        self.assertEqual(attributes["sessions"][1]["slot"], 1)
        blob = json.dumps(body)
        self.assertNotIn("SECRET", blob)
        self.assertNotIn("info", attributes)

    def test_done_lands_on_idle_and_one_entity_is_ever_created(self):
        reporter = self.reporter()
        self.assertTrue(reporter.start())
        self.assertTrue(reporter.done())
        bridge = self.bridge()
        self.assertTrue(bridge.poll_once())
        self.assertEqual(self.entity()["state"], "idle")
        self.assertEqual(self.entity()["attributes"]["counts"]["done"], 1)
        paths = {r.path for r in self.ha.records()}
        self.assertEqual({p for p in paths if p.startswith("/api/states/")},
                         {f"/api/states/{ENTITY}"})
        self.assertFalse([p for p in paths if p.startswith("/api/services/")])

    # -- deduplication and transitions ------------------------------------
    def test_identical_poll_does_not_republish(self):
        reporter = self.reporter()
        self.assertTrue(reporter.start())
        self.assertTrue(reporter.working())
        bridge = self.bridge()
        self.assertTrue(bridge.poll_once())
        self.assertEqual(len(self.ha.records("POST", f"/api/states/{ENTITY}")), 1)
        self.assertEqual(self.ha.events, [])

        self.assertTrue(bridge.poll_once())
        self.assertEqual(len(self.ha.records("POST")), 1)   # no second state, no event
        self.assertEqual(self.ha.events, [])

    def test_transition_publishes_the_event_exactly_once(self):
        reporter = self.reporter()
        self.assertTrue(reporter.start())
        self.assertTrue(reporter.working())
        bridge = self.bridge()
        self.assertTrue(bridge.poll_once())
        self.assertEqual(self.ha.events, [])               # first look is no transition

        self.assertTrue(reporter.blocked(request="approve"))
        self.assertTrue(bridge.poll_once())
        self.assertEqual([event for event, _ in self.ha.events], [EVENT])
        event_payload = self.ha.events[0][1]
        self.assertEqual(event_payload["state"], "attention")
        self.assertEqual(event_payload["attributes"],
                         self.entity()["attributes"])      # same minimal payload

        self.assertTrue(bridge.poll_once())                # identical poll
        self.assertEqual(len(self.ha.events), 1)

        self.assertTrue(reporter.resolve("approve"))
        self.assertTrue(bridge.poll_once())
        self.assertEqual([payload["state"] for _, payload in self.ha.events],
                         ["attention", "working"])
        self.assertTrue(bridge.poll_once())
        self.assertEqual(len(self.ha.events), 2)

    # -- credentials -------------------------------------------------------
    def test_token_is_sent_as_bearer_and_never_logged(self):
        reporter = self.reporter()
        self.assertTrue(reporter.start())
        self.assertTrue(reporter.working())
        bridge = self.bridge()
        self.assertTrue(bridge.verify())
        self.assertTrue(bridge.poll_once())
        for record in self.ha.records():
            self.assertEqual(record.headers.get("Authorization"),
                             f"Bearer {HA_TOKEN}")

        bad = self.bridge(token="wrong-token-for-tests")
        self.assertFalse(bad.poll_once())
        self.assertFalse(bad.verify())
        text = "\n".join(self.logs)
        self.assertNotIn(HA_TOKEN, text)
        self.assertNotIn("wrong-token-for-tests", text)
        self.assertIn("401", text)

        # The default logger goes to stderr and is just as careful.
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            quiet = HomeAssistantBridge(token="rejected-token-for-tests",
                                        timeout=1.0, config_path=self.config)
            self.assertFalse(quiet.poll_once())
            quiet.close()
        self.assertNotIn("rejected-token-for-tests", stream.getvalue())

    # -- a dead Home Assistant --------------------------------------------
    def test_unreachable_ha_does_not_raise_and_reconnects(self):
        reporter = self.reporter()
        self.assertTrue(reporter.start())
        self.assertTrue(reporter.working())
        bridge = self.bridge()
        self.ha.stop()

        self.assertFalse(bridge.poll_once())               # connection refused
        self.assertFalse(bridge.poll_once())               # still no exception
        self.assertTrue(any("did not answer" in log for log in self.logs))

        self.assertTrue(reporter.blocked(request="approve"))
        self.ha.restart()
        self.assertTrue(bridge.poll_once())                # heals without a new bridge
        self.assertEqual(self.entity()["state"], "attention")
        self.assertEqual(self.ha.events, [])               # first published state
        self.assertTrue(reporter.resolve("approve"))
        self.assertTrue(bridge.poll_once())
        self.assertEqual([payload["state"] for _, payload in self.ha.events],
                         ["working"])

        posts = len(self.ha.records("POST"))
        self.assertTrue(bridge.poll_once())
        self.assertEqual(len(self.ha.records("POST")), posts)   # settled again

    def test_reconnect_republishes_an_unchanged_state(self):
        reporter = self.reporter()
        self.assertTrue(reporter.start())
        self.assertTrue(reporter.working())
        bridge = self.bridge()
        self.assertTrue(bridge.poll_once())
        self.ha.stop()

        self.assertTrue(reporter.blocked(request="approve"))
        self.assertFalse(bridge.poll_once())               # attention never lands
        self.assertTrue(reporter.resolve("approve"))       # back to the last state HA saw
        self.assertFalse(bridge.poll_once())               # re-sends it, still refused
        self.ha.restart()
        self.assertTrue(bridge.poll_once())
        self.assertEqual(len(self.ha.records("POST", f"/api/states/{ENTITY}")), 2)
        self.assertEqual(self.ha.events, [])               # no transition made it out

    # -- the loop ----------------------------------------------------------
    def test_run_backs_off_and_never_raises(self):
        bridge = self.bridge(interval=0.5)
        self.ha.stop()
        delays: list[float] = []
        polls = bridge.run(iterations=3, sleep=delays.append)
        self.assertEqual(polls, 3)
        self.assertEqual(delays, [1.0, 2.0])               # 0.5 * 2^n, capped

    def test_main_once_publishes_and_always_exits_zero(self):
        reporter = self.reporter()
        self.assertTrue(reporter.start())
        self.assertTrue(reporter.working())
        self.assertEqual(main(["--once", "--timeout", "1"]), 0)
        self.assertEqual(self.entity()["state"], "working")

        self.ha.stop()
        self.assertEqual(main(["--once", "--timeout", "1"]), 0)

    def test_unconfigured_bridge_is_quiet_and_never_raises(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("RGI_HA_URL", None)
            os.environ.pop("RGI_HA_TOKEN", None)
            os.environ["RGI_HA_CONFIG"] = os.path.join(self.tmp.name, "absent.json")
            bridge = self.bridge(url=None, token=None, config_path=None)
            self.assertFalse(bridge.configured)
            self.assertFalse(bridge.poll_once())
            self.assertFalse(bridge.verify())
            self.assertEqual(main(["--once"]), 0)


class TestSummary(unittest.TestCase):
    """The representation itself: bounded, sorted, and honest about offline."""

    def test_offline_is_bounded_and_empty(self):
        payload = summary(None)
        self.assertEqual(payload["state"], "offline")
        self.assertEqual(payload["attributes"]["total"], 0)
        self.assertEqual(payload["attributes"]["sessions"], [])
        self.assertEqual(payload["attributes"]["counts"]["other"], 0)

    def test_list_is_truncated_but_counts_are_not(self):
        rows = {f"agent:{index:02d}": {"slot": index, "state": "working",
                                       "label": f"task {index}"}
                for index in range(MAX_SESSIONS + 5)}
        payload = summary({"sessions": rows})
        attributes = payload["attributes"]
        self.assertEqual(payload["state"], "working")
        self.assertEqual(attributes["total"], MAX_SESSIONS + 5)
        self.assertEqual(attributes["counts"]["working"], MAX_SESSIONS + 5)
        self.assertEqual(len(attributes["sessions"]), MAX_SESSIONS)
        self.assertEqual([s["slot"] for s in attributes["sessions"]],
                         list(range(MAX_SESSIONS)))
        for entry in attributes["sessions"]:
            self.assertEqual(set(entry), {"key", "name", "state", "slot", "label"})

    def test_unknown_state_is_counted_as_other(self):
        payload = summary({"sessions": {"a": {"slot": 0, "state": "off"}}})
        self.assertEqual(payload["state"], "idle")
        self.assertEqual(payload["attributes"]["counts"]["other"], 1)


class TestCredentials(unittest.TestCase):
    """URL and token resolve in one order, from a file that is never in the repo."""

    def test_file_then_environment_then_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "home-assistant.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"url": "http://ha.invalid:8123", "token": "file-secret"},
                          fh)
            with mock.patch.dict(os.environ):
                for name in ("RGI_HA_URL", "RGI_HA_TOKEN", "RGI_HA_CONFIG"):
                    os.environ.pop(name, None)
                self.assertEqual(resolve_credentials(config_path=path),
                                 ("http://ha.invalid:8123", "file-secret"))
                os.environ["RGI_HA_URL"] = "http://env.invalid:8123"
                os.environ["RGI_HA_TOKEN"] = "env-secret"
                self.assertEqual(resolve_credentials(config_path=path),
                                 ("http://env.invalid:8123", "env-secret"))
                self.assertEqual(resolve_credentials(
                    url="http://arg.invalid:8123", token="arg-secret",
                    config_path=path),
                    ("http://arg.invalid:8123", "arg-secret"))
                self.assertEqual(resolve_credentials(url="ftp://nope", token="x",
                                                     config_path=path),
                                 (None, "x"))       # explicit wins, but must be http(s)

    def test_missing_or_broken_file_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "home-assistant.json")
            with mock.patch.dict(os.environ):
                for name in ("RGI_HA_URL", "RGI_HA_TOKEN", "RGI_HA_CONFIG"):
                    os.environ.pop(name, None)
                self.assertEqual(resolve_credentials(config_path=path), (None, None))
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("{not json")
                self.assertEqual(resolve_credentials(config_path=path), (None, None))


if __name__ == "__main__":
    unittest.main()
