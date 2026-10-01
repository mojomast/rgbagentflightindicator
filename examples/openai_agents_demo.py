"""Run one OpenAI Agents SDK workflow with a lamp on the rgbafi panel.

The panel comes from the environment, exactly like every other rgi client:

    RGI_URL=http://<panel-host>:8730 RGI_TOKEN=$TOKEN \
        python examples/openai_agents_demo.py "Email Ada about the release"

Without RGI_URL the adapter falls back to the local daemon
(``http://127.0.0.1:8730``) and reports nothing if it is not running - the demo
still completes, because an indicator must never block the work.

The conversation id is the lane identity. Pass the same one again
(``--conversation ticket-1234`` or ``RGI_CONVERSATION_ID``) to resume the same
lane after an approval pause; without it a fresh id is generated for this run,
which is fine for a one-shot demo but will not survive a resume.

This is a demo shape, not a production approval UI: approvals are read from
stdin, and the SDK is an optional dependency guarded here so the file can be
read (and imported) without it. Install it with ``pip install openai-agents``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("prompt", nargs="?", default="Email Ada that the release passed its tests.")
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
        from agents import Agent, Runner, function_tool
    except ImportError:
        print("this demo needs the OpenAI Agents SDK: pip install openai-agents",
              file=sys.stderr)
        return 0

    from rgi.integrations.openai_agents import report_run

    @function_tool(needs_approval=True)
    def send_email(to: str, subject: str) -> str:
        """Send an email to `to` with `subject` and a short body."""
        return f"queued: {subject!r} to {to}"

    agent = Agent(
        name="demo-mailer",
        model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        instructions="You are a careful assistant. Send at most one email.",
        tools=[send_email],
    )

    conversation_id = args.conversation or f"openai-agents-demo-{os.getpid()}"

    async with report_run(conversation_id=conversation_id,
                          label="openai-agents demo") as run:
        # One top-level Runner.run owns the lane; hooks are metadata only.
        result = await Runner.run(agent, args.prompt, hooks=run.hooks,
                                  conversation_id=conversation_id)
        run.observe(result)

        # An interruption is a pause, not an end: the lane stays blocked until
        # every request has a decision, then the resumed run reports done.
        while result.interruptions:
            state = result.to_state()
            for item in result.interruptions:
                tool = getattr(item, "name", None) or "tool"
                if ask(f"approve {tool}? [y/N] "):
                    state.approve(item)
                else:
                    state.reject(item)
                run.resolve(item.call_id)
            result = await Runner.run(agent, state, hooks=run.hooks)
            run.observe(result)

        print(result.final_output if result.final_output is not None else "(no output)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
