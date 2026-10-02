"""``rgi hook <harness>``: one lifecycle event, one process, always exit 0.

A harness runs a hook as a child process and reads its exit code as a decision.
That makes the contract narrow and absolute: read one JSON payload from stdin,
report it, write nothing to stdout unless the harness asked for a decision, and
exit 0 even when everything is broken. A status panel that fails must never
change what the agent does - including when the harness name is wrong, because
the alternative is blocking the agent with exit code 2.

``rgi hook --source <harness>`` is the generic shim: it POSTs the payload
unchanged to the panel's ``/hook/<source>`` endpoint, where the server-side
normalizer maps it. Harnesses with native HTTP hooks do not need this at all.

Diagnostics go to stderr, and only when RGI_HOOK_DEBUG=1 (or --debug).
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import urllib.request

# Hook-based harnesses: names accepted by `rgi hook <name>`.
HOOKS: dict[str, str] = {
    "claude-code": "rgi.integrations.claude_code",
    "gemini-cli": "rgi.integrations.gemini_cli",
}


def read_payload(stream=None) -> dict:
    """One JSON object from stdin; anything unreadable becomes an empty dict."""
    stream = stream if stream is not None else sys.stdin
    try:
        raw = stream.read()
    except (OSError, ValueError):
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def run(name: str, argv: list[str] | None = None) -> int:
    """Report one event. Returns 0 unless the caller asks for --help."""
    del argv                                     # reserved for future flags
    module_name = HOOKS.get(name)
    if module_name is None:
        print(f"rgi hook: unknown harness {name!r}; known: "
              f"{', '.join(sorted(HOOKS))}", file=sys.stderr)
        return 0                                 # never block the agent
    try:
        module = importlib.import_module(module_name)
        handler = module.hook
    except (ImportError, AttributeError) as exc:
        print(f"rgi hook: {name} is unavailable here: {exc}", file=sys.stderr)
        return 0
    payload = read_payload()
    try:
        return int(handler(payload) or 0)
    except Exception as exc:                     # noqa: BLE001 - never break a run
        print(f"rgi hook: {name}: {exc}", file=sys.stderr)
        return 0


def _debug(message: str) -> None:
    if os.environ.get("RGI_HOOK_DEBUG") == "1":
        print(f"rgi hook: {message}", file=sys.stderr)


def run_source(source: str, url: str | None = None, token: str | None = None,
               payload: dict | None = None, timeout: float = 2.0) -> int:
    """Send one native payload to ``/hook/<source>``; always exit 0.

    The server does the mapping, so adding a harness is a mapping table on the
    daemon, not another client binary. No stdout: a command hook's output can
    be read as a decision by some harnesses.
    """
    payload = payload if payload is not None else read_payload()
    if not payload:
        _debug("empty payload; nothing sent")
        return 0
    from .config import resolve_token, resolve_url

    target = f"{resolve_url(url).rstrip('/')}/hook/{source}"
    body = json.dumps(payload).encode()
    request = urllib.request.Request(target, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    secret = resolve_token(token)
    if secret:
        request.add_header("X-LED-Token", secret)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
        _debug(f"sent {source} event to {target}")
    except Exception as exc:                     # noqa: BLE001 - never break a run
        _debug(f"panel unreachable ({exc}); event dropped")
    return 0
