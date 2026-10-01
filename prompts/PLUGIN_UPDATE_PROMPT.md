# Plugin update prompt

Give this to an agent on a machine that already has the watcher and/or the panel
plugin, when they need to be brought up to date.

## PASTE FROM HERE

**You are being asked to update the panel's pieces on this machine.**

Two things may be installed here:

- the **watcher** (`rgi watch`) which reports this machine's agent sessions
- the **plugin** (OpenCode's `plugins/rgi-panel/`) which draws the panel in the
  sidebar

### 1. Note your local edits first

Back up, then diff. Machines differ: the panel URL, the token and the start
command are often local.

```sh
cp ~/.config/rgi/watcher.log ~/.config/rgi/watcher.log.bak 2>/dev/null
```

Check for a `--hold` flag in how the watcher is started. That flag came from an
early build whose owner freed each lamp 45 seconds after every turn - if it is
still there, lamps will keep vanishing shortly after a turn ends. Remove it, or
use `--stale 7200`.

### 2. Update from the source

Either fetch from the panel:

```sh
BASE=<panel-host>
TOKEN=<token>
curl -sS -H "X-LED-Token: $TOKEN" "$BASE/files/tui.ts" -o ~/.config/opencode/plugins/rgi-panel/tui.ts
curl -sS -H "X-LED-Token: $TOKEN" "$BASE/files/index.ts" -o ~/.config/opencode/plugins/rgi-panel/index.ts
```

...or from the repository (`pip install -U` / `git pull`, whichever you used).

### 3. Restart the watcher, then OpenCode

Restart the watcher the same way it was started, with the panel URL it used
before. The plugin usually reloads when its files change; restart OpenCode if the
sidebar does not pick it up.

### 4. Verify behaviour, not just installation

Do a normal turn and watch the lamp:

1. while working: **green**
2. when the turn ends: **blinks white, then holds white**
3. **it must still be lit a minute later** - if it goes dark after ~45 seconds,
   the old `--hold` behaviour is still in play
4. next turn: **green again**, not stuck white

`~/.config/rgi/watcher.log` narrates every decision:

```
[bind] … (in flight)   [land] … (turn finished)   [go] … (working again)
[work] … (unblocked)   [hold] … (needs you)       [free] … (idle > 120 min)
```

### 5. Prove the watcher can reach OpenCode's CLI

```sh
time opencode api get /api/session/active     # expect JSON, well under a second
```

If that hangs, the watcher will hang with it. An early build called the CLI
through a shell, which on Linux and macOS ran only the first word of the command
(`opencode`) and waited for the 25 second timeout. The current build resolves the
binary and passes the arguments directly.

## PASTE TO HERE
