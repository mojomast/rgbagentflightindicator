# Troubleshooting

Symptoms first, causes second. Nearly every entry here cost hours to find.

## The keyboard

**Nothing lights up at all.**
Check the backend: `rgi detect`. If it reports no lamps, the device is not
visible — for USB boards, check the cable and that the keyboard is on **wired
USB**: many gaming keyboards drop off the USB bus entirely in Bluetooth or
2.4 GHz mode, and no amount of software will reach them.

**One interface accepts writes and ignores them.**
Classic. The Sinowealth board exposes a plausible-looking vendor interface that
swallows every report; the working one is elsewhere (`docs/protocols.md` has the
details). If your writes "succeed" and nothing changes, try the other interfaces
before assuming the protocol is wrong.

**Half the keys keep their old colour.**
Several boards split their LEDs across two reports or banks. Both must be written
every frame, or the untouched half retains whatever was there.

**The whole panel flashes on every update.**
Most controllers repaint everything on any write; there is no per-key update.
This is why the daemon diffs the rendered frame and only writes on change. If you
see constant flashing, something is changing the frame constantly — a blinking
lane is the intended case, a mismatched `min_interval` is not.

**Colours appear on the wrong keys.**
Your lamp indices do not line up with the keys. Run `rgi map`, note what lights
up, and pass `--lanes` explicitly.

**A key stops responding to my typing while the panel animates.**
Real firmware behaviour: the controller is busy repainting and drops keypresses.
The daemon freezes the frame on steady colours while you type (Windows only for
now); `--quiet-ms` tunes the window and `--no-quiet` disables it.

**It worked, I replugged, now nothing changes.**
A replug invalidates the device handle: writes appear to succeed and go nowhere.
The daemon notices write failures, reopens and repaints; if a backend does not
report its failures honestly, fix the backend — silence is the symptom that costs
an evening.

## The panel

**`409 no free lamps`.**
Every lamp is taken. Expected with a dozen lamps: `GET /slots` shows who holds
what, and `POST /clear` releases everything if you are the human cleaning up. A
new claim evicts the least recently used lane automatically — but never evicts a
lane that is currently `working`.

**A lamp vanished while I was still using it.**
Check for a `--hold` flag. An early build freed each lamp 45 seconds after every
turn, which looked exactly like this. The current rule is `--stale` seconds of
inactivity, two hours by default.

**A lamp never turns green again on the second turn.**
Also the old behaviour: a session that had already landed was never sent
`working` again. If your watcher has this bug, look for a branch that skips
sessions which are already bound. `watcher.log` should show `[go] … (working
again)`; if it does not, your watcher predates the fix.

**`400` on a call I think is correct.**
The response says why. On a shell, `400` almost always means quoting: the JSON
must reach the server intact. Write the body to a file and post `-d @file`, or
use your language's HTTP client rather than a shell.

**`401`.**
Wrong or missing `X-LED-Token`. The daemon prints whether auth is on at startup;
the token lives in `~/.config/rgi/token` on the panel's machine.

**`404` on `/session/state`.**
You are not holding a lamp: never claimed one, released it, or the panel was
restarted. Claim again — if you are a watcher, ask for the same lamp.

## The OpenCode plugin

**`Invalid V2 TUI plugin module: <dir>`.**
The default export is not `{ id, setup }`. The v1 shape `{ id, tui }` is rejected.

**`N errors building tui.ts`.**
JSX inside a `.ts` file. Use `jsx()` from the runtime instead of JSX syntax.

**The sidebar is blank, plugin.log says `No renderer found`.**
OpenCode imported the plugin before installing OpenTUI's Solid runtime support.
The plugin installs it itself; if it still fails, the log line names the step that
broke. See `docs/opencode.md`.

**The plugin loads and then disappears.**
Returning a value the host cannot mount gets the plugin disposed. Render `null`
when the JSX runtime is unavailable rather than a string.

**`Could not find package '@opentui/...'`.**
The peer dependencies are missing from the OpenCode config directory:
`npm install @opentui/core @opentui/solid solid-js` there, not in the plugin
folder.

## The watcher (and anything else that shells out)

**`TimeoutExpired` reading OpenCode's API, on Linux or macOS only.**
`subprocess.run([...], shell=True)` runs **only the first element** of the list on
POSIX — the rest become the shell's `$0`, `$1`… So
`["opencode", "api", "get", path]` launched a bare `opencode` and waited. Windows
hides this because `cmd.exe` joins the list back together. Never pass `shell=True`
with a list; resolve the binary with `shutil.which()` (which also finds the npm
`.cmd` shim on Windows) and pass the list directly.

**The watcher exits immediately.**
Run it in the foreground once — it prints why. Usually the panel URL is wrong, or
the panel is not running.

**It works, but the panel shows stale states.**
Look at `~/.config/rgi/watcher.log` timestamps: if they stopped, the watcher
died. If they continue but the panel disagrees, the watcher is not reaching it —
each entry reports what it sent.

## Debugging checklist

1. `rgi detect` — is the device there, and how many lamps?
2. `rgi map` — do the indices match the keys you care about?
3. `python -m rgi daemon --backend dummy --verbose` — does the software half work
   with no hardware in the picture? `rgi push working` should print a frame.
4. `cat ~/.config/rgi/watcher.log` / `plugin.log` — what do the agents think
   happened?
5. `rgi detect --backend openrgb --debug` — what did the protocol parse produce?

If you open an issue, those five outputs answer most questions before they are
asked.
