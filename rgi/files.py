"""Files the daemon publishes over GET /files, and their hashes.

Why: another machine (or another agent) can fetch the OpenCode plugin and the
current instructions with one request instead of being handed a wall of text
that drifts out of date. The prompts are the same files that live in ``prompts/``
in this repository.
"""

from __future__ import annotations

import hashlib
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

PUBLISHED: dict[str, str] = {
    # the OpenCode TUI plugin
    "index.ts": os.path.join(ROOT, "plugin", "index.ts"),
    "tui.ts": os.path.join(ROOT, "plugin", "tui.ts"),
    # instructions
    "AGENT_PROMPT.md": os.path.join(ROOT, "prompts", "AGENT_PROMPT.md"),
    "HERMES_PROMPT.md": os.path.join(ROOT, "prompts", "HERMES_PROMPT.md"),
    "PLUGIN_SETUP_PROMPT.md": os.path.join(ROOT, "prompts", "PLUGIN_SETUP_PROMPT.md"),
    "PLUGIN_UPDATE_PROMPT.md": os.path.join(ROOT, "prompts", "PLUGIN_UPDATE_PROMPT.md"),
    # the watcher: one file, standard library only, runnable on a machine that has
    # installed nothing - which is how a remote machine reports tokens and context
    "rgi-watch.py": os.path.join(ROOT, "rgi", "watchers", "opencode.py"),
    # integration instructions: a machine can read the page for its runtime
    # straight from the panel instead of being handed a link to a repository
    "integrations.md": os.path.join(ROOT, "docs", "integrations.md"),
    "claude-code.md": os.path.join(ROOT, "docs", "claude-code.md"),
    "gemini-cli.md": os.path.join(ROOT, "docs", "gemini-cli.md"),
    "pi.md": os.path.join(ROOT, "docs", "pi.md"),
    "copilot-sdk.md": os.path.join(ROOT, "docs", "copilot-sdk.md"),
    "openai-agents.md": os.path.join(ROOT, "docs", "openai-agents.md"),
    "pydantic-ai.md": os.path.join(ROOT, "docs", "pydantic-ai.md"),
    "langgraph.md": os.path.join(ROOT, "docs", "langgraph.md"),
    "crewai.md": os.path.join(ROOT, "docs", "crewai.md"),
    "ms-agent.md": os.path.join(ROOT, "docs", "ms-agent.md"),
    "wled.md": os.path.join(ROOT, "docs", "wled.md"),
    "home-assistant.md": os.path.join(ROOT, "docs", "home-assistant.md"),
    "openai.md": os.path.join(ROOT, "docs", "openai.md"),
    # reach: ingest, notifications and the extra backends
    "ingest.md": os.path.join(ROOT, "docs", "ingest.md"),
    "agentapi.md": os.path.join(ROOT, "docs", "agentapi.md"),
    "notify.md": os.path.join(ROOT, "docs", "notify.md"),
    "mqtt.md": os.path.join(ROOT, "docs", "mqtt.md"),
    "otlp.md": os.path.join(ROOT, "docs", "otlp.md"),
    "digest.md": os.path.join(ROOT, "docs", "digest.md"),
    "lamparray.md": os.path.join(ROOT, "docs", "lamparray.md"),
    "gamesense.md": os.path.join(ROOT, "docs", "gamesense.md"),
    "http-light.md": os.path.join(ROOT, "docs", "http-light.md"),
    # the Pi extension itself, as one file to drop in place
    "pi-extension.ts": os.path.join(ROOT, "plugin", "pi", "extension.ts"),
}

DIST = os.path.join(ROOT, "dist")


def built_wheels() -> dict[str, str]:
    """Client wheels that have been built, if any.

    `dist/` is build output and gitignored. Publishing whatever is there means a
    reporting machine can install the client without a registry, and the wheel
    matches the panel's own tree by construction. Nothing is published when the
    panel has not built one.
    """
    out: dict[str, str] = {}
    try:
        for name in os.listdir(DIST):
            if name.endswith(".whl"):
                out[name] = os.path.join(DIST, name)
    except OSError:
        pass
    return out


def published_files() -> list[dict]:
    out = []
    entries = {**PUBLISHED, **built_wheels()}
    for name in sorted(entries):
        path = entries[name]
        entry: dict = {"name": name, "url": f"/files/{name}"}
        try:
            with open(path, "rb") as fh:
                blob = fh.read()
            entry.update(bytes=len(blob),
                         sha256=hashlib.sha256(blob).hexdigest()[:16],
                         modified=os.path.getmtime(path))
        except OSError as exc:
            entry["error"] = str(exc)
        out.append(entry)
    return out


def read_published(name: str) -> str | None:
    path = PUBLISHED.get(name) or built_wheels().get(name)
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def read_published_bytes(name: str) -> bytes | None:
    """Raw bytes of any published file: a wheel is binary, prompts are not."""
    path = PUBLISHED.get(name) or built_wheels().get(name)
    if path is None:
        return None
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None
