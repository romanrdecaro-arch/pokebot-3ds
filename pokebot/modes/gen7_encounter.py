"""
Gen 7 (Sun / Moon, Ultra Sun / Ultra Moon) random encounters, in place.

The loop, as asked for:

    quick, equal Left / Right pushes so the player wiggles in the grass
      without leaving the starting spot
    a wild appears  -> shiny (or the target filter matches): STOP every
                       input, battle left on screen
                    -> anything else: run, and go back to wiggling

What the game does, found by driving it live (Ultra Sun, 2026-10-18):

* Only the Circle Pad walks. Pushes of 0.08 s each way wiggled the
  player in tall grass; the first wild came after six.
* The wild sits at PKMN-NTR's WildOffset4 (USUM 0x3002F9A0), inside the
  windows the game declares (observe.foe_windows), with an empty OT.
* The loaded CRO modules say where the game is: FieldRo in the
  overworld, Battle during a battle, and Bag + WazaOshie join while the
  command menu is up. The static CRS has no known pointer in Gen 7, so
  it is found once by its "CRO0" page and re-checked on every read.
* From the command menu D-pad Down selects RUN, and A runs. The D-pad
  does nothing in the overworld, so a press that lands late is harmless
  there (a touch would open the Rotom Dex).
"""
from __future__ import annotations

import logging
import struct
import time
from dataclasses import dataclass

from ..citra_rpc import RPCError
from ..pk6_export import ensure_targets_dir
from .observe import _report_encounter, foe_windows, scan_windows
from .soft_reset import _focus_if_needed

log = logging.getLogger(__name__)

#: Where RO maps modules; the static CRS page is the one with CRO0 at +0x80.
_RO_LO, _RO_HI = 0x00600000, 0x04000000
_MAX_MODULES = 64


@dataclass(frozen=True)
class WigglePlan:
    #: Seconds each Circle Pad push is held; Left and Right are equal so
    #: they cancel out.
    hold: float = 0.08
    #: Seconds to keep looking for the wild once a battle has started.
    find_timeout: float = 8.0
    #: Seconds a flee may take before the hunt stops instead.
    flee_timeout: float = 60.0

    @classmethod
    def from_config(cls, cfg: dict | None) -> "WigglePlan":
        cfg, d = cfg or {}, cls()
        try:
            hold = float(cfg.get("gen7_wiggle_hold", d.hold))
        except (TypeError, ValueError):
            hold = d.hold
        return cls(hold=min(max(hold, 0.02), 0.5))


class Modules:
    """Names of the loaded CRO modules."""

    def __init__(self, rpc):
        self.rpc = rpc
        self.crs = 0

    def _find_crs(self) -> int:
        for page in range(_RO_LO, _RO_HI, 0x1000):
            if self.rpc.read(page + 0x80, 4) == b"CRO0":
                return page
        return 0

    def names(self) -> set:
        if not self.crs or self.rpc.read(self.crs + 0x80, 4) != b"CRO0":
            self.crs = self._find_crs()     # first call, or after a reload
            if not self.crs:
                return set()
        out = set()
        m = struct.unpack("<I", self.rpc.read(self.crs + 0x88, 4))[0]
        for _ in range(_MAX_MODULES):
            if not m:
                break
            name_ptr, nxt = struct.unpack("<II", self.rpc.read(m + 0x84, 8))
            out.add(self.rpc.read(name_ptr, 32).split(b"\0")[0]
                    .decode("ascii", "replace"))
            m = nxt
        return out


def find_wild(ctx, windows, seen: set, timeout: float) -> list:
    """New un-owned PK7s, waiting for the record to be written."""
    end = time.monotonic() + timeout
    while not ctx.should_stop():
        new = [(a, p) for a, p in scan_windows(ctx, windows, set())
               if p.encryption_key not in seen and not p.ot_name]
        if new or time.monotonic() > end:
            return new
        ctx._stop_evt.wait(0.1)
    return []


def flee(ctx, mods: Modules, timeout: float) -> bool:
    """Run from the battle: B backs out of anything, Down puts the cursor
    on RUN and A takes it, each time the command menu is up. True once
    the overworld is back."""
    end = time.monotonic() + timeout
    while not ctx.should_stop() and time.monotonic() < end:
        try:
            names = mods.names()
        except (RPCError, OSError):
            names = {"Battle"}              # one dropped read: still in it
        if "Battle" not in names:
            return True
        if "Bag" in names:                  # the command menu is up
            for button in ("B", "DpadDown", "A"):
                ctx.input.tap(button, hold_s=0.06)
                ctx._stop_evt.wait(0.15)
            ctx._stop_evt.wait(0.6)
        else:
            ctx.input.tap("B", hold_s=0.06)  # move the text along
            ctx._stop_evt.wait(0.2)
    return False


def _is_hit(ctx, pkm) -> bool:
    return bool(pkm.shiny or (ctx.target and ctx.target.rules
                              and ctx.target.matches(pkm)))


def run(ctx) -> None:
    log.info("Mode: Gen 7 random encounters (wiggle in place)")
    game = ctx.game
    if getattr(game, "generation", None) != 7:
        log.error("  this mode is for Sun / Moon / Ultra Sun / Ultra Moon.")
        ctx.request_stop("not a Gen 7 game")
        return
    plan = WigglePlan.from_config(ctx.config.get("random_encounters"))
    ensure_targets_dir()
    _focus_if_needed(ctx)
    windows = foe_windows(game.offsets)
    mods = Modules(ctx.rpc)
    if "Battle" in mods.names():
        log.error("  a battle is already on screen; finish it and start "
                  "again from the grass.")
        ctx.request_stop("started in a battle")
        return
    seen = {p.encryption_key for _, p in scan_windows(ctx, windows, set())}
    log.info(f"  wiggling {plan.hold * 1000:.0f} ms Left / Right in place; "
             f"{len(seen)} old record(s) ignored. A shiny stops every input.")
    encounters = 0
    while not ctx.should_stop():
        for button in ("CircleLeft", "CircleRight"):
            ctx.input.tap(button, hold_s=plan.hold)
        try:
            in_battle = "Battle" in mods.names()
        except (RPCError, OSError):
            continue
        if not in_battle:
            continue

        wilds = find_wild(ctx, windows, seen, plan.find_timeout)
        seen.update(p.encryption_key for _, p in wilds)
        if not wilds:
            log.warning("  battle started but no wild record turned up; "
                        "running anyway")
        hit = None
        for addr, pkm in wilds:
            encounters += 1
            _report_encounter(ctx, pkm, addr, encounters, "gen7 wiggle")
            if _is_hit(ctx, pkm) and hit is None:
                hit = pkm
        if hit is not None:
            ctx.request_stop("shiny" if hit.shiny else "target")
            bar = "*" * 30
            for line in (bar,
                         f"  {'SHINY' if hit.shiny else 'TARGET'} #{hit.species} "
                         f"after {encounters} encounter(s)",
                         "  Bot STOPPED: no further input. The battle is on "
                         "screen; catch it yourself.",
                         bar):
                log.info(line)
            return
        fled_at = time.monotonic()
        if not flee(ctx, mods, plan.flee_timeout):
            if ctx.should_stop():
                return
            log.error(f"  could not run within {plan.flee_timeout:.0f}s; "
                      f"stopping with the battle on screen.")
            ctx.request_stop("flee failed")
            return
        log.info(f"  ran from it in {time.monotonic() - fled_at:.1f}s; "
                 f"wiggling again")
    log.info(f"Gen 7 hunt stopped after {encounters} encounter(s).")
