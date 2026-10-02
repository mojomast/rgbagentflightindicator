# Running the panel across machines

The panel itself lives on one machine — the one with the keyboard. Everything
else is optional and per-machine: a machine can *report* to the panel, *display*
it, or both.

| piece | where it runs | needs |
|---|---|---|
| **panel** (`rgi daemon`) | the machine with the keyboard | the keyboard, USB |
| **watcher** (`rgi watch`, or the published `rgi-watch.py`) | any machine whose OpenCode sessions should appear | Python 3, and a route to the panel |
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

The watcher is **one published file and needs nothing but Python 3** — no
install, no checkout:

```sh
curl -sS -H "X-LED-Token: $TOKEN" "http://<panel-host>:8730/files/rgi-watch.py" \
  -o /tmp/rgi-watch.py
python3 /tmp/rgi-watch.py --url http://<panel-host>:8730
```

Keep it running the way this machine keeps services (systemd, tmux, a scheduled
task).

The `rgi` client is not on PyPI, but the panel serves a built wheel — no
registry, and it matches the panel's own tree:

```sh
BASE=http://<panel-host>:8730                  # and TOKEN, as above
curl -sS -H "X-LED-Token: $TOKEN" "$BASE/files" > /tmp/files.json
WHEEL=$(python3 -c "import json;print(next(f['name'] for f in json.load(open('/tmp/files.json'))['files'] if f['name'].endswith('.whl')))")
curl -sS -H "X-LED-Token: $TOKEN" "$BASE/files/$WHEEL" -o /tmp/rgi.whl
python3 -m pip install --user /tmp/rgi.whl
```

The wheel is built on the panel with `uv build --wheel` (build output, not
committed); if the manifest has no `.whl`, install from a checkout instead.

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
curl -sS -H "X-LED-Token: $TOKEN" "http://<panel-host>:8730/files/rgi-watch.py" \
  -o /tmp/rgi-watch.py                            # the watcher: one file
curl -sS -H "X-LED-Token: $TOKEN" "http://<panel-host>:8730/files/tui.ts" \
  -o ~/.config/opencode/plugins/rgi-panel/tui.ts  # the plugin
```

Restart the watcher with the new file, then restart OpenCode if the sidebar does
not pick the change up (it often reloads on its own).

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
4. **The sidebar block** is labelled `⌨ rgbafi v0.5` and names each lane after its
   machine by default (`RGI_IDENT`, then `~/.config/rgi/name`, then the hostname),
   or after the `ident` an agent set, with the task on the line beneath.

Each of those four has been a real failure at least once; they are listed in this
order because the later ones cannot work while an earlier one is broken.

| symptom | cause |
|---|---|
| all calls `401` | token missing: `RGI_TOKEN` or `~/.config/rgi/token` |
| block says `offline` | the plugin cannot reach the panel — check `RGI_URL`, `~/.config/rgi/url`, and the token |
| log says `token=no` | the plugin found no token, so every poll fails |
| two sidebar blocks | the old `keyboard-status` plugin is still installed |
| block still says "Keyboard (number row)" | you are running the old plugin |
| watcher silent | check it is running; a refused lane is now logged with the reason |
| block empty, log healthy | no lanes are claimed — that is a panel-side question |

## Several agents on one machine

Agents sharing a machine must share the *installation*, not each make their own:

- **one plugin directory.** Two plugins draw two sidebar blocks, and one of them
  will be stale. `rgi doctor` lists what is there and says whether more than one
  is live.
- **one watcher.** A second `rgi watch` refuses to start and names the process
  holding the slot (`~/.config/rgi/watcher.lock`), because two watchers claim the
  same sessions twice — the panel then shows duplicate lanes that nobody can
  explain.
- **one token**, in `~/.config/rgi/token`, shared by every agent on the box.

`rgi doctor` answers the whole question in one command:

```
rgbafi 0.5.2 - this machine
  panel     reachable at http://127.0.0.1:8730: 1 device(s), 3 lane(s)
  token     found (--token, RGI_TOKEN, or ~/.config/rgi/token)
  watcher   running (pid 45680)
  plugin    1 live plugin directory: rgi-panel
              keyboard-status.retired: retired (fine to keep, but it is not loaded)
  deps      present (@opentui/solid under ~/.config/opencode)
