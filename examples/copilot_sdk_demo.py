"""Report a GitHub Copilot SDK session to the panel -- a runnable-shaped demo.

With ``github-copilot-sdk`` installed (``pip install github-copilot-sdk``,
Python 3.11+) this creates a real session and mirrors it onto a lane.
Without it, the same wiring is driven against a tiny fake session, so the
shape is still visible.

    python examples/copilot_sdk_demo.py

The panel address and token come from ``RGI_URL`` / ``RGI_TOKEN``, then from
``~/.config/rgi/url`` and ``~/.config/rgi/token``, then from localhost. If no
panel is listening, reporting is quietly skipped: an indicator that is down
must never break the application.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

try:                                            # third-party, optional
    from copilot import CopilotClient
    from copilot.session import PermissionHandler
    HAS_SDK = True
except ImportError:
    CopilotClient = None                        # type: ignore[assignment]
    PermissionHandler = None                    # type: ignore[assignment]
    HAS_SDK = False

from rgi.integrations.copilot import CopilotReporter


class FakeSession:
    """Stands in for ``copilot.CopilotSession`` when the SDK is absent."""

    session_id = "demo-fake-session"

    def __init__(self) -> None:
        self._handlers = []

    def on(self, handler):                       # same shape as the SDK
        self._handlers.append(handler)
        return lambda: self._handlers.remove(handler)

    def emit(self, event_type, data=None) -> None:
        event = SimpleNamespace(type=event_type, data=data, timestamp=None)
        for handler in list(self._handlers):
            handler(event)


async def run_with_sdk() -> None:
    async with CopilotClient() as client:
        session = await client.create_session(
            on_permission_request=PermissionHandler.approve_all,
            model="gpt-5",
        )
        # If the application already has its own permission handler, keep it and
        # hand the adapter a wrapper instead -- the handler decides, as before:
        #
        #   from copilot.session import PermissionHandler
        #   adapter.wrap_permission_handler(PermissionHandler.approve_all)
        adapter = CopilotReporter(session.session_id, label="copilot sdk demo")
        adapter.attach(session)
        try:
            answer = await session.send_and_wait("What is 2 + 2?")
            if answer is not None:
                print("answer:", answer.data.content)
        finally:
            adapter.close()


def run_without_sdk() -> None:
    print("github-copilot-sdk is not installed; driving the fake session instead.")
    fake = FakeSession()
    adapter = CopilotReporter(fake.session_id, label="copilot sdk demo (fake)")
    adapter.attach(fake)
    try:
        fake.emit("user.message", SimpleNamespace(content="What is 2 + 2?"))
        fake.emit("assistant.turn_start", SimpleNamespace())
        fake.emit("permission.requested", SimpleNamespace(
            request_id="perm-1",
            permission_request=SimpleNamespace(kind="shell",
                                               full_command_text="python -m pytest")))
        fake.emit("permission.completed",
                  SimpleNamespace(request_id="perm-1"))
        fake.emit("assistant.message", SimpleNamespace(content="4"))
        fake.emit("session.task_complete", SimpleNamespace(success=True))
        fake.emit("session.idle", SimpleNamespace())
    finally:
        adapter.close()


def main() -> int:
    if HAS_SDK:
        asyncio.run(run_with_sdk())
    else:
        run_without_sdk()
    return 0


if __name__ == "__main__":
    sys.exit(main())
