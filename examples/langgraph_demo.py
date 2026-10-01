"""Report a LangGraph thread to the panel -- a runnable-shaped demo.

With ``langgraph`` installed (``pip install langgraph``) this builds a tiny
graph whose middle node asks for approval with ``interrupt()``. The first pass
ends with the lane ``blocked``; a ``Command(resume=...)`` on the same thread
lands on the same lane, returns it to ``working`` and finishes it. Without the
package, fake chunks and callbacks drive the same adapter.

    python examples/langgraph_demo.py

The panel address and token come from ``RGI_URL`` / ``RGI_TOKEN``, then from
``~/.config/rgi/url`` and ``~/.config/rgi/token``, then from localhost. If no
panel is listening, reporting is quietly skipped: an indicator that is down
must never break the application.
"""

from __future__ import annotations

import sys

try:                                            # third-party, optional
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command, interrupt
    from typing_extensions import TypedDict

    HAS_LANGGRAPH = True
except ImportError:
    InMemorySaver = None                        # type: ignore[assignment]
    StateGraph = None                           # type: ignore[assignment]
    END = START = Command = interrupt = None    # type: ignore[assignment]

    class TypedDict(dict):                      # type: ignore[no-redef]
        """Stand-in so the module still imports without langgraph."""

    HAS_LANGGRAPH = False

from rgi.integrations.langgraph import report_run

THREAD = "langgraph-demo-thread"


def run_with_langgraph() -> None:
    class State(TypedDict):
        topic: str
        approved: str | None

    def draft(_state):
        return {"approved": None}

    def ask(state):
        # This pauses the graph. On resume the node re-runs and interrupt()
        # returns the value of Command(resume=...).
        answer = interrupt({"question": f"publish {state['topic']}?",
                            "options": ["yes", "no"]})
        return {"approved": str(answer)}

    def finish(state):
        print("finished; approved =", state.get("approved"))
        return {}

    graph = StateGraph(State)
    graph.add_node("draft", draft)
    graph.add_node("ask", ask)
    graph.add_node("finish", finish)
    graph.add_edge(START, "draft")
    graph.add_edge("draft", "ask")
    graph.add_edge("ask", "finish")
    graph.add_edge("finish", END)
    compiled = graph.compile(checkpointer=InMemorySaver())

    # run.config carries the thread id and the panel callback handler.
    with report_run(THREAD, label="langgraph demo") as run:
        for chunk in compiled.stream({"topic": "release notes"}, run.config,
                                     stream_mode=["updates", "values"]):
            run.observe(chunk)
        print("paused; pending =", run.pending)     # the human decides here

    # Later (same process or a new one): the same thread id, the same lane.
    with report_run(THREAD, label="langgraph demo") as run:
        print("still waiting on", run.pending)
        for chunk in compiled.stream(Command(resume="yes"), run.config,
                                     stream_mode=["updates", "values"]):
            run.observe(chunk)
    print("lane is done now")


class FakeInterrupt:
    def __init__(self, iid, value):
        self.id = iid
        self.value = value


class FakeCallback:
    def __init__(self, event, run_id="root", parent_ids=(), metadata=None, data=None):
        self.event = event
        self.run_id = run_id
        self.parent_ids = list(parent_ids)
        self.name = "LangGraph"
        self.metadata = metadata
        self.data = data


class FakeCommand:
    def __init__(self, resume):
        self.resume = resume
        self.goto = ()


def run_without_langgraph() -> None:
    print("langgraph is not installed; driving fake events instead.")
    with report_run(THREAD, label="langgraph demo (fake)") as run:
        run.observe(FakeCallback("on_chain_start", run_id="root"))
        run.observe({"research": {"notes": "draft written"}})
        run.observe({"__interrupt__": (FakeInterrupt("demo-int", "publish?"),)})
        print("blocked; pending =", run.pending)
    print("the stream ended while blocked: the wait outlives it")

    with report_run(THREAD, label="langgraph demo (fake)") as run:
        run.observe(FakeCommand("yes"))
        run.observe(FakeCallback("on_chain_end", run_id="root", data={}))
    print("lane is done now")


def main() -> int:
    if HAS_LANGGRAPH:
        run_with_langgraph()
    else:
        run_without_langgraph()
    return 0


if __name__ == "__main__":
    sys.exit(main())
