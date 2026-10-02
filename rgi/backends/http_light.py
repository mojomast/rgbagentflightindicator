"""Generic HTTP-light sink - one lamp for devices that answer HTTP.

Single-colour desk lights increasingly expose a small local HTTP contract
instead of a documented HID protocol: blink(1)'s ``blink1-tiny-server`` on
:8934, busylight's FastAPI server on :8000, Luxafor's local webhook, a lamp in
front of Home Assistant, an ESPHome ``rest_command``. This backend is the
adapter: it renders one colour into a configurable request.

    open()   probe the endpoint with a cheap GET; raise if nothing answers
    write()  render the template with the most urgent lane's colour and send
    close()  nothing to restore - see below

``per_lamp = False``, so the daemon collapses all lanes to the single most
urgent colour before calling ``write()`` (see daemon.Device.frame); this
backend therefore always paints the first colour it is given. ``lamps()``
returns ``count`` identical entries (default 12) so the daemon can track that
many sessions on the one light - the same honest pattern the QMK
single-colour path uses. That keeps the honesty rule from the README: one
addressable light means one lane's colour, not twelve fake lamps.

Templates. The URL and body are rendered with the placeholders::

    {r} {g} {b}    decimal channels, 0-255
    {hex}          uppercase RRGGBB, for devices that take "23FF00" or "%23FF00"
    {on}           the JSON literals true/false (black counts as off)

Rendering is a literal substitution of exactly those tokens - ``str.format``
is deliberately not used, so ``{r.__class__}`` and friends stay untouched and
a template can never reach into the process. A dict body is always sent as
JSON; a string body is sent as JSON when it renders to a JSON object or
array, and as the literal text otherwise.

Presets fill in a known request shape, then explicit arguments override it:
``blink1`` is ``GET http://127.0.0.1:8934/blink1/fadeToRGB?rgb=%23{hex}``;
``busylight`` is a JSON POST to ``http://127.0.0.1:8000/api/v1/lights/on``
with ``{"color": "#{hex}", "dim": 1.0, "led": 0}``.

Configuration: explicit arguments, then ``RGI_HTTP_LIGHT_URL`` and
``RGI_HTTP_LIGHT_PRESET``. ``available()`` pings the configured endpoint and
never raises; no configuration means not available. Every failed request
raises ``BackendUnavailable`` so the daemon logs it, closes and reopens the
backend, and never pretends the light was painted. ``close()`` sends nothing:
the last colour stays as the light's honest state until the panel paints
again.

Verified against local mock servers only - no blink(1), busylight or other
lamp hardware was used. See docs/http-light.md.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Sequence

from .base import OFF, RGB, Backend, BackendUnavailable, Lamp

DEFAULT_TIMEOUT = 1.5
PROBE_TIMEOUT = 0.6
DEFAULT_METHOD = "POST"

URL_ENV = "RGI_HTTP_LIGHT_URL"
PRESET_ENV = "RGI_HTTP_LIGHT_PRESET"

#: A known request shape per device family. ``url`` may contain placeholders;
#: tests replace only the host:port to point a preset at a mock.
PRESETS: dict[str, dict] = {
    "blink1": {
        "url": "http://127.0.0.1:8934/blink1/fadeToRGB?rgb=%23{hex}",
        "method": "GET",
        "headers": {},
        "body_template": None,
    },
    "busylight": {
        "url": "http://127.0.0.1:8000/api/v1/lights/on",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "body_template": {"color": "#{hex}", "dim": 1.0, "led": 0},
    },
}

_TOKEN = re.compile(r"\{(r|g|b|hex|on)\}")


def render(template: str, context: dict) -> str:
    """Replace exactly the documented tokens; everything else is literal."""
    return _TOKEN.sub(lambda match: context[match.group(1)], template)


def _context(rgb: RGB) -> dict:
    r, g, b = rgb
    return {
        "r": str(r),
        "g": str(g),
        "b": str(b),
        "hex": f"{r:02X}{g:02X}{b:02X}",
        "on": "true" if rgb != (0, 0, 0) else "false",
    }


def normalise_url(raw) -> str:
    """A usable http(s) URL, or "" if ``raw`` cannot be one. Adds http://."""
    if not isinstance(raw, str) or not raw.strip():
        return ""
    raw = raw.strip()
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return raw


def _clean_rgb(colour) -> RGB:
    out = []
    for channel in tuple(colour)[:3]:
        try:
            value = int(channel)
        except (TypeError, ValueError):
            value = 0
        out.append(max(0, min(255, value)))
    while len(out) < 3:
        out.append(0)
    return out[0], out[1], out[2]


def _has_header(headers: dict, name: str) -> bool:
    wanted = name.lower()
    return any(str(key).lower() == wanted for key in headers)


def _render_value(value, context: dict):
    if isinstance(value, str):
        return render(value, context)
    if isinstance(value, dict):
        return {key: _render_value(item, context) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_render_value(item, context) for item in value]
    return value


def _json_body(rendered: str):
    """The parsed JSON object/array in ``rendered``, or None.

    A string body is treated as JSON only when it actually renders to a JSON
    object or array; anything else - ``{hex}`` alone, plain text, a malformed
    snippet - is sent as the literal body. That keeps ``{hex}`` usable as a
    bare hex body and means unknown placeholders are never reinterpreted.
    """
    stripped = rendered.strip()
    if not stripped.startswith(("{", "[")):
        return None
    try:
        parsed = json.loads(stripped)
    except ValueError:
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


