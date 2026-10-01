"""Report a Microsoft Agent Framework workflow to the panel -- runnable-shaped.

With ``agent-framework`` installed (``pip install agent-framework``) this builds
a tiny workflow whose executor asks a human for input with
``ctx.request_info``: the lane reports ``blocked`` under the request id, the
response is sent with ``workflow.run(responses={...})`` on the same session,
and the terminal ``IDLE`` completes the lane. Without the package, fake events
drive the same adapter.

    python examples/ms_agent_demo.py

The agent middleware shape is shown in comments: ``agent.run(...,
middleware=[run.middleware])`` is the root boundary for a plain agent run, and
an approval request in the response blocks the lane until it is answered.

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
    from agent_framework import (
        Executor,
        WorkflowBuilder,
        WorkflowContext,
        handler,
        response_handler,
    )

    HAS_FRAMEWORK = True
except ImportError:
    Executor = object                          # type: ignore[assignment,misc]
    WorkflowBuilder = None                     # type: ignore[assignment]
    WorkflowContext = None                     # type: ignore[assignment]
    handler = response_handler = None          # type: ignore[assignment]
    HAS_FRAMEWORK = False

from rgi.integrations.ms_agent import report_run

SESSION = "ms-agent-demo"


async def run_with_framework() -> None:
    class Approver(Executor):
        @handler
        async def ask(self, message: str, ctx: WorkflowContext[str]) -> None:
            await ctx.request_info(f"publish {message}?", str)

        @response_handler
        async def answered(self, original_request, response: str,
                           ctx: WorkflowContext[str]) -> None:
            await ctx.yield_output(f"answer={response}")

    workflow = WorkflowBuilder(start_executor=Approver(id="approver")).build()

    with report_run(SESSION, label="agent framework demo") as run:
        request_id = None
        async for event in workflow.run("the draft", stream=True):
            run.observe(event)                 # request_info -> blocked
            if getattr(event, "type", "") == "request_info":
                request_id = event.request_id
        print("blocked; pending =", run.pending)

        if request_id:
            run.answer(request_id)             # the human answered
            result = await workflow.run(responses={request_id: "yes"})
            run.observe_result(result)         # IDLE -> done
    print("lane is done")


# A plain agent run uses the same lane through its middleware:
#
#   with report_run("ticket-42", label="triage") as run:
#       response = await agent.run(prompt, middleware=[run.middleware])
#       run.observe(response)                  # approval -> blocked
#       for request in response.user_input_requests:
#           run.answer(request.id)


class FakeWorkflowEvent:
    def __init__(self, type, **fields):
        self.type = type
        for name, value in fields.items():
            setattr(self, name, value)


class FakeContext:
    def __init__(self, result=None):
        self.result = result
        self.metadata = {}


async def _noop() -> None:
    return None


def run_without_framework() -> None:
    print("agent-framework is not installed; driving fake events instead.")
    with report_run(SESSION, label="agent framework demo (fake)") as run:
        asyncio.run(run.middleware.process(FakeContext(None), _noop))
        run.observe(FakeWorkflowEvent("executor_invoked", executor_id="approver"))
        run.observe(FakeWorkflowEvent("request_info", request_id="demo-req-1",
                                      source_executor_id="approver",
                                      data="publish the draft?"))
        print("blocked; pending =", run.pending)
        run.answer("demo-req-1")
        run.observe(FakeWorkflowEvent("executor_completed", executor_id="approver"))
        run.observe(FakeWorkflowEvent("status", state="IDLE"))
    print("lane is done")


def main() -> int:
    if HAS_FRAMEWORK:
        asyncio.run(run_with_framework())
    else:
        run_without_framework()
    return 0


if __name__ == "__main__":
    sys.exit(main())
