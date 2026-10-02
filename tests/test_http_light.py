"""HTTP-light backend against a local recording server.

No blink(1), busylight or other lamp is needed: a stdlib ThreadingHTTPServer
records every request (method, path, headers, body) and answers 200. The tests
pin the two presets and the template engine byte for byte, then prove the
daemon's single-colour collapse really arrives as one colour.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from rgi.backends.base import BackendUnavailable
from rgi.backends.http_light import (
    PRESETS, URL_ENV, HttpLightBackend, normalise_url, render,
)


class _LightServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):     # hung tests stay quiet
        pass


class MockLight:
    """Records requests; always answers ``status`` with a small JSON body."""

    def __init__(self):
        self.requests: list[dict] = []
        self.status = 200
        self.lock = threading.Lock()
        self.server = _LightServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)

    def recorded(self, path: str | None = None) -> list[dict]:
        with self.lock:
            return [request for request in self.requests
                    if path is None or request["path"].startswith(path)]

    def last(self) -> dict:
        with self.lock:
            return self.requests[-1]

    def _handler(self):
        mock_self = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                record = {
                    "method": self.command,
                    "path": self.path,
                    "headers": {key.lower(): value
                                for key, value in self.headers.items()},
                    "body": body,
                }
                with mock_self.lock:
                    mock_self.requests.append(record)
                payload = json.dumps({"ok": True}).encode()
                self.send_response(mock_self.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):                              # noqa: N802
                self._handle()

            def do_POST(self):                             # noqa: N802
                self._handle()

        return Handler


class HttpLightCase(unittest.TestCase):
    def setUp(self):
        self.light = MockLight()
        self.addCleanup(self.light.stop)

    def make(self, **kwargs) -> HttpLightBackend:
        kwargs.setdefault("url", self.light.url)
        backend = HttpLightBackend(**kwargs)
        self.addCleanup(backend.close)
        return backend

    def preset_url(self, name: str) -> str:
        """The preset's URL, retargeted at the mock's port."""
        return (PRESETS[name]["url"]
                .replace("127.0.0.1:8934", f"127.0.0.1:{self.light.port}")
                .replace("127.0.0.1:8000", f"127.0.0.1:{self.light.port}"))

    @staticmethod
    def free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]


class TestRendering(unittest.TestCase):
    def test_tokens_are_substituted(self):
        context = {"r": "1", "g": "2", "b": "3", "hex": "010203", "on": "true"}
        self.assertEqual(render("r={r} g={g} b={b} hex={hex} on={on}", context),
                         "r=1 g=2 b=3 hex=010203 on=true")

    def test_unknown_placeholders_are_literal(self):
        context = {"r": "5", "g": "6", "b": "7", "hex": "050607", "on": "true"}
        # substitution is literal, not str.format: no escaping rules, but
        # {r.__class__} can never reach into the process.
        self.assertEqual(render("{evil} {r} {{r}} {r.__class__}", context),
                         "{evil} 5 {5} {r.__class__}")

    def test_bare_hosts_get_http(self):
        self.assertEqual(normalise_url("lamp.local:8000"), "http://lamp.local:8000")
        self.assertEqual(normalise_url("https://lamp.local/x"), "https://lamp.local/x")
        self.assertEqual(normalise_url("ftp://lamp.local"), "")
        self.assertEqual(normalise_url(None), "")


class TestBlink1Preset(HttpLightCase):
    def test_preset_is_a_get_fade(self):
        spec = PRESETS["blink1"]
        self.assertEqual(spec["method"], "GET")
        self.assertEqual(spec["url"],
                         "http://127.0.0.1:8934/blink1/fadeToRGB?rgb=%23{hex}")
        backend = self.make(preset="blink1", url=self.preset_url("blink1"))
        self.assertEqual(backend.method, "GET")
        backend.open()
        backend.write([(0, 255, 0)])

        request = self.light.last()
        self.assertEqual(request["method"], "GET")
        self.assertEqual(request["path"],
                         "/blink1/fadeToRGB?rgb=%2300FF00")
        self.assertEqual(request["body"], b"")

    def test_black_is_still_sent_as_a_colour(self):
        backend = self.make(preset="blink1", url=self.preset_url("blink1"))
        backend.open()
        backend.write([(0, 0, 0)])
        self.assertEqual(self.light.last()["path"],
                         "/blink1/fadeToRGB?rgb=%23000000")


class TestBusylightPreset(HttpLightCase):
    def test_preset_posts_json(self):
        spec = PRESETS["busylight"]
        self.assertEqual(spec["method"], "POST")
        backend = self.make(preset="busylight", url=self.preset_url("busylight"))
        self.assertEqual(backend.method, "POST")
        backend.open()
        backend.write([(255, 0, 0)])

        request = self.light.last()
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["path"], "/api/v1/lights/on")
        self.assertEqual(request["headers"].get("content-type"),
                         "application/json")
        self.assertEqual(json.loads(request["body"]),
                         {"color": "#FF0000", "dim": 1.0, "led": 0})

    def test_a_failed_preset_request_raises(self):
        backend = self.make(preset="busylight", url=self.preset_url("busylight"))
        backend.open()
        self.light.status = 500
        with self.assertRaises(BackendUnavailable) as caught:
            backend.write([(1, 2, 3)])
        self.assertIn("HTTP 500", str(caught.exception))


class TestTemplates(HttpLightCase):
    def test_plain_text_body_with_every_placeholder(self):
        backend = self.make(
            method="POST",
            body_template="r={r} g={g} b={b} hex={hex} on={on}")
        backend.open()
        backend.write([(1, 2, 3)])
        request = self.light.last()
        self.assertEqual(request["body"], b"r=1 g=2 b=3 hex=010203 on=true")
        self.assertEqual(request["headers"].get("content-type"),
                         "text/plain; charset=utf-8")

    def test_on_is_false_for_black(self):
        backend = self.make(method="POST", body_template="{r},{g},{b},{on}")
        backend.open()
        backend.write([(0, 0, 0)])
        self.assertEqual(self.light.last()["body"], b"0,0,0,false")

    def test_unknown_placeholders_are_not_expanded(self):
        backend = self.make(method="POST",
                            body_template="{evil} {r} {{r}} {r.__class__}")
        backend.open()
        backend.write([(5, 6, 7)])
        self.assertEqual(self.light.last()["body"],
                         b"{evil} 5 {5} {r.__class__}")

    def test_json_string_template(self):
        backend = self.make(method="POST",
                            body_template='{"color": "#{hex}", "on": {on}}')
        backend.open()
        backend.write([(16, 32, 48)])
        request = self.light.last()
        self.assertEqual(request["headers"].get("content-type"),
                         "application/json")
        self.assertEqual(json.loads(request["body"]),
                         {"color": "#102030", "on": True})

    def test_bare_hex_string_is_a_literal_body(self):
        backend = self.make(method="POST", body_template="{hex}")
        backend.open()
        backend.write([(1, 2, 3)])
        self.assertEqual(self.light.last()["body"], b"010203")
        self.assertEqual(self.light.last()["headers"].get("content-type"),
                         "text/plain; charset=utf-8")

    def test_dict_template_is_json(self):
        backend = self.make(method="POST",
                            body_template={"outer": {"hex": "{hex}", "n": 1},
                                           "flag": "{on}"})
        backend.open()
        backend.write([(9, 8, 7)])
        request = self.light.last()
        self.assertEqual(request["headers"].get("content-type"),
                         "application/json")
        self.assertEqual(json.loads(request["body"]),
                         {"outer": {"hex": "090807", "n": 1}, "flag": "true"})

    def test_headers_are_kept_and_content_type_is_not_overwritten(self):
        backend = self.make(method="POST", headers={"X-Token": "s",
                                                    "Content-Type": "text/x-lit"},
                            body_template="{hex}")
        backend.open()
        backend.write([(1, 2, 3)])
        request = self.light.last()
        self.assertEqual(request["headers"].get("x-token"), "s")
        self.assertEqual(request["headers"].get("content-type"), "text/x-lit")

    def test_url_placeholders_are_rendered(self):
        backend = self.make(url=self.light.url + "/set?hex={hex}&on={on}")
        backend.open()
        backend.write([(10, 20, 30)])
        self.assertEqual(self.light.last()["path"], "/set?hex=0A141E&on=true")


class TestBehaviour(HttpLightCase):
    def test_lamps_are_identical_single_colour_entries(self):
        backend = self.make()
        self.assertFalse(backend.per_lamp)
        self.assertEqual(len(backend.lamps()), 12)
        self.assertEqual(backend.lamps()[0].label, "light")
        self.assertEqual([lamp.index for lamp in backend.lamps()],
                         list(range(12)))
        self.assertEqual(len(HttpLightBackend(count=3).lamps()), 3)

    def test_the_first_collapsed_colour_is_used(self):
        backend = self.make(body_template="{hex}", method="POST")
        backend.open()
        # the daemon repeats the collapsed colour for every lamp
        backend.write([(9, 8, 7)] * len(backend.lamps()))
        request = self.light.last()
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["body"], b"090807")

    def test_write_before_open_is_refused(self):
        backend = self.make()
        with self.assertRaises(BackendUnavailable):
            backend.write([(1, 2, 3)])

    def test_open_without_configuration_is_refused(self):
        with mock.patch.dict(os.environ, {URL_ENV: "", "RGI_HTTP_LIGHT_PRESET": ""}):
            backend = HttpLightBackend()
            with self.assertRaises(BackendUnavailable) as caught:
                backend.open()
        self.assertIn("not configured", str(caught.exception))

    def test_open_with_an_invalid_url_is_refused(self):
        backend = HttpLightBackend(url="ftp://lamp.local")
        with self.assertRaises(BackendUnavailable) as caught:
            backend.open()
        self.assertIn("not a valid", str(caught.exception))

    def test_open_with_a_dead_endpoint_is_refused(self):
        backend = HttpLightBackend(url=f"http://127.0.0.1:{self.free_port()}")
        with self.assertRaises(BackendUnavailable) as caught:
            backend.open()
        self.assertIn("no HTTP answer", str(caught.exception))

    def test_unknown_preset_is_a_configuration_error(self):
        with self.assertRaises(ValueError):
            HttpLightBackend(preset="not-a-light")

    def test_probe_accepts_any_http_answer(self):
        backend = self.make()
        self.light.status = 404
        self.assertTrue(backend.probe())

    def test_close_is_safe_twice_and_stops_writes(self):
        backend = self.make(body_template="{hex}")
        backend.open()
        backend.close()
        backend.close()
        with self.assertRaises(BackendUnavailable):
            backend.write([(1, 2, 3)])


class TestAvailability(HttpLightCase):
    def clean_env(self, **extra) -> dict:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("RGI_HTTP_LIGHT_")}
        env.update(extra)
        return env

    def test_available_when_the_service_answers(self):
        with mock.patch.dict(os.environ, self.clean_env(
                **{URL_ENV: self.light.url}), clear=True):
            self.assertTrue(HttpLightBackend.available())

    def test_unavailable_without_configuration(self):
        with mock.patch.dict(os.environ, self.clean_env(), clear=True):
            self.assertFalse(HttpLightBackend.available())

    def test_unavailable_when_nothing_listens(self):
        with mock.patch.dict(os.environ, self.clean_env(
                **{URL_ENV: f"http://127.0.0.1:{self.free_port()}"}), clear=True):
            self.assertFalse(HttpLightBackend.available())

    def test_available_never_raises(self):
        with mock.patch.dict(os.environ, self.clean_env(
                **{URL_ENV: "http://127.0.0.1:9"}), clear=True):
            with mock.patch("rgi.backends.http_light._probe",
                            side_effect=RuntimeError("boom")):
                self.assertFalse(HttpLightBackend.available())

    def test_a_configured_preset_pings_the_preset_url(self):
        probed = []
        with mock.patch(
            "rgi.backends.http_light._probe",
            side_effect=lambda url, timeout: probed.append(url) or True,
        ):
            with mock.patch.dict(os.environ, self.clean_env(
                    **{"RGI_HTTP_LIGHT_PRESET": "blink1"}), clear=True):
                self.assertTrue(HttpLightBackend.available())
        self.assertEqual(probed, [PRESETS["blink1"]["url"]])


class TestDaemonCollapse(HttpLightCase):
    """The daemon's most-urgent-colour collapse, end to end."""

    def test_frame_collapses_and_the_backend_paints_one_colour(self):
        from rgi.daemon import Device

        backend = self.make(body_template="{hex}")
        backend.open()
        device = Device(backend, list(range(len(backend.lamps()))),
                        label="http-light")
        now = time.monotonic()
        lanes = {
            0: "working",
            3: "blocked",
            7: "done",
            ("changed", 0): now,
            ("changed", 3): now,
            ("changed", 7): now,
        }
        colours = device.frame(lanes, now, quiet=True)
        self.assertEqual(len(colours), len(backend.lamps()))
        self.assertEqual(len(set(colours)), 1, "a single light holds one colour")
        self.assertNotEqual(colours[0], (0, 0, 0))

        backend.write(colours)
        expected = f"{colours[0][0]:02X}{colours[0][1]:02X}{colours[0][2]:02X}"
        self.assertEqual(self.light.last()["body"], expected.encode())


if __name__ == "__main__":
    unittest.main()
