"""Documentation checks: balanced fences, and no broken relative links.

    python tools/check_docs.py

Every fence counts, not just the bare ones - a file full of ```sh and ```json
blocks is exactly where a half-closed fence hides. The first version of this
check only looked for bare ``` and reported healthy files as broken, which is a
worse failure than not checking at all: it teaches people to ignore the output.
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "build", "dist"}

LINK = re.compile(r"\]\((?!https?://|mailto:|#)([^)]+)\)")


def _skip(directory: str) -> bool:
    return directory in SKIP_DIRS or directory.endswith(".egg-info")


def check(path: str) -> list[str]:
    problems: list[str] = []
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()

    fences = sum(1 for line in lines if line.strip().startswith("```"))
    if fences % 2:
        problems.append(f"{fences} code fences - one is unclosed")

    for number, line in enumerate(lines, 1):
        for target in LINK.findall(line):
            target = target.split("#")[0].strip()
            if not target:
                continue
            resolved = os.path.normpath(os.path.join(os.path.dirname(path), target))
            if not os.path.exists(resolved):
                problems.append(f"{number}: link goes nowhere: {target}")
    return problems


def main() -> int:
    checked = 0
    bad = 0
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if not _skip(d)]
        for name in sorted(files):
            if not name.endswith(".md"):
                continue
            path = os.path.join(base, name)
            checked += 1
            for problem in check(path):
                bad += 1
                print(f"  {os.path.relpath(path, ROOT)}: {problem}")

    print(f"checked {checked} markdown files")
    if bad:
        print(f"{bad} problem(s) - see above")
        return 1
    print("fences balanced, every relative link resolves")
    return 0


if __name__ == "__main__":
    sys.exit(main())
