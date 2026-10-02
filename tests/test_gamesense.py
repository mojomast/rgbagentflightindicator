"""SteelSeries GameSense against a local mock of the Engine HTTP API.

No Engine and no SteelSeries keyboard are needed: a stdlib ThreadingHTTPServer
records exactly what the backend posts and answers like Engine does. The tests
pin the three requests that matter - the bitmap handler binding, the 132-cell
``data.frame.bitmap`` frame, and the keepalive - plus what ``available()`` does
with a missing, stale or unreachable ``coreProps.json``.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from rgi.backends.base import BackendUnavailable
from rgi.backends.gamesense import (
    CORE_PROPS_ENV, GAME, GRID_CELLS, LANE_LABELS, LANE_TO_BITMAP,
    GamesenseBackend, engine_url, load_address,
)


def wait_for(predicate, timeout: float = 3.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class _EngineServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):     # keep hung tests quiet
        pass


class MockEngine:
    """A recording stand-in for SteelSeries Engine's JSON endpoint."""

    def __init__(self):
        self.requests: list[tuple[str, object]] = []
        self.status = 200
        self.lock = threading.Lock()
        self.server = _EngineServer(("127.0.0.1", 0), self._handler())
        self.address = f"127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)

    def posts(self, path: str | None = None) -> list:
        with self.lock:
            return [body for request_path, body in self.requests
                    if path is None or request_path == path]

    def _handler(self):
        mock_self = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):                            # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = None
                with mock_self.lock:
                    mock_self.requests.append((self.path, body))
                payload = json.dumps({"ok": True}).encode()
                self.send_response(mock_self.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        return Handler


class GamesenseCase(unittest.TestCase):
    def setUp(self):
        self.engine = MockEngine()
        self.addCleanup(self.engine.stop)
        self.tmp = tempfile.mkdtemp(prefix="rgi-gamesense-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.core = os.path.join(self.tmp, "coreProps.json")
        self.write_core_props(self.engine.address)

    def write_core_props(self, address, path=None) -> str:
        path = path or self.core
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"address": address}, fh)
        return path

    def make(self, **kwargs) -> GamesenseBackend:
        kwargs.setdefault("core_props", self.core)
        kwargs.setdefault("heartbeat_interval", 30.0)
        backend = GamesenseBackend(**kwargs)
        self.addCleanup(self._close_quietly, backend)
        return backend

    @staticmethod
    def _close_quietly(backend: GamesenseBackend) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            backend.close()

    @staticmethod
    def free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]


class TestProtocolShape(unittest.TestCase):
    def test_lane_to_bitmap_is_the_number_row(self):
        self.assertEqual(len(LANE_TO_BITMAP), len(LANE_LABELS))
        self.assertEqual(LANE_TO_BITMAP, tuple(range(22, 35)))
        for cell in LANE_TO_BITMAP:
            self.assertTrue(0 <= cell < GRID_CELLS)

    def test_lamps_expose_the_number_row_first(self):
        backend = GamesenseBackend()
        number_row = [lamp for lamp in backend.lamps()
                      if lamp.group == "number-row"]
        self.assertEqual([lamp.label for lamp in number_row], list(LANE_LABELS))
        self.assertTrue(backend.per_lamp)
        self.assertAlmostEqual(backend.min_interval, 0.1)
        # lamp indices are positions in lamps(), as the daemon assumes
        self.assertEqual([lamp.index for lamp in backend.lamps()],
                         list(range(len(backend.lamps()))))

    def test_every_lamp_index_is_contiguous_and_cells_map_one_to_one(self):
        backend = GamesenseBackend()
        cells = [cell for cell, label in enumerate(
            _layout_from_module()) if label is not None]
        self.assertEqual(len(cells), len(backend.lamps()))
        self.assertEqual(len(set(cells)), len(cells))


