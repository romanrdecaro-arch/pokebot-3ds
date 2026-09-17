"""
Rock Smash mode — shiny / target hunting on breakable rocks.

Mechanically a shiny-hunt with a different idle action: instead of
walking grass or casting a rod, the bot presses **A** at the rock in
front of the player to use Rock Smash, and stops pressing the instant
a wild record lands in the foe window. From there everything is the
shared engine: the encounter is reported and target-checked, and a
shiny runs the normal catch sequence. Anything else -- including
nothing at all -- ends in a soft reset.

**Player setup (one-time, manual):**

1. A party Pokémon that knows **Rock Smash** (TM94 in X/Y).
2. Stand **facing a breakable rock** — Glittering Cave, Connecting
   Cave, Reflection Cave, Terminus Cave, Route 9.
3. **SAVE there.** Every attempt starts from that save, because every
   attempt ends in a soft reset.

**Every attempt ends in a soft reset.** No shiny, or no encounter at
all within 15 s, and the game is relaunched. That is the loop, not an
error path: a smashed rock is *gone*, and nothing brings it back but
reloading the area — so fleeing a bad encounter would leave the bot
standing in front of rubble, pressing A at nothing. The reset respawns
every rock and puts the player back in front of one.

Which makes step 3 above a hard requirement rather than a convenience.
The reset returns to the **save**, so wherever the save is, is where
every attempt starts.

**Rock Smash does not guarantee an encounter.** Most smashes give
nothing at all, which is why the stall watchdog is 15 s here against
the walking hunt's 60: a quiet stretch is the normal case, not a
fault to wait out, and the sooner it resets the sooner the next
rock exists.

One consequence worth knowing: because the reset reloads the save, a
**catch is not safe until you save it**. The hunt therefore STOPS the
moment it catches something, rather than resuming into a reset that
would undo it.
"""
from __future__ import annotations

import logging
from dataclasses import replace

from .encounter import run as _encounter_run

log = logging.getLogger(__name__)

#: Settings that only this mode wants, and that nothing else writes.
_DEFAULTS = {
    "idle_action": "rock_smash",
    # No shiny, or no encounter at all -> relaunch the game.
    #
    # This is not an error path, it is the loop. A smashed rock is
    # GONE, and nothing brings it back but reloading the area -- so
    # fleeing a bad encounter would leave the bot standing in front of
    # rubble, pressing A at nothing until the watchdog gave up. The
    # reset respawns every rock on the map and puts the player back in
    # front of one, which is why this is how Rock Smash is hunted.
    #
    # It has a hard requirement attached: the player must have SAVED
    # facing the rock. The reset returns to the save, so wherever that
    # is, is where every attempt starts.
    "no_target_action": "soft_reset",
}

#: Settings this mode must OVERRIDE rather than default.
#:
#: The merge below is ``{**defaults, **user_config}`` so that anything
#: the user set wins -- which means a mode default can never beat a
#: key the shipped config.yaml already sets, because it is a default
#: and that key is set. Both of the values this mode needs to differ
#: on are exactly that kind of key:
#:
#:   stuck_timeout  config.yaml ships 60, tuned for a walking hunt
#:                  where a long silence means the RUN touch missed.
#:                  Rock Smash has no guaranteed encounter, so silence
#:                  is the normal case rather than something to wait
#:                  out, and 15 gets the loop restarted four times as
#:                  often.
#:   flee_delay     config.yaml ships 1.5. Kept as headroom for the
#:                  rare case where a flee still runs (no_target_action
#:                  turned back to "flee" by hand).
#:
#: So each gets a rock-smash-specific key instead. Setting one of
#: those still wins; leaving it alone gets the value this mode needs
#: rather than the walking hunt's.
#:
#: mapped key -> (the key a user sets to change it, default)
_OVERRIDES = {
    "stuck_timeout": ("smash_stuck_timeout", 15.0),
    "flee_delay": ("smash_flee_delay", 2.0),
}


def merged_config(rcfg: dict | None) -> dict:
    """The ``random_encounters`` block this mode actually runs with.

    Separate from ``run`` so the effective values can be asserted
    against the SHIPPED config. That distinction is not academic: the
    stall timeout below once read correctly off this mode's defaults
    while the hunt actually waited the walking hunt's 60, because
    config.yaml sets that key and a default cannot beat a set key.
    """
    rcfg = rcfg or {}
    merged = {**_DEFAULTS, **rcfg}
    for key, (own_key, default) in _OVERRIDES.items():
        raw = rcfg.get(own_key, default)
        try:
            merged[key] = float(raw)
        except (TypeError, ValueError):
            log.warning(f"  {own_key}={raw!r} is not a number; "
                        f"using {default}")
            merged[key] = float(default)
    return merged


def run(ctx):
    # See horde.run: never setdefault into the shared ctx.config. That
    # dict is the process-wide config object, and every mode here
    # writes DIFFERENT values to the SAME keys, so mutating it lets
    # whichever mode ran first win for the next one.
    merged = {**ctx.config,
              "random_encounters": merged_config(
                  ctx.config.get("random_encounters"))}
    ctx = replace(ctx, config=merged)
    rcfg = merged["random_encounters"]
    log.info("Mode: rock smash (A at the rock, foe-window detection; "
             "stops pressing on any wild, catches shiny / target)")
    log.info("  Setup: a party member that knows Rock Smash, SAVED "
             "facing a breakable rock.")
    if rcfg.get("no_target_action") == "soft_reset":
        log.info("  Every attempt ends in a soft reset, so every "
                 "attempt starts from that save.")
        log.info("  A catch STOPS the hunt — save it in-game before "
                 "starting again, or the next reset undoes it.")
    log.info(f"  Rock Smash has no guaranteed encounter — quiet "
             f"stretches are normal; the loop resets itself every "
             f"{rcfg['stuck_timeout']:.0f}s.")
    _encounter_run(ctx)
