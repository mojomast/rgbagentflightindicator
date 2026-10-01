"""Report CrewAI crew and flow events to the panel -- a runnable-shaped demo.

With ``crewai`` installed (``pip install crewai``) this attaches to the real
process-wide event bus and emits the lifecycle events a kickoff would emit --
no LLM call, no API key -- so the wiring is visible end to end. Without the
package, the same events are handed to the adapter directly.

    python examples/crewai_demo.py

The panel address and token come from ``RGI_URL`` / ``RGI_TOKEN``, then from
``~/.config/rgi/url`` and ``~/.config/rgi/token``, then from localhost. If no
panel is listening, reporting is quietly skipped: an indicator that is down
must never break the application.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

try:                                            # third-party, optional
    from crewai.events import crewai_event_bus
    from crewai.events.types.flow_events import (
        FlowFinishedEvent,
        FlowStartedEvent,
        HumanFeedbackReceivedEvent,
        HumanFeedbackRequestedEvent,
    )

    HAS_CREWAI = True
except ImportError:
    crewai_event_bus = None                     # type: ignore[assignment]
    FlowStartedEvent = None                     # type: ignore[assignment]
    FlowFinishedEvent = None                    # type: ignore[assignment]
    HumanFeedbackRequestedEvent = None          # type: ignore[assignment]
    HumanFeedbackReceivedEvent = None           # type: ignore[assignment]
    HAS_CREWAI = False

from rgi.integrations.crewai import CrewAIReporter, listen


def run_with_crewai() -> None:
    # `listen()` registers on `crewai_event_bus` for the block; every kickoff
    # or flow that runs inside it appears as a lane of its own.
    def emit(source, event) -> None:
        """Emit and wait for the bus's handlers, so the prints are in order."""
        future = crewai_event_bus.emit(source, event)
        if future is not None:
            try:
                future.result(timeout=5)
            except Exception:                    # noqa: BLE001 - demo only
                pass

    with listen(label="crewai demo") as panel:
        source = SimpleNamespace(name="demo")
        started = FlowStartedEvent(flow_name="demo flow", inputs={})
        emit(source, started)

        request = HumanFeedbackRequestedEvent(
            flow_name="demo flow",
            method_name="review",
            output="first draft",
            message="Publish the draft?",
            request_id="demo-feedback-1",
            parent_event_id=started.event_id,
        )
        emit(source, request)
        print("the lane is blocked on demo-feedback-1")

        emit(source, HumanFeedbackReceivedEvent(
            flow_name="demo flow",
            method_name="review",
            feedback="yes",
            request_id="demo-feedback-1",
        ))
        emit(source, FlowFinishedEvent(
            flow_name="demo flow",
            result="published",
            state={},
        ))
        print("the lane is done; runs seen:", sorted(panel.runs))


def run_without_crewai() -> None:
    print("crewai is not installed; driving fake events instead.")
    panel = CrewAIReporter(label="crewai demo (fake)")
    panel.observe(SimpleNamespace(type="crew_kickoff_started", event_id="demo-kick",
                                  crew_name="demo crew"))
    panel.observe(SimpleNamespace(type="human_feedback_requested", event_id="demo-fb",
                                  request_id="demo-feedback-1",
                                  method_name="review",
                                  message="Publish the draft?",
                                  parent_event_id="demo-kick"))
    print("the lane is blocked on demo-feedback-1")
    panel.observe(SimpleNamespace(type="human_feedback_received", event_id="demo-fb-2",
                                  request_id="demo-feedback-1",
                                  method_name="review"))
    panel.observe(SimpleNamespace(type="crew_kickoff_completed", event_id="demo-done",
                                  started_event_id="demo-kick",
                                  crew_name="demo crew"))
    key = "crewai:demo-kick"
    run = panel.run("demo-kick")
    print("lane", key, "finished:", run.finished if run else False)


def main() -> int:
    if HAS_CREWAI:
        run_with_crewai()
    else:
        run_without_crewai()
    return 0


if __name__ == "__main__":
    sys.exit(main())
