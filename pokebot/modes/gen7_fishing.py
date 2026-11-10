"""
Gen 7 (Sun / Moon, Ultra Sun / Ultra Moon) fishing.

The loop, as asked for:

    A to cast at the fishing spot
    a wild is detected -> A to hook it -> judge it in battle
    nothing            -> B, B to clear the text, A to cast again
    shiny (or target)  -> catch it (on_target: stop leaves it on screen)
    anything else      -> run, and cast again

The X/Y fishing loop (fishing_loop.cast_once), with Alola's buttons: a
fishing spot is fished with A, not a registered rod on Y. The hook still
follows DETECTION -- a new wild record in the foe windows -- and the
battle is judged, caught or run from exactly as the Gen 7 grass hunt
does it (gen7_encounter.settle).

shortcut: the hook relies on Gen 7 writing the wild before the hook, as
X/Y does; not yet checked live. If every cast logs "nothing", the record
only lands once the battle starts and the bite needs another signal.
"""
from __future__ import annotations

import logging
import time
from dataclasses import replace

from ..azahar_recover import AzaharGone
from ..citra_rpc import RPCError
from ..pk6_export import ensure_targets_dir
from . import fishing_loop
from .foe_watch import FoeWatch
from .gen7_encounter import (Modules, Tally, WigglePlan, _leave_leftover_battle,
                             find_wild, settle)
from .observe import foe_windows, read_pk6_at, scan_windows
from .soft_reset import _focus_if_needed

log = logging.getLogger(__name__)

#: Seconds from the hook to the battle's modules loading before the bite
#: counts as one that got away.
_HOOK_TO_BATTLE_S = 6.0
#: Casts in a row with nothing before the log says detection may be the
#: problem, not the luck.
_DRY_CASTS = 25


def run(ctx) -> None:
    try:
        _run(ctx)
    except AzaharGone:
        log.error("  Azahar is GONE -- it crashed. Nothing more is being "
                  "pressed. Start Azahar, load the save and start the hunt "
                  "again.")
        ctx.request_stop("Azahar crashed")


def _names(mods) -> set:
    try:
        return mods.names()
    except (RPCError, OSError):
        return {"Battle"}                    # a dropped read: assume the worst


def _wait_battle(ctx, mods, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while not ctx.should_stop():
        if "Battle" in _names(mods):
            return True
        if time.monotonic() > end:
            return False
        ctx._stop_evt.wait(0.1)
    return False


def _run(ctx) -> None:
    log.info("Mode: Gen 7 fishing (A to cast, A on a bite, B, B on nothing)")
    game = ctx.game
    if getattr(game, "generation", None) != 7:
        log.error("  this mode is for Sun / Moon / Ultra Sun / Ultra Moon.")
        ctx.request_stop("not a Gen 7 game")
        return
    rcfg = ctx.config.get("random_encounters") or {}
    plan = WigglePlan.from_config(rcfg)
    # The Gen 6 keys (fish_cast_button: Y) must not leak in: Alola casts on A.
    fish = replace(fishing_loop.FishPlan.from_config(rcfg), cast_button="A",
                   hook_button="A", clear_button="B", clear_taps=2)
    ensure_targets_dir()
    _focus_if_needed(ctx)
    windows = foe_windows(game.offsets)
    mods = Modules(ctx.rpc)
    if "Battle" in mods.names() and not _leave_leftover_battle(
            ctx, mods, windows, plan):
        return
    seen = {p.encryption_key for _, p in scan_windows(ctx, windows, set())}
    watch = FoeWatch(ctx, windows[0][0], windows[0][1], set(), seen,
                     windows=windows)
    watch.slots = list(getattr(game.offsets, "wild_slots", ()) or ())
    log.info(f"  Setup: standing facing a fishing spot. {len(seen)} old "
             f"record(s) ignored. A shiny is "
             f"{'caught' if plan.catch else 'left on screen, all input stopped'}.")

    tally, dry = Tally(), 0
    while not ctx.should_stop():
        got = fishing_loop.cast_once(ctx, watch.check, fish)
        if got == fishing_loop.STOPPED:
            break
        if not ctx.input.target_alive():
            raise AzaharGone()
        hooked = None
        if got == fishing_loop.HOOKED:
            dry = 0
            pkm = read_pk6_at(ctx, watch.hot)
            if pkm is not None and not (pkm.ot_name or ""):
                hooked = (watch.hot, pkm)
                seen.add(pkm.encryption_key)     # never the next bite too
            if not _wait_battle(ctx, mods, _HOOK_TO_BATTLE_S):
                if ctx.should_stop():
                    break
                log.info("  the bite got away; clearing and casting again")
                fishing_loop.clear_text(ctx, fish)
                continue
        elif "Battle" not in _names(mods):
            dry += 1
            if dry == _DRY_CASTS:
                log.warning(f"  {dry} casts in a row with nothing. If a battle "
                            f"never starts, Gen 7 may write the wild only once "
                            f"the battle begins: paste the log.")
            continue
        wilds = find_wild(ctx, windows, seen, 0.0 if hooked else plan.find_timeout)
        seen.update(p.encryption_key for _, p in wilds)
        if hooked:
            wilds = [hooked] + wilds
        if not wilds:
            if not _leave_leftover_battle(ctx, mods, windows, plan,
                                          "a battle with no new wild record"):
                return
            continue
        if not settle(ctx, mods, plan, wilds, tally, "gen7 fishing", "casting"):
            return
    log.info(f"Gen 7 fishing stopped after {tally.encounters} encounter(s).")
