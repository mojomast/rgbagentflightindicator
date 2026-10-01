"""Private OpenAI MCP adapter. The optional SDK is imported only at startup.

The adapter speaks stdio to a local host or Secure MCP Tunnel and makes bounded
requests to the loopback panel. It never starts hardware or exposes a listener.
"""

from __future__ import annotations

import asyncio
import datetime
import ipaddress
import json
import math
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from .daemon import resolve_token

SESSION_PATTERN = r"^[A-Za-z0-9_-]{1,96}$"
NAMESPACE_PATTERN = r"^[A-Za-z0-9_-]{1,32}$"
STATES = ["working", "done", "blocked", "error", "idle"]
MAX_RESPONSE_BYTES = 1024 * 1024


class PanelError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A local service must not redirect the panel credential elsewhere.
        return None


class PanelClient:
    def __init__(self, url: str = "http://127.0.0.1:8730", *,
                 namespace: str = "private", timeout: float = 2.0):
        parsed = urllib.parse.urlsplit(url)
        try:
            loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
            parsed.port
        except ValueError:
            loopback = False
        if (parsed.scheme not in ("http", "https") or not loopback
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("MCP panel URL must be a loopback IP origin, "
                             "such as http://127.0.0.1:8730")
        if not re.fullmatch(NAMESPACE_PATTERN, namespace):
            raise ValueError("namespace must be 1-32 letters, digits, underscores or hyphens")
        if not math.isfinite(timeout) or not 0.1 <= timeout <= 10:
            raise ValueError("timeout must be between 0.1 and 10 seconds")
        self.url = url.rstrip("/")
        self.namespace = namespace
        self.prefix = f"rgi-openai:{namespace}:"
        self.timeout = timeout
        self.lock = threading.RLock()
        # Loopback calls must not be routed through an inherited Internet proxy.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def _sid(self, session_id: str) -> str:
        if not isinstance(session_id, str) or not re.fullmatch(SESSION_PATTERN, session_id):
            raise ValueError("session_id must be 1-96 letters, digits, underscores or hyphens")
        return self.prefix + session_id

    def _request(self, path: str, payload: dict | None = None) -> dict:
        headers = {"Accept": "application/json"}
        # Resolve on every call so a locally rotated credential can be reused.
        credential = resolve_token(None)
        if credential:
            headers["X-LED-Token"] = credential
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url + path, data=body, headers=headers)
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            errors = {
                401: ("panel_auth_failed", "Panel authentication failed; check the local RGI credential."),
                404: ("session_not_found", "Session not found; call session_start with the same ID to reclaim it."),
                409: ("lane_unavailable", "The requested lane is unavailable; inspect panel_status."),
            }
            kind, message = errors.get(code, ("panel_http_error", f"Panel returned HTTP {code}."))
            raise PanelError(kind, message) from None
        except (TimeoutError, urllib.error.URLError, OSError):
            # A failed response is not proof that a write was never applied.
            message = "Panel unreachable or timed out."
            if payload is not None:
                message += " Write outcome is unknown; inspect panel_status before retrying."
            raise PanelError("panel_unreachable", message, retryable=payload is None) from None
        except (ValueError, UnicodeError):
            raise PanelError("panel_auth_failed", "Invalid local panel request or credential.") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise PanelError("panel_invalid_response", "Panel response exceeded the size limit.")
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise PanelError("panel_invalid_response", "Panel did not return valid JSON.") from None
        if not isinstance(data, dict):
            raise PanelError("panel_invalid_response", "Panel response must be a JSON object.")
        return data

    def panel_status(self) -> dict:
        with self.lock:
            data = self._request("/status")
            sessions = []
            for sid, lane in data["sessions"].items():
                if sid.startswith(self.prefix):
                    sessions.append({"session_id": sid[len(self.prefix):],
                                     **{k: lane[k] for k in ("slot", "key", "label", "state", "age")}})
            # Do not publish other agents' labels, hosts, paths, or loose metadata.
            devices = [{k: d[k] for k in ("label", "backend", "lamps", "lanes")}
                       for d in data["devices"]]
            return {"online": True, "namespace": self.namespace, "devices": devices,
                    "free": data["free"], "sessions": sessions,
                    "observed_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}

    def session_start(self, session_id: str, label: str, slot: int | None = None) -> dict:
        sid = self._sid(session_id)
        if (not isinstance(label, str) or not label.strip() or len(label) > 80
                or any(ord(c) < 32 or ord(c) == 127 for c in label)):
            raise ValueError("label must be 1-80 printable characters")
        if slot is not None and (type(slot) is not int or not 0 <= slot <= 255):
            raise ValueError("slot must be an integer between 0 and 255")
        with self.lock:
            try:
                lane = self._request("/session/" + sid)
            except PanelError as exc:
                if exc.code != "session_not_found":
                    raise
            else:
                if slot is not None and slot != lane["slot"]:
                    raise PanelError("lane_unavailable", "Session already holds a different lane.")
                return {"session_id": session_id, "slot": lane["slot"],
                        "key": lane["key"], "state": lane["state"]}
            free = self._request("/status")["free"]
            if slot is None:
                if not free:
                    raise PanelError("lane_unavailable", "No free lanes; existing sessions were preserved.")
                slot = free[0]
            # An explicit slot prevents the daemon evicting another agent's lane.
            lane = self._request("/session/start", {"sessionID": sid, "label": label,
                                 "agent": "openai", "ident": self.namespace, "slot": slot})
            return {"session_id": session_id, "slot": lane["slot"],
                    "key": lane["key"], "state": "idle"}

    def session_set_state(self, session_id: str, state: str) -> dict:
        sid = self._sid(session_id)
        if state not in STATES:
            raise ValueError("state must be working, done, blocked, error or idle")
        with self.lock:
            data = self._request("/session/state", {"sessionID": sid, "state": state})
            if data.get("ok") is not True:
                raise PanelError("panel_invalid_response", "Panel did not acknowledge the state update.")
        return {"session_id": session_id, "state": state, "ok": True}

    def session_end(self, session_id: str) -> dict:
        sid = self._sid(session_id)
        with self.lock:
            data = self._request("/session/end", {"sessionID": sid})
            if data.get("ok") is not True:
                raise PanelError("panel_invalid_response", "Panel did not acknowledge the release.")
        return {"session_id": session_id, "ok": True}


def create_server(client: PanelClient):
    from mcp.server.lowlevel import Server
    from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations

    server = Server("rgi-panel", version=__version__, instructions=(
        "Use panel_status to inspect the private RGB panel. Use one stable session_id per task. "
        "Claim with session_start, then report state transitions. Hold done until the user "
        "closes the session; session_end releases the lane. Panel failures must not block real work. "
        "Only report actual tool results; this server does not monitor ChatGPT lifecycle events."
    ))
    session = {"type": "string", "pattern": SESSION_PATTERN}
    state = {"type": "string", "enum": STATES}
    definitions = [
        ("panel_status", "Read RGB panel", "Read live devices, free lanes and this adapter's sessions.", {}, [],
         {"online": {"const": True}, "namespace": {"type": "string"},
          "devices": {"type": "array", "items": {"type": "object"}},
          "free": {"type": "array", "items": {"type": "integer"}},
          "sessions": {"type": "array", "items": {"type": "object"}},
          "observed_at": {"type": "string"}}, True),
        ("session_start", "Claim RGB lane", "Claim a lane for a task; reuse the same ID on retry. Does not evict other sessions.",
         {"session_id": session, "label": {"type": "string", "minLength": 1, "maxLength": 80},
          "slot": {"type": "integer", "minimum": 0, "maximum": 255}}, ["session_id", "label"],
         {"session_id": session, "slot": {"type": "integer"}, "key": {"type": "string"}, "state": state}, False),
        ("session_set_state", "Set RGB session state", "Set working, done, blocked, error or idle for a claimed task.",
         {"session_id": session, "state": state}, ["session_id", "state"],
         {"session_id": session, "state": state, "ok": {"const": True}}, False),
        ("session_end", "Release RGB lane", "Release this task's lane when its session closes. Safe to repeat.",
         {"session_id": session}, ["session_id"],
         {"session_id": session, "ok": {"const": True}}, False),
    ]

    @server.list_tools()
    async def list_tools():
        return [Tool(name=name, title=title, description=description,
                     inputSchema={"type": "object", "properties": inputs,
                                  "required": required, "additionalProperties": False},
                     outputSchema={"type": "object", "properties": outputs,
                                   "required": list(outputs), "additionalProperties": False},
                     annotations=ToolAnnotations(readOnlyHint=read_only, destructiveHint=False,
                                                 idempotentHint=True, openWorldHint=False))
                for name, title, description, inputs, required, outputs, read_only in definitions]

    methods = {name: getattr(client, name) for name, *_ in definitions}

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        try:
            if name not in methods:
                raise ValueError("Unknown tool")
            data = await asyncio.to_thread(methods[name], **arguments)
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(data))],
                                  structuredContent=data)
        except (PanelError, ValueError) as exc:
            error = {"code": exc.code if isinstance(exc, PanelError) else "invalid_input",
                     "message": str(exc), "retryable": getattr(exc, "retryable", False)}
        except Exception:
            # No upstream body, credential or traceback is returned to the model.
            error = {"code": "panel_invalid_response", "message": "Unexpected panel response.",
                     "retryable": False}
        return CallToolResult(content=[TextContent(type="text", text=json.dumps({"error": error}))],
                              isError=True)

    return server


async def serve(client: PanelClient) -> None:
    from mcp.server.stdio import stdio_server

    server = create_server(client)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