```

Before installing anything, an agent is told to run it (or, if `rgi` is not
installed yet, to look at the plugin directory by hand) and to reuse what it
finds. Both prompts lead with that, and the plugin-related prompts also forbid
hand-editing local copies — changes come from the panel's `/files` endpoint.

## Reaching the panel over Tailscale, securely

This is the recommended way to connect machines that are not on the same LAN, and
the only one this project supports for anything beyond a trusted network.

**Tailscale gives you the transport.** Every packet between peers is
WireGuard-encrypted and authenticated by device key, and nothing is exposed to the
public internet — there is no port to forward and nothing to find by scanning.
That is why the panel speaks plain HTTP: inside a tailnet, the encryption is
already done, and adding TLS would mean certificates for names that only exist on
your own tailnet.

But Tailscale authenticates *devices*, not *applications*. Any device on your
tailnet can reach port 8730 on the panel's host, so three things carry the rest of
the security:

**1. The token is mandatory, and it is not a formality.** Bind beyond localhost and
the daemon requires `X-LED-Token` on every request, including `/files`. It comes
from `--token`, then `RGI_TOKEN`, then `~/.config/rgi/token`:

```sh
mkdir -p ~/.config/rgi && chmod 700 ~/.config/rgi
head -c 32 /dev/urandom | base64 > ~/.config/rgi/token   # or: openssl rand -base64 32
chmod 600 ~/.config/rgi/token
```

Rotate it by replacing the file and restarting the daemon and every watcher — a
device removed from the tailnet keeps any token it was given, so removing the
device is not enough on its own.

**2. Use the tailnet's ACLs to say who may reach the port at all.** In the
Tailscale admin console's access controls, grant only the machines that should
report or observe, and only on that port:

```jsonc
{
  "grants": [
    {
      // agents that report their sessions
      "src": ["tag:agent"],
      "dst": ["tag:panel"],
      "ip": ["tcp:8730"],
    },
    {
      // the human's own devices, which read the lanes and the prompts
      "src": ["user:you@example.com"],
      "dst": ["tag:panel"],
      "ip": ["tcp:8730"],
    },
  ],
}
```

Tag the panel's machine (`tailscale tag`) so the rule survives renames, and put
agent machines behind `tag:agent`. Everything else on the tailnet is then unable
to open the port, whatever token it holds.

**3. Never do any of these:**

- **`tailscale funnel`**, which publishes to the public internet by design.
- **Port forwarding or a reverse proxy on a public interface** — the token is a
  shared secret in a header, not a hardened auth system; it belongs behind the
  tailnet.
- **`0.0.0.0` on a machine that also has a public IP.** If in doubt, bind the
  panel to its tailnet address explicitly:
  `rgi daemon --host $(tailscale ip -4) --port 8730`.
- **Committing a token.** `tools/scan_for_secrets.py` refuses to publish one, and
  it has caught real leaks — including in documentation.

### Addresses: use the name, not the IP

Prefer the tailnet's fully qualified name over the address:

```sh
tailscale status | grep -i panel          # find it
curl -sS -H "X-LED-Token: $TOKEN" http://panel.example.ts.net:8730/files | head -c 200
```

A name survives address changes and reads better on the lanes, and it avoids a
real trap: **short names can resolve to a link-local IPv6 address first**, and a
daemon bound to IPv4 will not answer, so the connection appears to hang. Use the
full `machine.tailnet.ts.net` form.

### Verifying the path before trusting it

From the machine that will report to the panel, in this order:

```sh
tailscale status                          # is the panel's machine online, and direct or relayed?
tailscale ping <panel-host>               # does a path exist at all
curl -sS -m 5 -o /dev/null -w '%{http_code}\n' \
  -H "X-LED-Token: $TOKEN" http://<panel-host>:8730/status
rgi doctor --url http://<panel-host>:8730 # if the client is installed
```

- `000` or a timeout: no path, or the wrong name form (try the FQDN), or the ACL
  denies it.
- `401`: the path is fine; the token is wrong.
- `200`: secure and working.

The watcher — either `rgi watch` or the published `rgi-watch.py` — and the plugin
both find the token themselves, so a machine that passes the check above needs no
further configuration.

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
