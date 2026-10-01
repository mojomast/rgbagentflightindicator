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
}


def published_files() -> list[dict]:
    out = []
    for name in sorted(PUBLISHED):
        path = PUBLISHED[name]
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
    path = PUBLISHED.get(name)
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None
