# Troubleshooting

Symptoms first, causes second. Nearly every entry here cost hours to find.

## The keyboard

**Nothing lights up at all, and the keyboard is showing its own effect.**
Something else took the device out from under the daemon — most often another
program opening the same HID interfaces (`rgi detect --open`, `rgi map`, a vendor
app, or a second daemon). The daemon keeps a handle the firmware no longer
honours, so its writes go nowhere while still appearing to succeed. Restart the
daemon. `rgi detect` is read-only for exactly this reason; only `--open`, `map`
and `daemon` claim the device.

On the Sinowealth board the same symptom has a second cause: per-key frames are
only honoured while the board is in per-key ("game") mode, and it boots into its
stock effect. The current driver enters the mode at open, so a restart fixes
that as well; a driver older than the game-commit fix never enters it at all.
`docs/protocols.md` has the sequence.

**The panel is driving a keyboard that is not the one in front of me.**
The backends attach to different physical boards: `evision` only opens devices
exposing usage page `0xFF1C` (Magic Refiner and kin), `sinowealth` only the
`258A:0049`. If the panel says `evision` while your 258A sits dark, the board it
is driving is unplugged. `rgi detect` shows what is actually present — match the
backend to the board on the desk.

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
The fix is stronger than freezing the animation, because the landing flash itself
was the problem: while you type the daemon writes nothing at all (Windows only
for now), so a lane that lands mid-sentence is held and painted in one frame when
you pause. `--quiet-ms` is how long "typing" lasts after the last keystroke
(default 1500); `--no-quiet` disables the hold.

**The web UI asks for a token, or shows "panel unreachable".**
Open it with `rgi ui`, which passes the token in the URL fragment; a bare
`/ui/` has no token and will ask. The token is kept in `sessionStorage` for the
tab only. If the page is open on another machine, use the same token as the
agents (`~/.config/rgi/token`). A 401 clears the stored token and reopens the
dialog; it never retries in a loop.

**The web UI's webcam mode says the camera needs a secure context.**
`getUserMedia` only works on `http://localhost`, `http://127.0.0.1` or HTTPS —
not on a LAN or Tailscale address. Open the UI on the machine with the camera,
or use manual press-to-label mapping, which works everywhere.

**I changed host/port/count and nothing happened.**
Those are startup flags: the UI saves them and shows a "restart required"
banner. Stop and start the daemon; your other changes were already applied live.

**Apply says another tab changed the config.**
Two tabs were editing the same revision. *Reload from panel* takes the newer
file and discards the draft; *Overwrite* applies your draft on top. Nothing is
merged silently, and the previous file stays in the backup chain either way.

**My keyboard looks like a grid of numbered lamps.**
No layout has been assigned yet, so the UI is drawing an honest approximation
from lamp order. In Layout, either *Use this template* from the built-in
profile banner (geometry only, no lamp identities claimed) or run **Identify**
to map the real keys. The device also reports which kind it thinks each lamp
is; unknown lamps render dashed.

**The webcam scan put key lamps in the base/logo group.**
Classification is a suggestion, not a verdict. Check that the four corner
clicks really landed on the corners of the key area (the overlay shows where it
expects keys), dim the room, and use *Name…* / *Mark base* in the review table
to correct rows. Manual press-to-label always works and is the ground truth.

**A "restored an unsaved draft" banner appeared.**
The UI autosaves the draft to the browser tab's `sessionStorage` and offers it
back after a reload. *Keep it* continues where you left off; *Discard it* goes
back to the last applied config. Nothing is applied until you press Apply.

**The map shows dashed outlines or a dashed strip.**
Dashed means unverified: a lamp whose identity comes from a built-in profile,
auto-layout, or a webcam detection below 50% confidence. It still paints and
can be pooled, but Identify is what turns it solid.

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
turn, which looked exactly like this. The current watcher rule is `rgi watch
--stale` seconds of inactivity (two hours by default); the daemon itself only
releases lanes explicitly or by least-recently-used eviction.

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

**A subagent is holding its own lamp, or a long-running child vanished from its
parent's lane.** Both are fixed in the current watcher, and both come from an
older copy of it: children used to be bound when they appeared between metadata
refreshes, and they were listed by a record timestamp that does not move while a
child works. Fetch `rgi-watch.py` from `/files` again, restart it, and any lamp a
child is holding is handed back on the next pass.

## Reach (notifications, ack, MQTT, ingest)

**A blocked lane keeps blinking after I answered the prompt.**
That is the ecosystem-wide stale-wait problem: a hook-driven reporter can only
clear a wait when a later event resolves the same request id. If the agent's
tool ran long, the lamp can stay red until its next event. Click **Ack** (web UI
or sidebar) or `POST /session/ack`: the lane keeps its colour but stops blinking
and stops notifying; the next real state change re-arms it. If the reporter is
one you wrote, resolve waits by id, never with a timeout.

**A lane stays green although the agent died.**
`settings.stale_s` (default 900) is how long a working lane may go without a
state change, info update or `heartbeat` before the UI marks it `stale?`. That
marker is display-only; the lamp itself is never changed by staleness. Long
tools should send `heartbeat()` from the reporter to stay honest.

**No notification arrived for a blocked lane.**
Check, in order: `notify.enabled` is true; `states` includes `blocked`;
`dry_run` is off; the lane is blocked longer than `min_duration_s`; the global
`cooldown_s` has elapsed; `quiet_hours` is not suppressing it (blocked should
break through); and the command itself works when run by hand with the payload
on stdin. Every decision the notifier makes lands in the Logs page.

**The notifier fires too much.**
Raise `min_duration_s` (quick turns stop qualifying), raise `cooldown_s`, set
`states` to just `["blocked"]`, or add quiet hours. Never notify on `done`
unless you actually want a per-turn push; that is the classic way to train
yourself to ignore the channel.

**MQTT entities do not appear in Home Assistant.**
Install the optional dependency (`pip install paho-mqtt`), confirm the broker
address/credentials, and make sure the daemon was restarted after enabling
`mqtt` (the Reach settings are restart-required). Entities are published under
`{base_topic}/{ident}/...` with discovery configs under
`homeassistant/sensor/<ident>/...`; the availability topic flips to `offline`
via Last Will if the daemon dies.

**A hook or webhook did nothing.**
The response to `POST /hook/<source>` says how many actions were applied, and
the Logs page records "no free lane" and delivery dedup decisions. A second
delivery with the same id (GitHub/GitLab re-delivery, Codex retry) is ignored on
purpose. Unknown event names map to zero actions rather than guessing.

**`rgi digest` says there is no history yet.**
History starts when the feature is enabled and lanes change; the file is
`~/.config/rgi/history/events.jsonl`, capped and rotated. `--since 8h`, `30m`,
`2d` or an epoch all work.

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
