#!/usr/bin/env python3
"""rgbagentflightindicator - turn an RGB keyboard into an agent status panel.

Like an aircraft annunciator panel: every agent session claims one lamp and the
colour tells you its state at a glance.

    green   in flight
    white   complete (blinks when it lands)
    red     needs a human, blinking
    off     everything else

The daemon owns one or more *backends* (keyboard drivers); agents talk to it over
HTTP. See README.md and docs/ for the full picture.
"""

# Keep this in step with pyproject.toml and, for the displayed short form, with
# VERSION_LABEL in plugin/tui.ts.
VERSION = "0.8.0"
VERSION_LABEL = "rgbafi v0.8"

__version__ = VERSION
