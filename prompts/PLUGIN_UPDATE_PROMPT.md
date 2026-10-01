# Plugin update prompt

Give this to an agent on a machine that already has the panel's pieces installed
(the watcher, the OpenCode plugin, or both) and needs them brought up to date.

## PASTE FROM HERE

**You are being asked to update the rbgafi panel's pieces on this machine.**

There may be three things here, and they update differently:

| piece | what it is | lives in |
|---|---|---|
| the **plugin** | draws the lane table in OpenCode's sidebar | `~/.config/opencode/plugins/` |
| the **watcher** | reports this machine's OpenCode sessions to the panel | `rgi watch`, run however you start it |
| the **client** | `rgi status` and friends, for reading the panel | the `rgbagentflightindicator` package |

### 1. Look at what you have before changing it

**One installation per machine.** If several agents share this one, they must
share the installation rather than each making their own: two plugins draw two
sidebar blocks, and two watchers claim the same sessions twice.

```sh
rgi doctor                       # panel, token, watcher, plugin, deps - in one command
ls ~/.config/opencode/plugins/   # which plugin directories exist?
rgi status 2>&1 | head -5        # does the client work, and can it see the panel?
```

- a plugin directory with a `tui.ts` in it: **update it in place**, do not add another
- `watcher   running (pid N)`: that watcher is this machine's; leave it running
  (a second `rgi watch` now refuses to start, and says who holds it)
- a directory called `keyboard-status`: the old plugin, retire it in step 3

### 2. Update the watcher, and the client if you have one

**The watcher is one file and needs nothing but Python 3.** The panel publishes
the current one, so any machine can run it without installing anything:

```sh
curl -sS -H "X-LED-Token: $TOKEN" "$BASE/files/rgi-watch.py" -o /tmp/rgi-watch.py
python3 /tmp/rgi-watch.py          # step 5 says how to keep it running
```

Running the current watcher is what puts tokens, context, subagents and running
shells into your lanes; older watchers only send the lane state.

The `rgi` client is separate and still not on PyPI, so there is no `pip install`
that works from outside the panel's own network. If you have a checkout, pull it:

```sh
git -C <your checkout> pull
```

If you do not have one, that is fine: the plugin (step 3) and the `curl` examples
below cover reporting and reading, and `rgi ...` commands exist only on machines
where the client is installed.

### 3. Install the current plugin

```sh
DIR=~/.config/opencode/plugins/rgi-panel
mkdir -p "$DIR"

BASE=<panel-host>            # e.g. http://panel.example:8730
TOKEN=<token>                # or read it from ~/.config/rgi/token

curl -sS -H "X-LED-Token: $TOKEN" "$BASE/files/tui.ts"   -o "$DIR/tui.ts"
curl -sS -H "X-LED-Token: $TOKEN" "$BASE/files/index.ts" -o "$DIR/index.ts"
```

Then retire anything old:

```sh
mv ~/.config/opencode/plugins/keyboard-status \
   ~/.config/opencode/keyboard-status.retired   2>/dev/null
```

Move it **out of** `plugins/`: renaming it in place keeps it discoverable, and
some builds then draw two sidebar blocks.

`GET $BASE/files` lists everything published, with sizes and hashes, if you want
to check what you are about to install. The panel must be reachable from this
machine; if it is not, ask the human for the address, and use the tailnet name
rather than an IP where you have one.

### 4. Make sure the plugin can authenticate

The plugin reads the panel address from `RGI_URL` (older builds also honour
`LEDD_URL`) and the token from `RGI_TOKEN`, falling back to
`~/.config/rgi/token`:

```sh
mkdir -p ~/.config/rgi
printf '%s' "<token>" > ~/.config/rgi/token     # if it is not already there
```

Set `RGI_URL` in the environment OpenCode is started from if the panel is not on
`http://127.0.0.1:8730`, and write the address to `~/.config/rgi/url` as well:

```sh
printf '%s' "http://<panel-host>:8730" > ~/.config/rgi/url
```

The file is what makes this work without any restart: a running OpenCode cannot
acquire a new environment variable, and the plugin (and the `rgi` client) read
this file after `RGI_URL` and before localhost. New terminals carry `RGI_URL`;
the file covers everything already running.

### 5. Run the current watcher

Use the file from step 2:

```sh
python3 /tmp/rgi-watch.py           # or: --url "$BASE" --stale 7200
```

Restart it the way the old one ran (systemd unit, tmux, scheduled task). Check
for a `--hold` flag: that came from an early build that freed each lamp 45 seconds
after every turn. Remove it, or use `--stale 7200`. The watcher finds the panel
address and token by itself (`RGI_URL` then `~/.config/rgi/url`; `RGI_TOKEN` then
`~/.config/rgi/token`), and if the panel refuses a lane it says so in
`~/.config/rgi/watcher.log` instead of failing silently.

The plugin usually reloads when its files change; restart OpenCode if not.

### 6. Verify, and say what you saw

Success looks like this in the sidebar:

```
⌨ rbgafi v0.4
▸1| hermes(▶) Verify the nightly artifacts
 2| opencode(✔) Keyboard status: LED upd…
    (click a lane for detail, the title for all)
⧉ copy install/update prompt + token
```

A collapsed lane is the lamp, the agent's own `ident` (what the human told it to
call itself), the state mark and an abbreviated task, coloured like the lamps:
green in flight, white complete, red needs a human. Clicking a lane shows the
whole task wrapped across lines, then the detail lines, where the harness it runs
in and the machine appear on their own line.

And this in `~/.config/rgi/plugin.log`:

```
setup() called (panel=<your panel>, from RGI_URL, token=yes)
OpenTUI runtime support installed
jsx runtime ready
slot registered: sidebar.content
slot registered: home.footer.status
```

Check these three things specifically, because each has been a real failure before:

1. **The header says `rbgafi v0.4`.** If it still says "Keyboard (number row)" or
   "Panel", you are running the old plugin - go back to step 3.
2. **The log says `token=yes`.** If it says `token=no`, every poll is a 401 and
   the block will read `offline` - fix step 4.
3. **Lanes appear at all.** If the block says `no lanes claimed` but the log is
   healthy, nothing has claimed a lamp yet - that is a panel-side question, not
   a problem here.

If the log says `could not install OpenTUI runtime support` or
`jsx() failed: No renderer found`, report those exact lines - the plugin renders
nothing rather than breaking the TUI, and that message says why.

### 7. Reporting your own state (unchanged, but worth checking)

Claiming a lane and reporting transitions has not changed:

```sh
curl -sS -X POST "$BASE/session/start" -H "Content-Type: application/json" \
  -H "X-LED-Token: $TOKEN" \
  -d '{"agent":"hermes","sessionID":"job-123","label":"nightly sync"}'
```

Then `working` / `blocked` / `done`, then `/session/end`. `prompts/HERMES_PROMPT.md`
on the panel covers reporting *and* reading the lanes.

## PASTE TO HERE
