"""
Fishing mode — shiny / target hunting on water tiles.

Mechanically a shiny-hunt with a different idle action: instead of
walking grass or Sweet-Scenting, the bot presses **Y** to use the
player's registered key item (assumed to be a fishing rod) and then
hooks with **A** the moment a wild record lands in the foe window.
The hook either starts a wild battle (→ standard detection + target
check + catch/flee) or nothing happens and the loop recasts.

**Player setup (one-time, manual):**

1. Have an Old / Good / Super Rod in your bag.
2. Bag → Key Items → rod → **Register** so pressing Y in the
   overworld uses it.
3. Stand facing a fishable water tile (any pier, the dock at Couriway
   Town, Route 8/16/22, Cyllage / Ambrette beach, etc.).

Detection / flee are the same engine as `encounter.run`; only the
idle action differs. The hook follows DETECTION rather than a
timer, so it does not depend on guessing a bite window the bot cannot
see; a cast that catches nothing clears its text and recasts.

The player does not move. A fishing hunt stands facing one water tile,
so the left/right stepping the walking hunt does between encounters
would walk it off the spot.
"""
from __future__ import annotations

import logging
from dataclasses import replace

from .encounter import run as _encounter_run

log = logging.getLogger(__name__)

#: Fishing-specific defaults.
#:
#: flee_delay: the fishing intro has its own cutscene (rod reel, fish
#: leap, "Oh! A bite!"), so it needs more headroom than a grass
#: encounter before the RUN touch can land -- but nothing like the 9 s
#: this used to take. That was tuned against a 100% emulator; the hunt
#: runs Azahar around 600%, where the cutscene is over in well under a
#: second. If a flee ever fires early the stall watchdog catches it.
#:
#: flee_intro_taps / flee_clear_taps: ZERO. The walking hunt presses B
#: either side of the RUN touch to clear battle text, but here the
#: flee is nothing but the screen press. The delay above is what lets
#: the command menu finish drawing, and nothing is walking around
#: afterwards that needs text cleared out of its way.
_DEFAULTS = {
    "idle_action": "fish",
    "flee_delay": 2.0,
    "flee_intro_taps": 0,
    "flee_clear_taps": 0,
}


def run(ctx):
    # See horde.run: never setdefault into the shared ctx.config.
    rcfg = ctx.config.get("random_encounters") or {}
    merged = {**ctx.config, "random_encounters": {**_DEFAULTS, **rcfg}}
    ctx = replace(ctx, config=merged)
    log.info("Mode: fishing (Y → A-spam, foe-window detection; "
             "stops on shiny / target)")
    log.info("  Setup: rod registered to Y, standing facing water.")
    _encounter_run(ctx)
