"""
Rock Smash mode — shiny / target hunting on breakable rocks.

Mechanically a shiny-hunt with a different idle action: instead of
walking grass or casting a rod, the bot presses **A** at the rock in
front of the player to use Rock Smash, and stops pressing the instant
a wild record lands in the foe window. From there everything is the
shared engine: the encounter is reported and target-checked, a shiny
runs the normal catch sequence, and anything else is fled.

**Player setup (one-time, manual):**

1. A party Pokémon that knows **Rock Smash** (TM94 in X/Y).
2. Stand **facing a breakable rock** — Glittering Cave, Connecting
   Cave, Reflection Cave, Terminus Cave, Route 9.
3. That is all. The bot never moves the player, so the rock stays in
   front of it.

**Rock Smash does not guarantee an encounter.** Most smashes give
nothing at all, which is the whole reason this mode exists separately
from the walking hunt: a quiet stretch is normal here rather than a
fault. So the stall watchdog is shortened to **30 s** and its recovery
is a screen clear (B) rather than the walking hunt's RUN touch —
there is usually no battle to run from, and that touch would land on
the PSS in the overworld.

One thing to watch in-game: a smashed rock is gone until the area
reloads. If the bot settles into 30-second resets that never produce
anything, step out of the room and back in to respawn the rocks —
the bot deliberately does not walk anywhere on its own, because the
one thing it must not do is wander off the rock it is aimed at.
"""
from __future__ import annotations

import logging
from dataclasses import replace

from .encounter import run as _encounter_run

log = logging.getLogger(__name__)

#: Settings that only this mode wants, and that nothing else writes.
_DEFAULTS = {
    "idle_action": "rock_smash",
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
#:                  is the normal case and 30 gets the loop restarted
#:                  twice as often.
#:   flee_delay     config.yaml ships 1.5. The battle here opens behind
#:                  the rock-break animation and wants a little more
#:                  headroom before the RUN touch can land.
#:
#: So each gets a rock-smash-specific key instead. Setting one of
#: those still wins; leaving it alone gets the value this mode needs
#: rather than the walking hunt's.
#:
#: mapped key -> (the key a user sets to change it, default)
_OVERRIDES = {
    "stuck_timeout": ("smash_stuck_timeout", 30.0),
    "flee_delay": ("smash_flee_delay", 2.0),
}


def merged_config(rcfg: dict | None) -> dict:
    """The ``random_encounters`` block this mode actually runs with.

    Separate from ``run`` so the effective values can be asserted
    against the SHIPPED config, which is where the difference between
    "the default says 30" and "the hunt waits 30" showed up.
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
    log.info("  Setup: a party member that knows Rock Smash, standing "
             "facing a breakable rock.")
    log.info(f"  Rock Smash has no guaranteed encounter — quiet "
             f"stretches are normal; the loop resets itself every "
             f"{rcfg['stuck_timeout']:.0f}s.")
    _encounter_run(ctx)
