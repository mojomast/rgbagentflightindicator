"""Run one Pydantic AI workflow with a lamp on the rbgafi panel.

The panel comes from the environment, exactly like every other rgi client:

    RGI_URL=http://<panel-host>:8730 RGI_TOKEN=$TOKEN \
        python examples/pydantic_ai_demo.py "Refund order 4711"

Without RGI_URL the adapter falls back to the local daemon
(``http://127.0.0.1:8730``) and reports nothing if it is not running - the demo
still completes, because an indicator must never block the work.

The conversation id is the lane identity. Pass the same one again
(``--conversation order-4711`` or ``RGI_CONVERSATION_ID``) to resume the same
lane; without it a fresh id is generated for this run, which is fine for a
one-shot demo but will not survive a resume. Pydantic AI carries the message
history across a deferred-tool resume, and the lane is correlated by the
conversation id, so keep the two together.

Deferred approvals block the lane; deferred *external* calls (a
``CallDeferred`` background job) keep it ``working`` instead - nobody is being
asked for anything. This demo only shows the approval flow: decisions come
from stdin, and external calls are reported and left for the caller to answer.

Pydantic AI is an optional dependency guarded here so the file can be read
(and imported) without it. Install it with ``pip install pydantic-ai``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("prompt", nargs="?", default="Refund order 4711 and email the customer.")
    parser.add_argument("--conversation", default=os.environ.get("RGI_CONVERSATION_ID"),
                        help="stable lane id; reuse it to resume the same lane")
    return parser.parse_args(argv)


def ask(question: str) -> bool:
    """Read one yes/no answer; a closed stdin means 'no'."""
    try:
        return input(question).strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


async def main(argv=None) -> int:
    args = parse_args(argv)

    try:
        from pydantic_ai import Agent, DeferredToolRequests
    except ImportError:
        print("this demo needs Pydantic AI: pip install pydantic-ai", file=sys.stderr)
        return 0

    from rgi.integrations.pydantic_ai import report_run

    agent = Agent(
        os.environ.get("PYDANTIC_AI_MODEL", "openai:gpt-4o-mini"),
        name="demo-refunder",
        output_type=[str, DeferredToolRequests],
        instructions="You are a careful assistant. Refund at most one order.",
    )

    @agent.tool_plain(requires_approval=True)
    def refund(order_id: str) -> str:
        """Refund `order_id`; only runs after a human approves it."""
        return f"refunded {order_id}"

    conversation_id = args.conversation or f"pydantic-ai-demo-{os.getpid()}"

    async with report_run(conversation_id=conversation_id,
                          label="pydantic-ai demo") as run:
        # The event stream handler feeds the lane while the run is alive; the
        # finished result is folded in by observe(). run.run_kwargs carries the
        # handler, so the adapter and the app never fight over the stream.
        result = await agent.run(args.prompt, conversation_id=conversation_id,
                                 **run.run_kwargs)
        run.observe(result)

        while isinstance(result.output, DeferredToolRequests):
            deferred = result.output
            if deferred.calls:
                print(f"{len(deferred.calls)} external call(s) still running; "
                      "supply their results and re-run to finish.", file=sys.stderr)
                break
            approvals = {call.tool_call_id: ask(f"approve {call.tool_name}? [y/N] ")
                         for call in deferred.approvals}
            results = deferred.build_results(approvals=approvals)
            result = await agent.run(
                deferred_tool_results=results,
                message_history=result.all_messages(),
                conversation_id=conversation_id,       # same lane on resume
                **run.run_kwargs,
            )
            run.observe(result)

        print(result.output if result.output is not None else "(no output)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
