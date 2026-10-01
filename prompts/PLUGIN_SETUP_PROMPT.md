# Plugin setup prompt

Give this to an agent that should show the panel inside OpenCode's sidebar, on a
machine where the plugin is not installed yet.

## PASTE FROM HERE

**You are being asked to install an OpenCode plugin that displays a status
panel.**

An RGB keyboard is being used as an agent annunciator panel, driven by a small
HTTP service. The plugin shows the same information inside OpenCode: a block in
the session sidebar, and a compact strip on the home footer.

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

`tui.ts` reads the panel URL from `RGI_URL` (default `http://127.0.0.1:8730`) and
the token from `RGI_TOKEN` or `~/.config/rgi/token`. If the panel runs on another
machine, set `RGI_URL` in the environment OpenCode is started from, or edit the
default in the file.

### 4. Check OpenCode can see it

```sh
opencode plugin list
```

The plugin is found by directory discovery; nothing belongs in `cli.json`.

### 5. Restart OpenCode, then verify

`~/.config/rgi/plugin.log` should gain `setup() called`, `slot registered:
sidebar.content` and `slot registered: home.footer.status`. The sidebar should
show a block like:

```
⌨ Panel
▸  1 ▶ laptop        the session you are looking at
   2 ✔ build-box     some other session
```

If the log says `could not install OpenTUI runtime support` or
`jsx() failed: No renderer found`, report those exact lines - the plugin will
have fallen back to rendering nothing rather than breaking the TUI.

### Rules

1. **Do not edit `cli.json`.** Directory discovery is what is verified.
2. **Keep the plugin's log lines.** A failed TUI plugin is otherwise silent.
3. **Report what you observed**, not what should have happened.

## PASTE TO HERE
