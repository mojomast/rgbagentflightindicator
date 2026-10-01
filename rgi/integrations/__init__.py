"""Optional adapters that report other agent runtimes to the panel.

Every adapter is imported lazily, so the core install stays lightweight: nothing
here needs a framework, an SDK, or a network to import. An adapter that is not
installed simply says so when it is asked for.

Adding one means: a module beside this file, a line in ``ADAPTERS``, a test, and
a page under ``docs/``. The module is expected to import the third-party package
*inside* the functions that need it, and to carry on if it is missing.
"""

from __future__ import annotations

import importlib

# name -> module. The names are the user-facing ones, used by `rgi hook <name>`
# for hook-based harnesses and by the examples in docs/.
ADAPTERS: dict[str, str] = {
    "claude-code": "rgi.integrations.claude_code",
    "gemini-cli": "rgi.integrations.gemini_cli",
    "copilot": "rgi.integrations.copilot",
    "openai-agents": "rgi.integrations.openai_agents",
    "pydantic-ai": "rgi.integrations.pydantic_ai",
    "langgraph": "rgi.integrations.langgraph",
    "crewai": "rgi.integrations.crewai",
    "ms-agent": "rgi.integrations.ms_agent",
    "zoo-cli": "rgi.integrations.zoo_cli",
    "home-assistant": "rgi.integrations.home_assistant",
}


def load(name: str):
    """Import an adapter by name. KeyError if there is no such adapter."""
    module = ADAPTERS.get(name)
    if module is None:
        raise KeyError(name)
    return importlib.import_module(module)


def names() -> list[str]:
    return sorted(ADAPTERS)
