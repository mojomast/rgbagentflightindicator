"""rgbagentflightindicator - turn an RGB keyboard into an agent status panel.

Like an aircraft annunciator panel: every agent session claims one lamp and the
colour tells you its state at a glance.

    green   in flight
    white   complete (blinks when it lands)
    red     needs a human, blinking
    off     everything else

The daemon owns a *backend* (a keyboard driver); agents talk to it over HTTP.
See README.md and docs/ for the full picture.
"""

__version__ = "0.1.0"
