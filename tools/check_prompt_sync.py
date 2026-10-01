"""Check that the code embedded in PLUGIN_SETUP_PROMPT.md is byte-identical to
the files that are actually working, so the prompt cannot drift."""

import os
import re

HOME = os.path.expanduser("~")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPT = os.path.join(ROOT, "prompts", "PLUGIN_SETUP_PROMPT.md")
PLUGIN = os.path.join(HOME, ".config", "opencode", "plugins", "rgi-panel")

md = open(PROMPT, encoding="utf-8").read()
blocks = re.findall(r"```ts\n(.*?)```", md, re.S)
print(f"found {len(blocks)} ```ts blocks in the prompt")

names = ["index.ts", "tui.ts"]
ok = True
# The last two blocks are the file contents; an earlier snippet shows the two
# lines a remote install has to edit.
for want, block in zip(names, blocks[-2:]):
    path = os.path.join(PLUGIN, want)
    actual = open(path, encoding="utf-8").read()
    a = block.strip() + "\n"
    b = actual.strip() + "\n"
    if a == b:
        print(f"  MATCH    {want}  ({len(b.splitlines())} lines)")
    else:
        ok = False
        print(f"  MISMATCH {want}")
        al, bl = a.splitlines(), b.splitlines()
        for i in range(max(len(al), len(bl))):
            x = al[i] if i < len(al) else "<eof>"
            y = bl[i] if i < len(bl) else "<eof>"
            if x != y:
                print(f"      line {i + 1} differs:")
                print(f"        prompt: {x!r}")
                print(f"        file  : {y!r}")
                break

print()
print("prompt code is in sync" if ok else "PROMPT IS OUT OF DATE")
