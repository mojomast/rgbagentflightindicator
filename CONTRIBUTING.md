# Contributing

Thank you for considering it. The single most valuable contribution to this
project is **hardware support** — every board is a different dialect, and the
list of supported keyboards only grows when somebody who owns one writes it down.

This document is the whole process, end to end. It should take five minutes to
read. Questions that are not bugs or proposals belong in
[Discussions](https://github.com/mojomast/rgbagentflightindicator/discussions),
not in the issue tracker.

> [!TIP]
> Adding a keyboard? Read [Adding hardware support](#adding-hardware-support)
> first and open a **New device** issue *before* writing code. It takes two
> minutes and can save you a week.

## Contents

- [Ground rules](#ground-rules)
- [Setting up to work on it](#setting-up-to-work-on-it)
- [Adding hardware support](#adding-hardware-support)
- [What a new backend must prove](#what-a-new-backend-must-prove)
- [Working with plugins and the UI](#working-with-plugins-and-the-ui)
- [House style](#house-style)
- [Commits and pull requests](#commits-and-pull-requests)
- [What will not be merged](#what-will-not-be-merged)
- [Reporting a bug](#reporting-a-bug)
- [Licence](#licence)

## Ground rules

1. **One pull request, one functional area.** A new keyboard and a refactor of
   the daemon are two pull requests. This is the rule most contributions break,
   and the one that slows review down the most.
2. **Never claim more than you verified.** "Works on my board" is a perfectly
   good claim. "Works" is not. Every backend in this repository states which
   parts were confirmed on hardware and which were not — that honesty is what
   makes the rest of it trustworthy.
3. **Open a draft pull request** while you are still working, and take it out of
   draft when it is ready for review.
4. **Discuss significant work first.** A new backend is significant; a typo is
   not. An issue saves you from building something that does not fit.
5. **Do not commit secrets.** Tokens, tailnet names, private addresses and local
   paths do not belong in the repository — `tools/scan_for_secrets.py` fails the
   build if it finds any, and it has caught real leaks, including in
   documentation examples.
6. **Hardware safety.** This project only ever writes **LED feature and output
   reports**. It does not touch firmware, DFU or bootloader interfaces, and no
   backend may add that. A status panel is not worth a bricked keyboard.

## Setting up to work on it

Python 3.10+ and nothing else for the core; the backends that talk to hardware
need `hidapi`.

```sh
git clone https://github.com/mojomast/rgbagentflightindicator
cd rgbagentflightindicator
pip install -e .
python -m unittest discover -s tests -t .
```

The test suite needs **no hardware and no network**: the daemon is exercised over
real HTTP against a dummy backend, and the device protocols are checked byte for
byte. If your change needs a device to test, that is a sign the logic should be
split so it does not.

Before opening a pull request, these must pass:

```sh
python -m unittest discover -s tests -t .     # all tests
cd rgi/webui && node --test && cd ../..       # browser modules (Node 18+, no npm)
python tools/scan_for_secrets.py              # no tokens, addresses, local paths
python tools/check_portability.py             # no shell traps that break on POSIX
python tools/check_docs.py                    # fences balanced, links resolve
python tools/check_prompt_sync.py             # prompt code blocks match the files
```

A new agent integration follows the contract in `docs/integrations.md`: one
module that imports its framework lazily, a line in
`rgi/integrations/__init__.py`, a test against `tests/mock_panel.py`, and a page
that ends with the supported version. Its tests must pass with none of the
frameworks installed.

Useful while working:

```sh
python -m rgi daemon --backend dummy --verbose   # the whole panel, no hardware
python -m rgi detect                             # what is plugged in, read-only
python -m rgi doctor                             # what is installed on this machine
python -m rgi map --backend <yours>              # light one lamp at a time
```

## Adding hardware support

A backend is **one file** in `rgi/backends/`, one entry in the registry, a test,
a row in the device matrix, and a protocol note. [docs/backends.md](docs/backends.md)
is the long form: which route to try for a given brand, and how to reverse
engineer a board nobody has written down.

Before you write code, open a **New device** issue containing:

- the device name as sold, and where you bought it if it is a rebrand;
- `VID:PID`, and the manufacturer and product strings;
- `rgi detect` output, and what `lsusb -v` / Windows device properties say;
- which interfaces exist, their usage pages, and their report sizes;
- screenshots or names of the vendor software, if there is any;
- a link to any protocol capture you have (USBPcap/Wireshark, `usbmon`);
- whether anyone else is already working on it (search first — for example, the
  Magic Refiner MK 17 in this project's history was already reported upstream in
  another project, which saved a week).

That issue is where the protocol discussion happens. Link it from your pull
request with `Closes #123`.

### The interface you implement

```python
class MyKeyboardBackend(Backend):
    name = "mykeyboard"
    min_interval = 0.013          # seconds between writes; hardware may need one

    @classmethod
    def available(cls) -> bool: ...      # cheap: is the device or dependency here?
    def open(self) -> None: ...          # claim it, or raise BackendUnavailable
    def close(self) -> None: ...         # release it; safe to call twice
    def lamps(self) -> list[Lamp]: ...   # labels and groups
    def write(self, colours) -> None: ... # paint every lamp
```

Four rules, each of which cost somebody hours:

1. **`write()` paints everything.** Most controllers have no per-key update at
   all, and several repaint the whole panel on any write. Build one frame from
   the full list. The daemon only calls you when the rendered result changed.
2. **Label every lamp, and set `group="number-row"` on the row you would like
   lanes to use.** That is how the lane pool is chosen without configuration. If
   you do not know the key order yet, `str(index)` is an honest label and
   `rgi map` is how the owner finds the row.
3. **Set `min_interval` honestly.** Back-to-back writes are dropped or corrupt
   on some firmware; ~13 ms is what the Sinowealth board needs.
4. **`write()` must not lie.** If a write fails, raise. A silent failure leaves
   the daemon holding a handle the firmware ignores, and the panel appears to
   work while doing nothing — the single worst failure mode this project has.

### Register it, test it, document it

```python
# rgi/backends/__init__.py
from .mykeyboard import MyKeyboardBackend
out["mykeyboard"] = MyKeyboardBackend
```

Tests must not need the hardware. Check the bytes you would send, the way
`tests/test_sinowealth.py` and `tests/test_openrgb.py` do:

```python
def test_colours_land_in_the_right_planes(self):
    frame = self.backend.build(colours, HEADER)
    self.assertEqual(frame[R_START + 1], 10)
```

If the protocol has a frame, a checksum or a packet layout, a byte-level test is
the difference between "works on my board" and something other people can trust.
If the device answers a capability query, use the **real reply** as a fixture and
say where it came from.

Then:

- add a row to the device matrix in [docs/backends.md](docs/backends.md), with the
  **verified** column filled in truthfully;
- add a section to [docs/protocols.md](docs/protocols.md): interfaces, framing,
  commands, colour layout, and an explicit *what is not known* list;
- mention `min_interval`, whether the device repaints on every write, and whether
  it drops input while busy — the daemon has behaviour for each.

## What a new backend must prove

Fill this in on the pull request. It is the checklist the review is done against,
adapted from what OpenRGB, OpenRazer and INDI ask of a new device.

| | |
|---|---|
| **Detected** | `rgi detect` lists it, and reports the right lamp count |
| **Painted** | every lamp lights, and each in the colour asked for |
| **Lanes** | `rgi map` walks them one at a time, and the row you chose is the row that lights |
| **Platforms** | which OS and version you tested: Windows build, Linux distro and kernel, macOS release |
| **Idempotent** | repeating a frame changes nothing visible; the panel does not flicker |
| **Survives** | a replug, a daemon restart, and a second `rgi daemon` being refused rather than fighting |
| **Releases** | `close()` hands the device back to its own firmware |
| **Not verified** | **what you could not test** — per-key colour order, other revisions of the board, macOS, whatever it is |

If the protocol came from capture rather than from a vendor document, attach the
capture or describe it well enough that the next person can re-derive your work.
Do not attach vendor software binaries.

## Working with plugins and the UI

The OpenCode plugin in `plugin/` has four non-obvious rules that cost this
project a full day each. They are documented in
[docs/opencode.md](docs/opencode.md); the short version:

- the module must be `{ id, setup }` — the v1 shape `{ id, tui }` is rejected;
- the entrypoint is `tui.ts`, built **without** a JSX transform, so elements come
  from `jsx()` at runtime;
- slots take exactly one placement key: `context.ui.slot({ append: "sidebar.content", render })`;
- install OpenTUI's Solid runtime support before using the JSX runtime, and render
  `null` rather than something the host cannot mount.

Two rules for anything interactive:

- **Detection is read-only.** Never claim a device another program is driving:
  a stale handle accepts writes and sends them nowhere, and the panel then looks
  alive while doing nothing.
- **Never return a value the host cannot mount** — a plugin that throws is
  disposed, and the failure is silent.

The plugin is published to other machines from the panel's `GET /files`, so a
change here reaches a fleet by fetching, never by hand-editing a local copy.

## House style

- **Python**: standard library only in the core. `hidapi` is the one optional
  dependency, and only for backends that need it. Match the file you are editing;
  the code is deliberately plain, with docstrings that explain *why* where the
  reason is not obvious. No emoji in code.
- **Tests**: `unittest`, no pytest, no fixtures beyond `tempfile` and threads.
  A test that needs hardware belongs in the manual checklist instead.
- **Markdown**: keep `docs/` truthful. If behaviour changes, the documentation is
  part of the change — a doc that describes a workflow the project no longer
  follows is worse than no doc at all.
- **Shell examples**: wrap anything that could hang in a timeout, and prefer the
  full tailnet name over an IP where a name exists (short names can resolve to a
  link-local IPv6 address first and hang).

## Commits and pull requests

Commit subjects: `type: summary`, where type is one of `feat`, `fix`, `docs`,
`test`, `refactor`, `chore`, `perf`. The body explains **why**, not what — the
diff already shows what.

```
fix: make detect read-only so it cannot steal the device from a running daemon

Opening the Sinowealth backend sends the unlock and the mode commit. Run while a
daemon was already driving the board, that took the device away from it and left
the daemon holding a handle the firmware no longer honoured: writes appeared to
succeed and went nowhere.

Found the honest way - by doing it to a running panel with the first version of
this command.
```

A pull request is ready when:

- [ ] one functional area, based on current `main`
- [ ] tests pass, and new behaviour has tests that do not need hardware
- [ ] `tools/scan_for_secrets.py` and `tools/check_portability.py` pass
- [ ] documentation updated in the same change (`docs/`, `README.md`, device matrix)
- [ ] the [proving checklist](#what-a-new-backend-must-prove) is filled in, **including what you could not verify**
- [ ] commit messages follow the format above

Use the pull request template — it asks for exactly those things.

## What will not be merged

- **Untested hardware support presented as working.** A backend that has never
  seen the device is welcome *if it says so*, is tested byte-wise, and is marked
  unverified in the device matrix.
- **Firmware, DFU or bootloader code.** LED reports only.
- **Code that claims a device another program is driving**, or that writes to a
  device without a way to release it.
- **A second installation path.** One plugin directory, one watcher per machine;
  `rgi doctor` exists because duplicates are confusing and common.
- **Secrets or machine-specific values.** Private addresses, tailnet names, tokens,
  local user paths — the scan will catch them, and it is not negotiable.
- **Mixed-scope pull requests.** Split them.

## Reporting a bug

Include:

- what you expected and what happened;
- `rgi doctor` output, and `rgi detect` output;
- the backend name, and the OS/version;
- `~/.config/rgi/watcher.log` and `~/.config/rgi/plugin.log` if the watcher or
  plugin is involved;
- the exact error text, not a paraphrase.

[docs/troubleshooting.md](docs/troubleshooting.md) is organised by symptom and
answers a lot of this before you file anything.

## Licence

MIT. By contributing you agree your work is published under it — see
[LICENSE](LICENSE). If your change includes someone else's work, make sure they
agree to that too.
