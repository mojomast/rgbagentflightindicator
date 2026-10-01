"""Rewrite the code blocks inside PLUGIN_SETUP_PROMPT.md from the live files.

The prompt embeds index.ts and tui.ts verbatim. Hand-copying drifts; this keeps
them identical by construction.

    python sync-prompt-code.py          # update the prompt
    python check-prompt-sync.py         # verify (separate script)
"""

import os

HOME = os.path.expanduser("~")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPT = os.path.join(ROOT, "prompts", "PLUGIN_SETUP_PROMPT.md")
PLUGIN = os.path.join(HOME, ".config", "opencode", "plugins", "rgi-panel")

text = open(PROMPT, encoding="utf-8").read()

for fname in ("index.ts", "tui.ts"):
    body = open(os.path.join(PLUGIN, fname), encoding="utf-8").read().rstrip("\n")
    marker = body.splitlines()[0]          # first line is unique per file

    hits = [i for i in range(len(text)) if text.startswith(marker, i)]
    if len(hits) != 1:
        print(f"  {fname}: marker appears {len(hits)} times, expected 1 - skipped")
        continue
    idx = hits[0]

    start = text.rindex("```ts\n", 0, idx) + len("```ts\n")
    end = text.index("\n```", idx)
    old_lines = len(text[start:end].splitlines())
    new_lines = len(body.splitlines())

    text = text[:start] + body + text[end:]
    print(f"  {fname}: {old_lines} embedded lines -> {new_lines}")

open(PROMPT, "w", encoding="utf-8", newline="\n").write(text)
print("prompt updated")
