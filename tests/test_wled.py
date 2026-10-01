"""WLED backend against a local mock of the WLED JSON API.

No strip, no network: a stdlib ThreadingHTTPServer stands in for the controller,
records every request, and can be made slow, unreachable or hanging. The mock is
deliberately dumb - it does not emulate WLED's rendering, only the wire
behaviour this backend depends on - so the assertions are about the exact JSON
bodies the backend posts, including WLED's segment-relative ``i`` indices and
the state close() puts back.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from rgi.backends.base import BackendUnavailable
from rgi.backends.wled import WledBackend, normalise_url, parse_pixels


def wait_for(predicate, timeout: float = 3.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def segment(seg_id: int = 0, start: int = 0, stop: int = 12, **overrides) -> dict:
    """One segment as GET /json/state serializes it."""
    seg = {
        "id": seg_id, "start": start, "stop": stop, "len": stop - start,
        "grp": 1, "spc": 0, "of": 0, "on": True, "frz": False, "bri": 255,
        "cct": 127, "col": [[255, 160, 0], [0, 0, 0], [0, 0, 0]],
        "fx": 0, "sx": 128, "ix": 128, "pal": 0, "sel": True, "rev": False,
        "mi": False, "o1": False, "o2": False, "o3": False, "si": 0,
        "m12": 0, "lc": 1,
    }
    seg.update(overrides)
    seg["len"] = seg["stop"] - seg["start"]
    return seg


def solid(backend: WledBackend, colour) -> list:
    return [colour] * len(backend.lamps())


class _WledServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):   # a hung test must stay quiet
        pass


class MockWled:
    """A recording stand-in for one WLED box."""

    def __init__(self, *, leds: int = 64, segments=None, on: bool = True, bri: int = 200):
        self.info = {"ver": "16.0.1", "name": "mock wled", "live": False,
                     "leds": {"count": leds, "maxseg": 16}}
        self.state = {"on": on, "bri": bri, "mainseg": 0,
                      "seg": segments if segments is not None else [segment(stop=leds)]}
        self.requests: list[tuple[str, str, object]] = []
        self.post_delay = 0.0
        self.get_delay = 0.0
        self.offline = False          # answer POSTs with 503
        self.hang = False             # accept a POST and never answer until released
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.server = _WledServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)

    def posts(self) -> list:
        with self.lock:
            return [body for method, path, body in self.requests
                    if method == "POST" and path == "/json/state"]

    def frames(self) -> list:
        """State POSTs that carry individual pixels (`i`)."""
        out = []
        for body in self.posts():
            if not isinstance(body, dict):
                continue
            segs = body.get("seg")
            if isinstance(segs, list) and segs and isinstance(segs[0], dict) \
                    and "i" in segs[0]:
                out.append(body)
        return out

    # -- the HTTP side -----------------------------------------------------
    def _handler(self):
        mock_self = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _record(self, body):
                with mock_self.lock:
                    mock_self.requests.append((self.command, self.path, body))

            def _send(self, code: int, payload: dict) -> None:
                raw = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):                    # noqa: N802
                self._record(None)
                if mock_self.get_delay:
                    time.sleep(mock_self.get_delay)
                if self.path == "/json/info":
                    self._send(200, mock_self.info)
                elif self.path == "/json/state":
                    self._send(200, mock_self.state)
                else:
                    self._send(404, {"error": "unknown"})

            def do_POST(self):                   # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = None
                self._record(body)
                if mock_self.offline:
                    self._send(503, {"error": "offline"})
                    return
                if mock_self.post_delay:
                    time.sleep(mock_self.post_delay)
                if mock_self.hang:
                    mock_self.release.wait(5.0)
                self._send(200, {"success": True})

        return Handler


class WledTestCase(unittest.TestCase):
    def setUp(self):
        self.mock = MockWled()
        self.addCleanup(self.mock.stop)
        self.tmp = tempfile.mkdtemp(prefix="rgi-wled-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config_file = os.path.join(self.tmp, "wled.json")   # deliberately absent

    def make(self, **kwargs) -> WledBackend:
        kwargs.setdefault("url", self.mock.url)
        kwargs.setdefault("segment", 0)
        kwargs.setdefault("config_file", self.config_file)
        backend = WledBackend(**kwargs)
        self.addCleanup(self._close_quietly, backend)
        return backend

    @staticmethod
    def _close_quietly(backend: WledBackend) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            backend.close()


class TestDiscovery(WledTestCase):
    def test_info_and_state_are_parsed(self):
        self.mock.info["ver"] = "0.14.4"
        self.mock.state["seg"] = [segment(stop=30), segment(1, start=30, stop=42, n="panel")]
        backend = self.make(segment=1)
        backend.open()

        self.assertEqual(backend.version, "0.14.4")
        self.assertEqual(backend.strip_leds, 64)
        self.assertEqual(len(backend.lamps()), 12)
        self.assertEqual(backend.lamps()[0].label, "px0")
        self.assertEqual(backend.lamps()[11].label, "px11")
        self.assertTrue(backend.per_lamp)
        gets = [path for method, path, _ in self.mock.requests if method == "GET"]
        self.assertIn("/json/info", gets)
        self.assertIn("/json/state", gets)

    def test_missing_segment_is_refused(self):
        backend = self.make(segment=9)
        with self.assertRaises(BackendUnavailable):
            backend.open()

    def test_pixels_select_segment_relative_offsets(self):
        self.mock.state["seg"] = [segment(start=30, stop=42)]
        backend = self.make(pixels="2,4-5")
        backend.open()
        self.assertEqual([lamp.label for lamp in backend.lamps()],
                         ["px2", "px4", "px5"])
        # indices are inside the segment, not at strip offsets 32/34/35
        self.assertEqual(backend.frame_body([(1, 2, 3), (4, 5, 6), (7, 8, 9)])["i"],
                         [2, "010203", 4, "040506", 5, "070809"])


class TestPainting(WledTestCase):
    def test_posts_segment_relative_pixels(self):
        self.mock.state["seg"] = [segment(1, start=30, stop=42)]
        backend = self.make(segment=1)
        backend.open()
        colours = [(i, 255 - i, i * 2) for i in range(12)]
        backend.write(colours)

        self.assertTrue(wait_for(lambda: len(self.mock.frames()) >= 1))
        seg = self.mock.frames()[-1]["seg"][0]
        self.assertEqual(seg["id"], 1)
        self.assertTrue(seg["on"])
        self.assertEqual(seg["bri"], 255)
        self.assertEqual(seg["i"], [f"{r:02X}{g:02X}{b:02X}" for r, g, b in colours])


class TestBrightness(WledTestCase):
    def test_off_strip_is_turned_on_and_power_is_restored(self):
        self.mock.state["on"] = False
        self.mock.state["bri"] = 0
        backend = self.make()
        backend.open()
        self.assertEqual(self.mock.posts()[0], {"on": True, "bri": 255})

        backend.close()
        restore = self.mock.posts()[-1]
        self.assertEqual(restore.get("on"), False)
        self.assertEqual(restore.get("bri"), 0)
        self.assertIn("seg", restore)

    def test_running_strip_is_left_alone(self):
        backend = self.make()
        backend.open()
        self.assertEqual(self.mock.posts(), [])


class TestSenderThread(WledTestCase):
    def test_frames_coalesce_while_one_is_in_flight(self):
        self.mock.post_delay = 0.5
        backend = self.make(timeout=3.0)
        backend.min_interval = 0.01
        backend.open()

        backend.write(solid(backend, (1, 1, 1)))
        self.assertTrue(wait_for(lambda: len(self.mock.frames()) == 1))
        backend.write(solid(backend, (2, 2, 2)))   # arrives while the first is in flight
        backend.write(solid(backend, (3, 3, 3)))

        self.assertTrue(wait_for(lambda: len(self.mock.frames()) == 2))
        time.sleep(self.mock.post_delay + 0.2)
        frames = self.mock.frames()
        self.assertEqual(len(frames), 2, "the middle frame must be dropped, not queued")
        self.assertEqual(frames[1]["seg"][0]["i"][0], "030303")
        self.mock.post_delay = 0.0

    def test_timeout_does_not_stall_the_caller_or_raise(self):
        self.mock.hang = True
        backend = self.make(timeout=0.25)
        backend.open()
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            started = time.monotonic()
            backend.write(solid(backend, (9, 9, 9)))
            self.assertLess(time.monotonic() - started, 0.2)

            self.assertTrue(wait_for(lambda: len(self.mock.frames()) == 1))
            time.sleep(0.4)                        # the request gives up by itself
            lines = [l for l in buf.getvalue().splitlines() if "wled" in l]
            self.assertEqual(len(lines), 1)

            self.mock.hang = False
            self.mock.release.set()
            backend.close()


class TestFailureAndRecovery(WledTestCase):
    def test_failure_is_logged_once_per_outage(self):
        self.mock.offline = True
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            backend = self.make(timeout=0.5, retry_interval=30.0)
            backend.open()
            for shade in (1, 2, 3):
                backend.write(solid(backend, (shade, shade, shade)))
                time.sleep(0.05)

            self.assertTrue(wait_for(lambda: len(self.mock.frames()) == 1))
            time.sleep(0.25)
            self.assertEqual(len(self.mock.frames()), 1, "backoff must hold the retries")
            lines = [l for l in buf.getvalue().splitlines() if "wled" in l]
            self.assertEqual(len(lines), 1)

    def test_recovers_when_the_controller_comes_back(self):
        backend = self.make(timeout=0.3, retry_interval=0.05)
        backend.open()
        self.mock.offline = True

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            backend.write(solid(backend, (5, 5, 5)))
            self.assertTrue(wait_for(lambda: len(self.mock.frames()) == 1))

            self.mock.offline = False
            # the backend retries the newest frame on its own
            self.assertTrue(wait_for(lambda: len(self.mock.frames()) >= 2))
            backend.write(solid(backend, (6, 6, 6)))
            self.assertTrue(wait_for(lambda: len(self.mock.frames()) >= 3))

        self.assertEqual(self.mock.frames()[-1]["seg"][0]["i"][0], "060606")


class TestRestore(WledTestCase):
    def test_close_restores_the_captured_segment(self):
        original = segment(2, start=30, stop=42, bri=140, fx=3, sx=100, ix=50,
                           pal=5, col=[[10, 20, 30], [1, 2, 3], [4, 5, 6]])
        self.mock.state["seg"] = [segment(0, start=0, stop=30), original]
        backend = self.make(segment=2)
        backend.open()
        backend.write(solid(backend, (255, 0, 0)))
        self.assertTrue(wait_for(lambda: len(self.mock.frames()) >= 1))

        backend.close()
        restored = None
        for payload in reversed(self.mock.posts()):
            segs = payload.get("seg") if isinstance(payload, dict) else None
            if isinstance(segs, list) and segs and isinstance(segs[0], dict) \
                    and segs[0].get("id") == 2 and "i" not in segs[0]:
                restored = segs[0]
                break
        self.assertIsNotNone(restored, "close() must post the segment back")
        for key in ("start", "stop", "on", "frz", "bri", "fx", "sx", "ix",
                    "pal", "col", "grp", "spc", "of", "cct", "rev", "mi",
                    "o1", "o2", "o3"):
            self.assertEqual(restored[key], original[key], key)
        for key in ("sel", "len", "lc"):           # never written back
            self.assertNotIn(key, restored)

    def test_close_is_safe_twice(self):
        backend = self.make()
        backend.open()
        backend.close()
        posts = len(self.mock.posts())
        backend.close()
        self.assertEqual(len(self.mock.posts()), posts)


def clean_env(**extra) -> dict:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("RGI_WLED_")}
    env.update(extra)
    return env


class TestAvailability(unittest.TestCase):
    def test_no_configuration_means_not_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "wled.json")
            with mock.patch.dict(os.environ,
                                 clean_env(RGI_WLED_CONFIG=missing), clear=True):
                self.assertFalse(WledBackend.available())

    def test_env_url_is_enough_even_when_unreachable(self):
        with mock.patch.dict(os.environ,
                             clean_env(RGI_WLED_URL="http://127.0.0.1:9"), clear=True):
            self.assertTrue(WledBackend.available())   # must not have touched it

    def test_config_file_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "wled.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"url": "wled.local", "segment": 3, "pixels": "1-2"}, fh)
            with mock.patch.dict(os.environ,
                                 clean_env(RGI_WLED_CONFIG=path), clear=True):
                self.assertTrue(WledBackend.available())
                backend = WledBackend()
                self.assertEqual(backend.url, "http://wled.local")
                self.assertEqual(backend.segment_id, 3)
                self.assertEqual(backend.pixels, [1, 2])

    def test_env_overrides_the_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "wled.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"url": "wled.local", "segment": 3}, fh)
            env = clean_env(RGI_WLED_CONFIG=path, RGI_WLED_URL="http://127.0.0.1:9",
                            RGI_WLED_SEGMENT="7")
            with mock.patch.dict(os.environ, env, clear=True):
                backend = WledBackend()
                self.assertEqual(backend.url, "http://127.0.0.1:9")
                self.assertEqual(backend.segment_id, 7)


class TestConfigParsing(unittest.TestCase):
    def test_pixel_specs(self):
        self.assertEqual(parse_pixels("0-3, 7"), [0, 1, 2, 3, 7])
        self.assertEqual(parse_pixels([0, "2-3"]), [0, 2, 3])
        self.assertEqual(parse_pixels([]), [])
        with self.assertRaises(ValueError):
            parse_pixels("odd")

    def test_bare_hosts_get_http(self):
        self.assertEqual(normalise_url("wled.local"), "http://wled.local")
        self.assertEqual(normalise_url("http://wled.local/"), "http://wled.local")
        self.assertEqual(normalise_url("ftp://wled.local"), "")
        self.assertEqual(normalise_url(None), "")


if __name__ == "__main__":
    unittest.main()