def _probe(url: str, timeout: float = PROBE_TIMEOUT) -> bool:
    """True when something HTTP answers at the URL's origin.

    Any status counts, including 404: the question is "is the service there",
    not "is this path valid". The probe never touches the templated endpoint,
    so it cannot change the light.
    """
    parts = urllib.parse.urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}/"
    request = urllib.request.Request(base, method="GET",
                                     headers={"Accept": "*/*"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            response.read(256)
        return True
    except urllib.error.HTTPError:
        return True                                    # an HTTP answer is enough
    except Exception:
        return False


class HttpLightBackend(Backend):
    name = "http-light"
    min_interval = 0.05
    per_lamp = False

    def __init__(self, url=None, method=None, headers=None, body_template=None,
                 preset=None, timeout: float = DEFAULT_TIMEOUT,
                 probe_timeout: float = PROBE_TIMEOUT, count: int = 12,
                 verbose: bool = False):
        preset_name = preset or os.environ.get(PRESET_ENV) or None
        if preset_name and preset_name not in PRESETS:
            raise ValueError(
                f"unknown http-light preset {preset_name!r}; "
                f"known: {', '.join(sorted(PRESETS))}"
            )
        spec = PRESETS.get(preset_name, {})

        raw_url = url or os.environ.get(URL_ENV) or spec.get("url")
        self.preset = preset_name
        self.url = normalise_url(raw_url)
        self._configured = bool(raw_url)
        self._config_error: str | None = None
        if self._configured and not self.url:
            self._config_error = f"http-light URL {raw_url!r} is not a valid http(s) address"

        self.method = str(method or spec.get("method") or DEFAULT_METHOD).upper()
        self.headers = dict(spec.get("headers") or {})
        self.headers.update(dict(headers or {}))
        self.body_template = (body_template if body_template is not None
                              else spec.get("body_template"))
        self.timeout = self._positive(timeout, DEFAULT_TIMEOUT)
        self.probe_timeout = self._positive(probe_timeout, PROBE_TIMEOUT)
        self.verbose = verbose
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 12
        self.count = max(1, count)
        # One physical light, but `count` identical lamps so the daemon can
        # track that many lanes and collapse them to the most urgent colour -
        # the same honest pattern the QMK single-colour path uses.
        self._lamps = [Lamp(index=i, label="light", group="light")
                       for i in range(self.count)]
        self._open = False
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @staticmethod
    def _positive(value, fallback: float) -> float:
        try:
            value = float(value)
        except (TypeError, ValueError):
            return fallback
        return value if value > 0 else fallback

    # -- discovery ---------------------------------------------------------
    @classmethod
    def available(cls) -> bool:
        """Ping a configured endpoint with a cheap GET; never raises."""
        try:
            url = normalise_url(os.environ.get(URL_ENV))
            if not url:
                preset = os.environ.get(PRESET_ENV) or ""
                url = normalise_url(PRESETS.get(preset, {}).get("url"))
            return bool(url) and _probe(url, PROBE_TIMEOUT)
        except Exception:
            return False

    # -- lifecycle ---------------------------------------------------------
    def probe(self) -> bool:
        """Whether this instance's endpoint answers right now."""
        return bool(self.url) and _probe(self.url, self.probe_timeout)

    def open(self) -> None:
        self.close()
        if not self.url:
            if self._config_error:
                raise BackendUnavailable(self._config_error)
            raise BackendUnavailable(
                "http-light is not configured: pass url=..., use "
                f"--http-light-url, or set {URL_ENV} (see docs/http-light.md)"
            )
        if not self.probe():
            origin = urllib.parse.urlsplit(self.url)
            raise BackendUnavailable(
                f"no HTTP answer from {origin.scheme}://{origin.netloc}/ - is "
                "the light service running? (docs/http-light.md)"
            )
        self._open = True
        if self.verbose:
            print(f"[http-light] {self.method} {self.url}")

    def close(self) -> None:
        self._open = False

    def lamps(self) -> list[Lamp]:
        return list(self._lamps)

    # -- painting ----------------------------------------------------------
    def _body(self, context: dict) -> tuple[bytes | None, bool]:
        """(body, is_json) for this frame, or (None, False) for a bare URL."""
        template = self.body_template
        if template is None:
            return None, False
        if isinstance(template, dict):
            rendered = _render_value(template, context)
            return json.dumps(rendered, separators=(",", ":")).encode("utf-8"), True
        rendered = render(str(template), context)
        parsed = _json_body(rendered)
        if parsed is not None:
            return json.dumps(parsed, separators=(",", ":")).encode("utf-8"), True
        return rendered.encode("utf-8"), False

    def write(self, colours: Sequence[RGB]) -> None:
        if not self._open:
            raise BackendUnavailable("http-light backend is not open")
        frame = list(colours)
        # per_lamp = False: the daemon collapses to the most urgent colour and
        # repeats it for every lamp; the first entry is that colour.
        rgb = _clean_rgb(frame[0]) if frame else OFF
        context = _context(rgb)
        url = render(self.url, context)
        body, is_json = self._body(context)
        headers = dict(self.headers)
        if body is not None and not _has_header(headers, "Content-Type"):
            headers["Content-Type"] = ("application/json" if is_json
                                       else "text/plain; charset=utf-8")
        request = urllib.request.Request(url, data=body, method=self.method,
                                         headers=headers)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = getattr(response, "status", None) or response.getcode()
                response.read(1024)
        except urllib.error.HTTPError as exc:
            raise BackendUnavailable(
                f"http-light: {self.method} {url}: HTTP {exc.code}"
            ) from exc
        except Exception as exc:
            raise BackendUnavailable(
                f"http-light: {self.method} {url}: {exc}"
            ) from exc
        if not 200 <= int(status) < 300:
            raise BackendUnavailable(
                f"http-light: {self.method} {url}: HTTP {status}"
            )
        if self.verbose:
            print(f"[http-light] {self.method} {url} -> {rgb}")