def _layout_from_module():
    from rgi.backends.gamesense import GAMESENSE_LAYOUT
    return GAMESENSE_LAYOUT


class TestOpen(GamesenseCase):
    def test_open_binds_the_bitmap_handler(self):
        backend = self.make()
        backend.open()
        binds = self.engine.posts("/bind_game_event")
        self.assertEqual(len(binds), 1)
        bind = binds[0]
        self.assertEqual(bind["game"], GAME)
        self.assertEqual(bind["event"], "LANES")
        self.assertTrue(bind["value_optional"])
        self.assertEqual(bind["handlers"],
                         [{"device-type": "rgb-per-key-zones", "mode": "bitmap"}])
        self.assertEqual(backend.address, self.engine.address)
        self.assertTrue(backend.http_url.startswith("http://127.0.0.1:"))

    def test_missing_core_props_is_unavailable(self):
        backend = self.make(core_props=os.path.join(self.tmp, "absent.json"))
        with self.assertRaises(BackendUnavailable) as caught:
            backend.open()
        self.assertIn("coreProps.json was not found", str(caught.exception))

    def test_broken_core_props_is_unavailable(self):
        path = os.path.join(self.tmp, "broken.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        backend = self.make(core_props=path)
        with self.assertRaises(BackendUnavailable) as caught:
            backend.open()
        self.assertIn("broken.json", str(caught.exception))

    def test_core_props_without_address_is_unavailable(self):
        path = os.path.join(self.tmp, "empty.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"foo": 1}, fh)
        backend = self.make(core_props=path)
        with self.assertRaises(BackendUnavailable):
            backend.open()

    def test_bind_failure_is_unavailable(self):
        self.engine.status = 500
        backend = self.make()
        with self.assertRaises(BackendUnavailable) as caught:
            backend.open()
        self.assertIn("did not accept", str(caught.exception))
        self.assertFalse(backend._open)

    def test_engine_address_is_parsed_and_normalised(self):
        self.assertEqual(engine_url("127.0.0.1:51000"), "http://127.0.0.1:51000")
        self.assertEqual(engine_url("http://127.0.0.1:51000/"),
                         "http://127.0.0.1:51000")
        self.assertEqual(engine_url(""), "")
        self.assertEqual(engine_url("not a url"), "")
        with open(self.core, encoding="utf-8") as fh:
            self.assertIn("address", fh.read())


class TestPainting(GamesenseCase):
    def setUp(self):
        super().setUp()
        self.backend = self.make()
        self.backend.open()
        self.lanes = self.backend.default_lanes(13)
        self.assertEqual(len(self.lanes), 13)

    def test_write_posts_a_132_cell_bitmap(self):
        frame = [(0, 0, 0)] * len(self.backend.lamps())
        for lane, colour in enumerate([(255, 0, 0), (0, 255, 0), (0, 0, 255)]
                                      * 5):
            if lane < len(self.lanes):
                frame[self.lanes[lane]] = colour
        self.backend.write(frame)

        events = self.engine.posts("/game_event")
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["game"], GAME)
        self.assertEqual(event["event"], "LANES")
        bitmap = event["data"]["frame"]["bitmap"]
        self.assertEqual(len(bitmap), GRID_CELLS)
        lit = set()
        for lane, position in enumerate(self.lanes):
            colour = frame[position]
            if colour != (0, 0, 0):
                cell = LANE_TO_BITMAP[lane]
                self.assertEqual(bitmap[cell], list(colour))
                lit.add(cell)
        for cell, colour in enumerate(bitmap):
            if cell not in lit:
                self.assertEqual(colour, [0, 0, 0], f"cell {cell} should be black")

    def test_lane_colours_land_on_the_number_row(self):
        frame = [(0, 0, 0)] * len(self.backend.lamps())
        frame[self.lanes[0]] = (1, 2, 3)                  # the backtick key
        frame[self.lanes[1]] = (4, 5, 6)                  # the "1" key
        self.backend.write(frame)
        bitmap = self.engine.posts("/game_event")[-1]["data"]["frame"]["bitmap"]
        self.assertEqual(bitmap[LANE_TO_BITMAP[0]], [1, 2, 3])
        self.assertEqual(bitmap[LANE_TO_BITMAP[1]], [4, 5, 6])

    def test_bitmap_frame_pads_short_frames_with_black(self):
        bitmap = self.backend.bitmap_frame([(9, 9, 9)])
        self.assertEqual(bitmap[0], [9, 9, 9])            # esc, the first lamp
        self.assertEqual(bitmap[LANE_TO_BITMAP[0]], [0, 0, 0])

    def test_write_after_close_is_refused(self):
        self.backend.close()
        with self.assertRaises(BackendUnavailable):
            self.backend.write([(0, 0, 0)] * len(self.backend.lamps()))

    def test_write_failure_raises_backend_unavailable(self):
        self.engine.status = 400
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(BackendUnavailable) as caught:
                self.backend.write([(0, 0, 0)] * len(self.backend.lamps()))
        self.assertIn("HTTP 400", str(caught.exception))

    def test_close_removes_the_event(self):
        self.backend.close()
        removals = self.engine.posts("/remove_game_event")
        self.assertEqual(removals, [{"game": GAME, "event": "LANES"}])
        self.backend.close()
        self.assertEqual(len(self.engine.posts("/remove_game_event")), 1)


class TestHeartbeat(GamesenseCase):
    def test_keepalive_posts_while_open(self):
        backend = self.make(heartbeat_interval=0.15)
        backend.open()
        self.assertTrue(wait_for(lambda: self.engine.posts("/game_heartbeat")))
        heartbeat = self.engine.posts("/game_heartbeat")[0]
        self.assertEqual(heartbeat, {"game": GAME})
        backend.close()
        # after close the thread is stopped; Engine must fall back by timeout
        self.assertFalse(backend._open)

    def test_a_failed_heartbeat_does_not_raise(self):
        backend = self.make(heartbeat_interval=0.15)
        backend.open()
        self.engine.status = 500
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(wait_for(lambda: self.engine.posts("/game_heartbeat")))
            time.sleep(0.3)
        self.assertTrue(backend._open)


class TestAvailability(GamesenseCase):
    def test_available_when_looking_at_a_live_engine(self):
        with mock.patch.dict(os.environ, {CORE_PROPS_ENV: self.core}):
            self.assertTrue(GamesenseBackend.available())

    def test_unavailable_when_the_file_is_missing(self):
        with mock.patch.dict(os.environ,
                             {CORE_PROPS_ENV: os.path.join(self.tmp, "no.json")}):
            self.assertFalse(GamesenseBackend.available())

    def test_unavailable_when_the_address_is_stale(self):
        path = os.path.join(self.tmp, "stale.json")
        self.write_core_props(f"127.0.0.1:{self.free_port()}", path)
        with mock.patch.dict(os.environ, {CORE_PROPS_ENV: path}):
            self.assertFalse(GamesenseBackend.available())

    def test_unavailable_when_the_file_is_garbage(self):
        path = os.path.join(self.tmp, "garbage.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("not json")
        with mock.patch.dict(os.environ, {CORE_PROPS_ENV: path}):
            self.assertFalse(GamesenseBackend.available())

    def test_available_never_raises(self):
        with mock.patch.dict(os.environ, {CORE_PROPS_ENV: self.core}):
            with mock.patch("rgi.backends.gamesense._reachable",
                            side_effect=RuntimeError("boom")):
                self.assertFalse(GamesenseBackend.available())

    def test_load_address_reads_the_documented_key(self):
        self.assertEqual(load_address(self.core), self.engine.address)
        with self.assertRaises(OSError):
            load_address(os.path.join(self.tmp, "no.json"))


if __name__ == "__main__":
    unittest.main()
