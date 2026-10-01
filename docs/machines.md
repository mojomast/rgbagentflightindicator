# Running the panel across machines

The panel itself lives on one machine — the one with the keyboard. Everything
else is optional and per-machine: a machine can *report* to the panel, *display*
it, or both.

| piece | where it runs | needs |
|---|---|---|
| **panel** (`rgi daemon`) | the machine with the keyboard | the keyboard, USB |
| **watcher** (`rgi watch`) | any machine whose OpenCode sessions should appear | `rgi`, and a route to the panel |
| **plugin** (`tui.ts`) | any machine whose editor should *show* the lanes | OpenCode, and a route to the panel |
| **client** (`rgi status`) | anywhere, for reading the lanes | `rgi`, and a route to the panel |

A machine that does not run OpenCode, but does run other agents, needs none of
this — hand those agents [AGENT_PROMPT.md](../prompts/AGENT_PROMPT.md) and they
report over HTTP directly.

## What the panel must be doing

Remote machines can only reach it if it is bound beyond localhost, and a
non-localhost bind **requires a token**:

```sh
rgi daemon --host 0.0.0.0 --port 8730          # token from ~/.config/rgi/token
```

Give other machines an address they can route to. Prefer a name over an IP (a
tailnet name, a LAN hostname): it survives address changes, and it reads better on
the lanes. Confirm from the other machine before anything else:

```sh
curl -sS -H "X-LED-Token: $TOKEN" http://<panel-host>:8730/files | head -c 200
```

`401` means the token is wrong or missing; a hang usually means a bad name (some
resolvers hand back a link-local IPv6 address first — try the fully qualified
name) or a firewall.

## First install on another machine

Two commands, plus the plugin:

```sh
pip install rgbagentflightindicator
rgi watch --url http://<panel-host>:8730      # reports this machine's sessions
```

```sh
DIR=~/.config/opencode/plugins/rgi-panel
mkdir -p "$DIR"
curl -sS -H "X-LED-Token: $TOKEN" "http://<panel-host>:8730/files/tui.ts"   -o "$DIR/tui.ts"
curl -sS -H "X-LED-Token: $TOKEN" "http://<panel-host>:8730/files/index.ts" -o "$DIR/index.ts"
mkdir -p ~/.config/rgi && printf '%s' "$TOKEN" > ~/.config/rgi/token
```

The plugin needs OpenCode's peer packages once per machine:

```sh
cd ~/.config/opencode
npm install "@opentui/core@^0.5.14" "@opentui/solid@^0.5.14" "solid-js@^1.9.12"
```

`prompts/PLUGIN_SETUP_PROMPT.md` is the same thing written for an agent to follow.

## Updating an existing machine

```sh
pip install -U rgbagentflightindicator            # the client and watcher
curl -sS -H "X-LED-Token: $TOKEN" "http://<panel-host>:8730/files/tui.ts" \
  -o ~/.config/opencode/plugins/rgi-panel/tui.ts  # the plugin
```

Restart the watcher the way you started it, then restart OpenCode if the sidebar
does not pick the change up (it often reloads on its own).

**Retire the old plugin if you have one.** Early builds installed
`~/.config/opencode/plugins/keyboard-status/` and asked for `LEDD_URL`. It is a
different plugin with a different id, so leaving both installed means two blocks
in the sidebar and one of them stuck on an old panel:

```sh
mv ~/.config/opencode/plugins/keyboard-status \
   ~/.config/opencode/plugins/keyboard-status.retired
```

`prompts/PLUGIN_UPDATE_PROMPT.md` is the agent-facing form of this section, and it
opens by asking what is actually installed.

## Configuration on a remote machine

| setting | where | meaning |
|---|---|---|
| panel address | `RGI_URL` env, else `http://127.0.0.1:8730` | the plugin's panel |
| token | `RGI_TOKEN` env, else `~/.config/rgi/token` | shared secret |
| watcher log | `~/.config/rgi/watcher.log` | every decision it makes |
| plugin log | `~/.config/rgi/plugin.log` | whether the plugin loaded and how |

`LEDD_URL` / `LEDD_TOKEN` are still honoured by the plugin, because machines set
up before the rename have them in their environment.

## Verify, in this order

1. **The panel answers** from that machine: the `curl …/files` above.
2. **The watcher binds**: `~/.config/rgi/watcher.log` shows `[bind] … (in flight)`
   shortly after an OpenCode turn starts, and `[land]` when it ends.
3. **The plugin loaded and authenticated**: `~/.config/rgi/plugin.log` shows
   `setup() called … token=yes`, `OpenTUI runtime support installed`,
   `jsx runtime ready`, and two `slot registered:` lines.
4. **The sidebar block** is labelled `⌨ rbgafi v0.3` and lists lanes with host
   columns.

Each of those four has been a real failure at least once; they are listed in this
order because the later ones cannot work while an earlier one is broken.

| symptom | cause |
|---|---|
| all calls `401` | token missing: `RGI_TOKEN` or `~/.config/rgi/token` |
| block says `offline` | the plugin cannot reach the panel — check `RGI_URL` and the token |
| log says `token=no` | the plugin found no token, so every poll fails |
| two sidebar blocks | the old `keyboard-status` plugin is still installed |
| block still says "Keyboard (number row)" | you are running the old plugin |
| watcher silent | check it is running; a refused lane is now logged with the reason |
| block empty, log healthy | no lanes are claimed — that is a panel-side question |

## Handing this to an agent

Both prompts are published, so you never paste a wall of text:

```
Fetch and follow the instructions at
http://<panel-host>:8730/files/PLUGIN_UPDATE_PROMPT.md
sending the header  X-LED-Token: <token>
```

| prompt | for |
|---|---|
| [PLUGIN_SETUP_PROMPT.md](../prompts/PLUGIN_SETUP_PROMPT.md) | a machine with no plugin yet |
| [PLUGIN_UPDATE_PROMPT.md](../prompts/PLUGIN_UPDATE_PROMPT.md) | a machine that already has one |
| [HERMES_PROMPT.md](../prompts/HERMES_PROMPT.md) | an agent that reports *and* reads the panel |
| [AGENT_PROMPT.md](../prompts/AGENT_PROMPT.md) | an agent that only reports |

`GET /files` lists everything the panel publishes, with sizes and sha256 hashes,
so an agent can check what it is about to install.
