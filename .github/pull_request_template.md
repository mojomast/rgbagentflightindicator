## What this changes

<!-- One functional area. If it is a new device, link the issue: "Closes #123". -->

Closes #

## Why

<!-- The reason, not the diff. What was broken, what was missing, what you measured. -->

## How it was tested

<!--
Say what you actually ran, and where. "Windows 11 24H2, Magic Refiner MK 17, 126
lamps" is a useful answer. "Tested locally" is not.
-->

```sh
python -m unittest discover -s tests -t .
python tools/scan_for_secrets.py
python tools/check_portability.py
```

## New device checklist

<!-- Delete this section unless you are adding hardware support. -->

| | |
|---|---|
| **Detected** | `rgi detect` lists it with the right lamp count |
| **Painted** | every lamp lights, in the colour asked for |
| **Lanes** | `rgi map` walks them, and the row chosen for lanes is the row that lights |
| **Platforms** | OS and version tested |
| **Idempotent** | repeating a frame changes nothing visible; no flicker |
| **Survives** | replug, daemon restart, and a second daemon being refused |
| **Releases** | `close()` hands the device back to its own firmware |
| **Not verified** | **what you could not test** |

- [ ] device matrix row added in `docs/backends.md`, with the verified column filled in truthfully
- [ ] protocol section added in `docs/protocols.md`, including what is still unknown
- [ ] a byte-level test that needs no hardware (or an explanation of why it cannot exist)
- [ ] `min_interval`, repaint-on-every-write, and dropped-input-while-busy noted

## Checklist

- [ ] one functional area, based on current `main`
- [ ] tests pass, and new behaviour has tests that do not need hardware
- [ ] documentation updated in the same change
- [ ] commit messages follow `type: summary` and explain why in the body
