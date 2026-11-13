"""
Gen 7 (Ultra Sun / Ultra Moon) fishing.

    A to cast at the fishing spot
    wait for the bite, pressing NOTHING -- an early A reels the line in
    the bite ("!") -> A once; a battle should start
    no bite, or the A came too late -> B, B to clear the text, cast again
    shiny (or target)  -> catch it (on_target: stop leaves it on screen)
    anything else      -> run, and cast again

The bite signal. Gen 7 writes the wild only once the battle starts, so
the X/Y trick (watch for the wild record) sees nothing. The first fix
pressed A blind until a battle appeared, and every cast ended "You
reeled your line in too slow!": an A before the "!" pulls the line.
Screenshots synced with RAM (2026-11-12, Ultra Sun, scripts/
find_fish_bite.py) found a u32 in code.bin's .data that the game counts
up in bursts: +0x52 for the cast (+0x32, then +0x20), not one step while
the line waits, then +0x13 the frame the "!" shows. So the bite is the
counter rising past the cast's +0x52. It is a count, not a time, so it
holds at any emulator speed. Live, 3 minutes at ~830% speed: 22 of 30
bites became battles, no dead casts. Every miss logged a clean +0x13,
so the A came too late for the "!" (~0.2 s of wall time at that speed),
not at the wrong moment; a slower emulator should miss fewer.

shortcut: the address is Ultra Sun / Ultra Moon's (live-checked on Ultra
Sun only); Sun / Moon needs its own, found the same way with
find_fish_bite.py.
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

#: The counter that jumps when the "!" shows, per game.
BITE_COUNTER = {"USUM-USA-1.2": 0x006A3B10}
#: What a cast adds to it; anything past this is the bite.
CAST_RISE = 0x52
#: Seconds to wait for the "!" (at 100% speed a bite can take several
#: seconds; the counter ends the wait the moment it comes).
BITE_WAIT = 20.0
#: Seconds from the hook press for the battle to start.
HOOK_WAIT = 6.0
#: Seconds the counter must sit still before a cast. Straight after a
#: battle it still jumps (+0xde, +0xad seen live) and the cast's A is
#: swallowed, so casting then read noise as bites or waited out dead casts.
QUIET = 0.5
#: Seconds after the cast's A with the counter unmoved: the cast did not
#: register, so A again (with no line out it cannot reel anything in).
RECAST = 3.0

HOOKED, NO_BITE, MISSED, STOPPED = "hooked", "no-bite", "missed", "stopped"


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


def _count(ctx, addr):
    try:
        return ctx.rpc.read_u32(addr)
    except (RPCError, OSError):
        return None


def _battle_starts(ctx, mods, wait: float) -> bool:
    end = time.monotonic() + wait
    while not ctx.should_stop():
        if _in_battle(mods):
            return True
        if time.monotonic() > end:
            return False
        ctx._stop_evt.wait(0.05)
    return False


def _quiet(ctx, addr: int, still: float, limit: float = 10.0):
    """The counter once it has not moved for ``still`` seconds (or after
    ``limit``, whatever it reads then); None if it cannot be read."""
    last = _count(ctx, addr)
    since = start = time.monotonic()
    while not ctx.should_stop() and time.monotonic() - start < limit:
        ctx._stop_evt.wait(0.01)
        now = _count(ctx, addr)
        if now != last:
            last, since = now, time.monotonic()
        elif time.monotonic() - since >= still:
            break
    return last


def cast(ctx, mods, addr: int, bite_wait: float = BITE_WAIT,
         hook_wait: float = HOOK_WAIT) -> str:
    """A to cast, nothing until the bite counter moves, then A once."""
    if ctx.should_stop():
        return STOPPED
    before = _quiet(ctx, addr, QUIET)
    if before is None:
        return NO_BITE                   # a dropped read: clear and recast
    _tap(ctx, "A", hold_s=0.05)
    t0 = pressed = time.monotonic()
    deadline, last, steps = t0 + bite_wait, before, []
    while not ctx.should_stop():
        now = _count(ctx, addr)
        if now is not None and now != last:
            steps.append(f"{time.monotonic() - t0:.2f}s+{(now - last) & 0xFFFFFFFF:#x}")
            last = now
        if last == before and time.monotonic() - pressed > RECAST:
            log.info("    the cast did not register; A again")
            _tap(ctx, "A", hold_s=0.05)
            pressed = time.monotonic()
        if now is not None and (now - before) & 0xFFFFFFFF > CAST_RISE:
            _tap(ctx, "A", hold_s=0.05)
            if _battle_starts(ctx, mods, hook_wait):
                return HOOKED
            log.info(f"    bite counter: {' '.join(steps)}")
            return MISSED
        if time.monotonic() > deadline:
            log.info(f"    bite counter: {' '.join(steps) or 'never moved'}")
            return NO_BITE
        ctx._stop_evt.wait(0.002)
    return STOPPED


def _run(ctx) -> None:
    log.info("Mode: Gen 7 fishing (A to cast, A on the bite, B, B on "
             "nothing)")
    game = ctx.game
    if getattr(game, "generation", None) != 7:
        log.error("  this mode is for Ultra Sun / Ultra Moon.")
        ctx.request_stop("not a Gen 7 game")
        return
    addr = BITE_COUNTER.get(getattr(game, "key", ""))
    if addr is None:
        log.error("  the bite signal is only known for Ultra Sun / Ultra "
                  "Moon so far; Sun / Moon would press A blind and reel "
                  "the line in every cast.")
        ctx.request_stop("no bite signal for this game")
        return
    rcfg = ctx.config.get("random_encounters") or {}
    plan = WigglePlan.from_config(rcfg)
    # The Gen 6 keys (fish_cast_button: Y) must not leak in: Alola casts on A.
    fish = replace(fishing_loop.FishPlan.from_config(rcfg), clear_button="B",
                   clear_taps=2)
    bite_wait = float(rcfg.get("gen7_bite_wait", BITE_WAIT))
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

    tally, misses = Tally(), 0
    while not ctx.should_stop():
        got = cast(ctx, mods, addr, bite_wait)
        if got == STOPPED:
            break
        if got in (NO_BITE, MISSED):
            if got == MISSED:
                misses += 1
                log.info(f"  fishing: hooked on the bite but no battle "
                         f"({misses} so far) - B, B and casting again")
            else:
                log.info(f"  fishing: no bite in {bite_wait:.0f}s - B, B "
                         f"and casting again")
            fishing_loop.clear_text(ctx, fish)
            ctx._stop_evt.wait(fish.recast_gap)
            continue
        wilds = find_wild(ctx, windows, seen, plan.find_timeout)
        seen.update(p.encryption_key for _, p in wilds)
        if not wilds:
            if not _leave_leftover_battle(ctx, mods, windows, plan,
                                          "a battle with no new wild record"):
                return
            continue
        if not settle(ctx, mods, plan, wilds, tally, "gen7 fishing", "casting"):
            return
    log.info(f"Gen 7 fishing stopped after {tally.encounters} encounter(s); "
             f"{misses} bite(s) missed.")
