"""
Fishing mode — shiny / target hunting on water tiles.

Mechanically a shiny-hunt with a different idle action: instead of
walking grass or Sweet-Scenting, the bot presses **Y** to use the
player's registered key item (assumed to be a fishing rod) and then
spams **A** through the bite window. The hook either lands a wild
battle (→ standard scan_nonparty detection + target check + flee) or
nothing happens and the next iteration recasts.

**Player setup (one-time, manual):**

1. Have an Old / Good / Super Rod in your bag.
2. Bag → Key Items → rod → **Register** so pressing Y in the
   overworld uses it.
3. Stand facing a fishable water tile (any pier, the dock at Couriway
   Town, Route 8/16/22, Cyllage / Ambrette beach, etc.).

Detection / flee are the same engine as `encounter.run`; only the
idle action differs. Hit rate is slightly below a perfect-timing
human (some bites get fumbled when the A-spam aligns with the wrong
phase) but the loop is patient — misses just recast.
"""
from __future__ import annotations

import logging
from dataclasses import replace

from .encounter import run as _encounter_run

log = logging.getLogger(__name__)

#: Fishing-specific defaults.
#:
#: The fishing intro does have its own cutscene (rod reel, fish leap,
#: "Oh! A bite!"), so it needs more headroom than a grass encounter
#: before the RUN touch can land -- but nothing like the 9 s this used
#: to take. That number was tuned against a 100% emulator; the hunt
#: runs Azahar around 600%, where the same cutscene is over in well
#: under a second. If a flee ever fires too early the stall watchdog
#: catches it within a minute.
_DEFAULTS = {
    "idle_action": "fish",
    "flee_delay": 2.0,
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
