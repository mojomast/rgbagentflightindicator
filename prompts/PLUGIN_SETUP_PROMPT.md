# Plugin setup prompt

Give this to an agent that should show the panel inside OpenCode's sidebar, on a
machine where the plugin is not installed yet.

## PASTE FROM HERE

**You are being asked to install an OpenCode plugin that displays a status
panel.**

An RGB keyboard is being used as an agent annunciator panel, driven by a small
HTTP service. The plugin shows the same information inside OpenCode: a block in
the session sidebar, and a compact strip on the home footer.

### 0. Find out whether this machine already has it

**Several agents can share one machine, and none of them should install a second
copy of anything.** Two plugins draw two sidebar blocks; two watchers claim the
same sessions twice and the panel fills with lanes nobody can explain. Look
first:

```sh
rgi doctor                       # if you have the client: the whole picture
ls ~/.config/opencode/plugins/   # plugin directories
```

If `rgi` is not installed, that is normal: the client is not on PyPI. Nothing a
machine needs in order to report is behind it — step 6 runs the watcher without it.

Read it before touching anything:

| what you see | what to do |
|---|---|
| a plugin directory containing `tui.ts` | **use and update that one** - do not create another |
| `watcher   running (pid N)` | a watcher already runs here; leave it, do not start a second |
| a directory named `keyboard-status` | the old plugin: retire it in step 2 |
| `rgi` not found | install it; `doctor` needs the client, so fall back to the two commands above |
| `deps MISSING` | install the peer packages in step 1 |

### 1. Install the peer dependencies

```sh
cd ~/.config/opencode
npm install "@opentui/core@^0.5.14" "@opentui/solid@^0.5.14" "solid-js@^1.9.12"
```

npm may warn `EBADENGINE ... required: { node: '>=26.4.0' }` for `@opentui/core`.
That is harmless: OpenCode loads plugins in its own bundled Bun runtime, so the
packages only need to be resolvable from the plugin directory. Do not change Node
because of it.

### 2. Put the plugin in place

```
<opencode config dir>/plugins/rgi-panel/
    index.ts     server half, deliberately empty
    tui.ts       the sidebar and footer
```

Fetch them from the panel if it is reachable:

```sh
DIR=~/.config/opencode/plugins/rgi-panel
mkdir -p "$DIR"
curl -sS -H "X-LED-Token: <token>" "<panel-host>/files/index.ts" -o "$DIR/index.ts"
curl -sS -H "X-LED-Token: <token>" "<panel-host>/files/tui.ts"   -o "$DIR/tui.ts"
```

`GET <panel-host>/files` lists what is published, with sizes and hashes.

Otherwise copy `plugin/index.ts` and `plugin/tui.ts` from this repository.

### 3. Point it at the panel

`tui.ts` reads the panel URL from `RGI_URL`, then `~/.config/rgi/url`, then
`http://127.0.0.1:8730`; the token comes from `RGI_TOKEN` or
`~/.config/rgi/token`. If the panel runs on another machine, set `RGI_URL` in the
environment OpenCode is started from **and** write the address to the file:

```sh
printf '%s' "http://<panel-host>:8730" > ~/.config/rgi/url
```

Never edit the default in `tui.ts`: the next update overwrites it, and a running
OpenCode cannot be handed a new environment variable anyway.

### 4. Check OpenCode can see it

```sh
opencode plugin list
```

The plugin is found by directory discovery; nothing belongs in `cli.json`.

### 5. Restart OpenCode, then verify

`~/.config/rgi/plugin.log` should gain `setup() called (panel=…, from …, token=yes)`,
`slot registered: sidebar.content` and `slot registered: home.footer.status`. The
sidebar should show a block like:

```
⌨ rbgafi v0.4
▸0| hermes(▶) Fetch and follow the panel…
 2| opencode(✔) Keyboard status: LED upd…
```

If the log says `could not install OpenTUI runtime support` or
`jsx() failed: No renderer found`, report those exact lines - the plugin will
have fallen back to rendering nothing rather than breaking the TUI.

### 6. Run the watcher

Lanes exist only while something claims them. The panel publishes the watcher as
one runnable file — no installation, Python 3 and nothing else:

```sh
curl -sS -H "X-LED-Token: <token>" "<panel-host>/files/rgi-watch.py" -o /tmp/rgi-watch.py
python3 /tmp/rgi-watch.py
```

Keep it running the way this machine keeps services. The current watcher is what
reports tokens, context, subagents and running shells; older ones only send the
lane state. It finds the panel address (`RGI_URL`, then `~/.config/rgi/url`) and
the token (`RGI_TOKEN`, then `~/.config/rgi/token`) by itself.

### Rules

1. **Do not edit `cli.json`.** Directory discovery is what is verified.
2. **Keep the plugin's log lines.** A failed TUI plugin is otherwise silent.
3. **Report what you observed**, not what should have happened.

## PASTE TO HERE
