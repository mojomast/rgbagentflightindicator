"""Refuse to publish anything personal.

This project was written on a real machine against a real keyboard, with a real
shared token and a real tailnet. None of that belongs in a public repository.

Run before every push:  python tools/scan_for_secrets.py

It exits non-zero if it finds a token, a private address, a local user path or a
hostname from the machine the project came from. Please extend the patterns
rather than weakening them.
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "build", "dist", ".mypy_cache"}


def _skip(directory: str) -> bool:
    return directory in SKIP_DIRS or directory.endswith(".egg-info")

PATTERNS: list[tuple[str, str]] = [
    # credentials
    (r"(?i)\bX-LED-Token:\s*(?!<|your|\$)[A-Za-z0-9_\-]{8,}", "a real token in a header example"),
    (r"\b[a-zA-Z0-9_\-]{20,}\b(?=.*token)", "something long that sits next to 'token'"),
    # networks
    (r"\b100\.(6[4-9]|[7-9][0-9]|1[0-2][0-9])\.\d{1,3}\.\d{1,3}\b", "a Tailscale CGNAT address"),
    (r"\b192\.168\.\d{1,3}\.\d{1,3}\b", "a private LAN address"),
    (r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "a private LAN address"),
    (r"\b172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b", "a private LAN address"),
    (r"\.ts\.net\b", "a tailnet DNS name"),
    # local paths and machines
    (r"(?i)\bC:\\+Users\\+[a-z0-9._-]+", "a local Windows user path"),
    (r"/home/[a-z0-9._-]+/", "a local home path"),
    (r"/Users/[a-z0-9._-]+/", "a local home path"),
]

# machine names from the machine this started on; keep this list growing
EXTRA = [
    "brrrrrrrr",
    "tailec998",
    "Sinodragon",
    "aQ3Ovx7mzy34z29qJks7tFSG",
]

# things that are legitimately allowed to look suspicious
ALLOW = [
    "127.0.0.1",
    "0.0.0.0",
    "http://panel:8730",
    "<panel-host>",
    # documentation placeholders, obviously not a real tailnet: the point of this
    # check is to catch the machine's own names, not to make examples unwritable
    "example.ts.net",
    "tailnet.ts.net",
    "<token>",
    "$TOKEN",
    "your token",
    # token-counter vocabulary from usage metadata (OTLP, agent reports), not
    # credentials; long names here would otherwise trip the length heuristic
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "cache_creation_tokens",
]


def files():
    me = os.path.abspath(__file__)
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if not _skip(d)]
        for name in names:
            if name.endswith((".pyc", ".png", ".jpg", ".zip")):
                continue
            path = os.path.join(base, name)
            if os.path.abspath(path) == me:
                continue            # this file lists the patterns it forbids
            yield path


def main() -> int:
    problems: list[str] = []
    checked = 0

    for path in files():
        rel = os.path.relpath(path, ROOT)
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        checked += 1
        for line_no, line in enumerate(text.splitlines(), 1):
            if any(allowed in line for allowed in ALLOW):
                stripped = line
                for allowed in ALLOW:
                    stripped = stripped.replace(allowed, "")
            else:
                stripped = line
            for pattern, why in PATTERNS:
                if re.search(pattern, stripped):
                    problems.append(f"{rel}:{line_no}: {why}\n    {line.strip()[:110]}")
            for needle in EXTRA:
                if needle.lower() in stripped.lower():
                    problems.append(f"{rel}:{line_no}: machine-specific text {needle!r}\n"
                                    f"    {line.strip()[:110]}")

    print(f"scanned {checked} files")
    if problems:
        print(f"\n{len(problems)} problem(s) - do not publish this yet:\n")
        for p in problems:
            # the console encoding on some machines cannot render the panel's
            # own glyphs; never let reporting a problem become the problem
            printable = p.encode("ascii", "replace").decode("ascii")
            print("  " + printable)
        return 1
    print("no tokens, private addresses, local paths or machine names found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
