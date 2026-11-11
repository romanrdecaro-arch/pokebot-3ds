"""
Gen 7 (Sun / Moon, Ultra Sun / Ultra Moon) fishing.

The loop, as asked for:

    A to cast at the fishing spot
    keep pressing A until the battle starts -- that is the hook landing
    nothing in the bite window -> B, B to clear the text, A to cast again
    shiny (or target)  -> catch it (on_target: stop leaves it on screen)
    anything else      -> run, and cast again

Why A is pressed blind. The first version hooked the way X/Y does: wait
for the wild record to land in the foe windows, then press A. A live
Ultra Moon run (2026-11-11) saw nothing in 18 casts while bites came and
went: Gen 7 writes the wild only once the battle starts, so there is no
bite to see in memory. What the bot CAN see is the battle itself (the
Battle module loading), so it presses A through the whole bite window,
and the press that lands on the "!" is the one that starts the battle.

Every press follows a check of the module list, so no A ever lands in a
battle: there A is FIGHT, and the next one a move -- into the shiny.

shortcut: assumes an A before the "!" does not cost the cast (or, if it
reels in, that the next A simply casts again); not yet checked live. If
casts keep ending with "nothing", that is the assumption failing.
"""
from __future__ import annotations

import logging
import time
from dataclasses import replace

from ..azahar_recover import AzaharGone
from ..citra_rpc import RPCError
from ..pk6_export import ensure_targets_dir
from . import fishing_loop
from .gen7_encounter import (Modules, Tally, WigglePlan, _leave_leftover_battle,
                             _tap, find_wild, settle)
from .observe import foe_windows, scan_windows
from .soft_reset import _focus_if_needed

log = logging.getLogger(__name__)

#: How long each blind A is held. With the module check before it, a
#: press every ~50-80 ms: well inside a bite window at any Azahar speed.
_MASH_HOLD = 0.04
#: Casts in a row with no battle before the log says the hook is the
#: problem, not the luck.
_DRY_CASTS = 25

HOOKED, NO_BITE, STOPPED = "hooked", "no-bite", "stopped"


def run(ctx) -> None:
    try:
        _run(ctx)
    except AzaharGone:
        log.error("  Azahar is GONE -- it crashed. Nothing more is being "
                  "pressed. Start Azahar, load the save and start the hunt "
                  "again.")
        ctx.request_stop("Azahar crashed")


def _in_battle(mods) -> bool:
    try:
        return "Battle" in mods.names()
    except (RPCError, OSError):
        return True                      # a dropped read: never press into it


def cast(ctx, mods, fish) -> str:
    """A to cast, then A until the battle starts or ``bite_timeout``."""
    if ctx.should_stop():
        return STOPPED
    _tap(ctx, "A", hold_s=0.05)
    deadline = time.monotonic() + fish.bite_timeout
    while not ctx.should_stop():
        if _in_battle(mods):
            return HOOKED
        if time.monotonic() > deadline:
            return NO_BITE
        _tap(ctx, "A", hold_s=_MASH_HOLD)
    return STOPPED


def _run(ctx) -> None:
    log.info("Mode: Gen 7 fishing (A to cast, A until the battle, B, B on "
             "nothing)")
    game = ctx.game
    if getattr(game, "generation", None) != 7:
        log.error("  this mode is for Sun / Moon / Ultra Sun / Ultra Moon.")
        ctx.request_stop("not a Gen 7 game")
        return
    rcfg = ctx.config.get("random_encounters") or {}
    plan = WigglePlan.from_config(rcfg)
    # The Gen 6 keys (fish_cast_button: Y) must not leak in: Alola casts on A.
    fish = replace(fishing_loop.FishPlan.from_config(rcfg), clear_button="B",
                   clear_taps=2)
    ensure_targets_dir()
    _focus_if_needed(ctx)
    windows = foe_windows(game.offsets)
    mods = Modules(ctx.rpc)
    if _in_battle(mods) and not _leave_leftover_battle(ctx, mods, windows,
                                                       plan):
        return
    seen = {p.encryption_key for _, p in scan_windows(ctx, windows, set())}
    log.info(f"  Setup: standing facing a fishing spot. {len(seen)} old "
             f"record(s) ignored. A shiny is "
             f"{'caught' if plan.catch else 'left on screen, all input stopped'}.")

    tally, dry = Tally(), 0
    while not ctx.should_stop():
        got = cast(ctx, mods, fish)
        if got == STOPPED:
            break
        if got == NO_BITE:
            dry += 1
            log.info(f"  fishing: no battle in {fish.bite_timeout:.1f}s - "
                     f"B, B and casting again")
            if dry == _DRY_CASTS:
                log.warning(f"  {dry} casts in a row with no battle. If you "
                            f"see bites on screen, the early A presses may be "
                            f"reeling the line in: paste the log.")
            fishing_loop.clear_text(ctx, fish)
            ctx._stop_evt.wait(fish.recast_gap)
            continue
        dry = 0
        wilds = find_wild(ctx, windows, seen, plan.find_timeout)
        seen.update(p.encryption_key for _, p in wilds)
        if not wilds:
            if not _leave_leftover_battle(ctx, mods, windows, plan,
                                          "a battle with no new wild record"):
                return
            continue
        if not settle(ctx, mods, plan, wilds, tally, "gen7 fishing", "casting"):
            return
    log.info(f"Gen 7 fishing stopped after {tally.encounters} encounter(s).")
