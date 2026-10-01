"""Where the panel is, who this machine is, and the token - resolved one way.

Everything in this project needs the same three answers: the panel's address,
the shared token, and the name this machine's lanes carry. Each resolves the
same way - explicit argument, environment, file, then a sensible default - so a
machine can be configured by writing one file and every process agrees,
including processes that started before the environment changed.

Two things cannot import this and carry their own copy on purpose: the OpenCode
plugin (TypeScript) and the published standalone watcher (served as one file to
machines that have installed nothing). Keep the order in step if you touch this.
"""

from __future__ import annotations

import os
import socket

DEFAULT_URL = "http://127.0.0.1:8730"
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "rgi")
URL_FILE = os.path.join(CONFIG_DIR, "url")
TOKEN_FILE = os.path.join(CONFIG_DIR, "token")
NAME_FILE = os.path.join(CONFIG_DIR, "name")


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            value = fh.read().strip()
        return value or None
    except OSError:
        return None


def resolve_url(explicit: str | None = None) -> str:
    """URL from --url, RGI_URL/LEDD_URL, ~/.config/rgi/url, or localhost."""
    if explicit and explicit.strip():
        return explicit.strip().rstrip("/")
    env = os.environ.get("RGI_URL") or os.environ.get("LEDD_URL")
    if env:
        return env.strip().rstrip("/")
    return (_read(URL_FILE) or DEFAULT_URL).rstrip("/")


def resolve_token(explicit: str | None = None) -> str | None:
    """Token from --token, RGI_TOKEN/LEDD_TOKEN, or ~/.config/rgi/token."""
    if explicit:
        return explicit
    env = os.environ.get("RGI_TOKEN") or os.environ.get("LEDD_TOKEN")
    if env:
        return env
    return _read(TOKEN_FILE)


def resolve_ident(explicit: str | None = None) -> str:
    """The name this machine's lanes carry: ident, RGI_IDENT, file, hostname."""
    if explicit and explicit.strip():
        return explicit.strip()
    env = os.environ.get("RGI_IDENT")
    if env and env.strip():
        return env.strip()
    return _read(NAME_FILE) or socket.gethostname().split(".")[0]
