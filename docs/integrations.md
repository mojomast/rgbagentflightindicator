# Integrations

The panel can be driven by the harnesses and frameworks agents actually run in.
Every integration goes through the same small client — `rgi/report.py` — rather
than speaking HTTP on its own, so they all behave the same way:

- **one lane per root workflow.** The lane's identity is
  `<namespace>:<stable session id>`, so two harnesses never collide and a resumed
  conversation lands on the same lane again.
- **children are metadata.** A subagent or background task updates the lane's
  detail; it never claims a lamp of its own.
- **attention is counted by id.** Concurrent approval or input requests all
  survive each other. While any is open the lane reads *blocked*, and a quiet
  wait is never treated as stale.
- **events are ordered and deduplicated.** An event that arrives out of order
  cannot overwrite newer state; a repeated state is a no-op.
- **slots are claimed by number from the free list**, so a new integration can
  never evict somebody else's blocked lane.
- **an integration failure never interrupts the agent.** Every call has a short
  timeout, every error is logged once and swallowed, and hooks always exit 0.

| integration | transport | namespace | doc |
|---|---|---|---|
| Claude Code | command hooks | `claude-code:` | [claude-code.md](claude-code.md) |
| Gemini CLI | hooks / extension | `gemini-cli:` | [gemini-cli.md](gemini-cli.md) |
| Pi | event-API extension | `pi:` | [pi.md](pi.md) |
| GitHub Copilot SDK | application adapter | `copilot:` | [copilot-sdk.md](copilot-sdk.md) |
| Zoo Code CLI | NDJSON event stream | `zoo:` | [zoo-code.md](zoo-code.md) |
| OpenAI Agents SDK | runner hooks | `openai-agents:` | [openai-agents.md](openai-agents.md) |
| Pydantic AI | run/event hooks | `pydantic-ai:` | [pydantic-ai.md](pydantic-ai.md) |
| LangGraph | stream interrupts | `langgraph:` | [langgraph.md](langgraph.md) |
| CrewAI | event bus | `crewai:` | [crewai.md](crewai.md) |
| Microsoft Agent Framework | middleware | `ms-agent:` | [ms-agent.md](ms-agent.md) |
| ChatGPT and Codex | stdio MCP | `rgi-openai:` | [openai.md](openai.md) |
| Native hook/webhook ingest | server-side mapping (Claude Code schema + CI webhooks) | harness or `ci:` | [ingest.md](ingest.md) |
| AgentAPI | `/status` + `/events` watcher (11+ CLI agents) | `agentapi:` | [agentapi.md](agentapi.md) |
| WLED | display backend | — | [wled.md](wled.md) |
| Home Assistant | state publisher | — | [home-assistant.md](home-assistant.md) |

Hook-capable harnesses can also POST **directly** to `POST /hook/<source>` (no
client binary on the machine) when they support HTTP hook handlers; the mapping
tables and the per-harness field differences are in [ingest.md](ingest.md).

## Supported versions

Every integration was written against a **released** version of its upstream,
not a default branch, and each page ends with the same details. Nothing here was
run against a live runtime on this machine: the tests drive mocks and fakes, and
the pages say so where it matters. What *was* exercised on real hardware is the
panel itself — the keyboards, the OpenCode watcher and the plugin.

| integration | upstream | verified against | exercised against |
|---|---|---|---|
| Claude Code | `@anthropic-ai/claude-code` | 2.1.286 | mock panel, documented payload shapes |
| Gemini CLI | `@google/gemini-cli` | 0.62.0 | mock panel, documented payload shapes |
| Pi | `@earendil-works/pi-coding-agent` | 0.99.2 | structural tests + `node --check`; in-memory fetch stub |
| Copilot SDK | `github-copilot-sdk` (Python), `@github/copilot-sdk` (Node) | 1.0.16 | mock panel, fake emitter |
| Zoo Code CLI | `@roo-code/cli` (binary `roo`), extension 3.84.0 | 0.1.17 / 3.84.0 | mock panel, recorded NDJSON stream; **no live CLI run** |
| OpenAI Agents SDK | `openai-agents` | 0.22.3 | mock panel, fake hooks/results |
| Pydantic AI | `pydantic-ai` | 2.52.0 | mock panel, fake events |
| LangGraph | `langgraph` (+ `langchain-core` 1.6.6) | 1.2.12 | mock panel, fake chunks/callbacks |
| CrewAI | `crewai` | 1.15.23 | mock panel, fake bus events |
| Microsoft Agent Framework | `agent-framework` | 1.19.0 | mock panel, fake middleware contexts |
| ChatGPT / Codex | `mcp` | `>=1.30,<2` | transport tests skip when the SDK is absent |
| Hook/webhook ingest | Claude Code hook schema (cross-vendor), GitHub/GitLab/Jenkins webhooks | recorded-shape fixtures | offline fixtures; no live harness |
| AgentAPI | `coder/agentapi` | OpenAPI schema 0.12.2 | mock AgentAPI server; **no live instance** |
| WLED | firmware | 16.0.1 | mock WLED server — **no strip was available** |
| Home Assistant | Core | 2026.9 | mock REST server — **no live instance** |

The framework extras exist so a machine can ask for exactly one stack:
`pip install -e '.[langgraph]'`, `.[crewai]`, `.[openai-agents]`,
`.[pydantic-ai]`, `.[ms-agent]`, `.[copilot]`, or `.[openai]` for the MCP
adapter. The hooks (Claude Code, Gemini CLI) and the Pi extension need no
package at all — they are one file each.

## Adding your own

An integration is a directory, not a framework. The shape is:

1. a module in `rgi/integrations/` exposing whichever entry point suits the
   runtime (`hook(payload) -> int` for command hooks, a wrapper class for
   SDK-owned sessions), importing any third-party package *inside* functions;
2. a line in `rgi/integrations/__init__.py` (`ADAPTERS`) and, for hook harnesses,
   one in `rgi/hooks.py` (`HOOKS`);
3. a test that drives the integration against `tests/mock_panel.py` — a real
   HTTP mock of the daemon — with no third-party package installed;
4. a page under `docs/` that ends with a **Supported versions** section: the
   package and version the integration was verified against, how it was verified,
   and what is known not to work.

`docs/agents.md` covers writing a watcher when no integration exists for a
runtime you use.
