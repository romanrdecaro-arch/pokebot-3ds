"""
Gen 7 (Sun / Moon, Ultra Sun / Ultra Moon) random encounters, in place.

The loop, as asked for:

    quick, equal Left / Right pushes so the player wiggles in the grass
      without leaving the starting spot
    a wild appears  -> shiny (or the target filter matches): catch it
                       (Y for the quick Poke Ball, A, A, then B out of the
                       battle) and go back to wiggling; with
                       on_target: stop, STOP every input instead
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
* Equal Left / Right pushes still drift (about 2 units south a cycle,
  measured live in Ultra Sun), so on Ultra Sun / Ultra Moon an Anchor
  reads the player's position and pushes back to the starting spot.
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

#: Where the player stands, per game: (root global, slot, vtable). Found
#: for Ultra Sun / Ultra Moon from code.bin's RTTI and checked live
#: (2026-10-30): *(0x0062143C) is a field object whose +0x88 is the
#: Field::MoveModel::FieldMoveModelPlayer (vtable 0x0064A4F0; null during
#: a battle), and its +0x3B0 holds x, y, z (+x east, +z south, toward the
#: camera). Sun / Moon have their own addresses, not found yet.
_PLAYER_ROUTE = {"USUM-USA-1.2": (0x0062143C, 0x88, 0x0064A4F0)}
_POS_OFF = 0x3B0
#: How far (world units) the wiggle may wander from home before a push
#: brings it back. One 0.08 s push at ~10x emulation is ~150 units.
_ANCHOR_TOL = 30.0


@dataclass(frozen=True)
class WigglePlan:
    #: Seconds each Circle Pad push is held; Left and Right are equal so
    #: they cancel out.
    hold: float = 0.08
    #: Seconds to keep looking for the wild once a battle has started.
    find_timeout: float = 8.0
    #: Seconds a flee (or a catch) may take before the hunt stops instead.
    flee_timeout: float = 60.0
    #: Catch a shiny / target (random_encounters.on_target: catch, the
    #: default) rather than stop with it on screen (on_target: stop).
    catch: bool = True
    #: Balls thrown before the hunt stops with the battle up.
    catch_tries: int = 10
    #: B taps (dismiss_gap apart, ~3 s) before each throw, so Y lands on
    #: the command menu and not on battle text.
    settle_taps: int = 8
    #: Seconds between Y, A, A (the quick-ball menu has to open), and
    #: between the 15 B presses that clear Gotcha!, the Pokedex and the
    #: nickname prompt.
    throw_gap: float = 0.5
    dismiss_gap: float = 0.4

    @classmethod
    def from_config(cls, cfg: dict | None) -> "WigglePlan":
        cfg, d = cfg or {}, cls()
        try:
            hold = float(cfg.get("gen7_wiggle_hold", d.hold))
        except (TypeError, ValueError):
            hold = d.hold
        return cls(hold=min(max(hold, 0.02), 0.5),
                   catch=str(cfg.get("on_target", "catch")).lower() != "stop")


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


def read_xz(rpc, route) -> tuple[float, float] | None:
    """The player's (x, z), or None outside the overworld."""
    root, slot, vtable = route
    parent = rpc.read_u32(root)
    player = rpc.read_u32(parent + slot) if parent else 0
    if not player or rpc.read_u32(player) != vtable:
        return None
    x, _, z = struct.unpack("<3f", rpc.read(player + _POS_OFF, 12))
    return x, z


class Anchor:
    """Keeps the wiggle on its starting spot.

    Equal Left / Right pushes do not cancel in Gen 7: each reversal
    swings the player through facing the camera, and a measured run crept
    ~2 units south per cycle. After every pair, where the pair ended (the
    spot the player rests on between pairs) is compared with home; an
    axis off by more than ``tol`` gets one push back, sized from the push
    speed learned live (it scales with emulator speed, so it cannot be a
    constant). The pair's end, not the sway's midpoint, is the reference:
    the two pushes do not travel equally far, and live the midpoint sat
    ~250 units off while the corrections fought it."""

    def __init__(self, rpc, route, home, tol: float = _ANCHOR_TOL):
        self.rpc, self.route, self.home, self.tol = rpc, route, home, tol
        self.samples: list = []         # positions after this pair's pushes
        self.speed: dict = {}           # axis -> units per second of push

    def sample(self) -> None:
        xz = read_xz(self.rpc, self.route)
        if xz is not None:
            self.samples.append(xz)

    def _learn(self, axis: int, moved: float, held: float) -> None:
        if moved > 1e-3 and held > 0:
            v = moved / held
            old = self.speed.get(axis)
            self.speed[axis] = v if old is None else 0.7 * old + 0.3 * v

    def correct(self, ctx, hold: float) -> None:
        """Called after a Left, Right pair in the overworld."""
        samples, self.samples = self.samples, []
        if len(samples) < 2:
            return
        (xl, zl), (xr, zr) = samples[-2], samples[-1]
        self._learn(0, abs(xr - xl), hold)          # the Right push
        for axis, err, toward_minus, toward_plus in (
                (0, xr - self.home[0], "CircleRight", "CircleLeft"),
                (1, zr - self.home[1], "CircleDown", "CircleUp")):
            speed = self.speed.get(axis) or self.speed.get(0)
            if abs(err) <= self.tol or not speed:
                continue
            held = min(max(abs(err) / speed, 0.02), 0.25)
            before = read_xz(self.rpc, self.route)
            ctx.input.tap(toward_plus if err > 0 else toward_minus, hold_s=held)
            after = read_xz(self.rpc, self.route)
            if before and after:
                self._learn(axis, abs(after[axis] - before[axis]), held)


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


def catch(ctx, mods: Modules, plan: WigglePlan) -> bool:
    """Y (the quick Poke Ball), A, A to throw, then 15 B out of the
    battle. A ball that breaks hands the command menu back, so it is
    thrown again, up to ``plan.catch_tries`` balls. True once the
    overworld is back.

    Bag stays loaded through the ball panel and the throw (seen live), so
    it does not prove the menu is up. A Y lost to battle text would let
    A, A pick FIGHT and a move, so each throw waits behind a run of B
    taps, which clear text and do nothing on the menu.

    shortcut: "the battle ended" is taken as "caught"; a wild that
    teleports away (Abra) would read the same. Confirm from the party /
    PC box if that ever matters."""
    def names():
        try:
            return mods.names()
        except (RPCError, OSError):
            return {"Battle"}               # one dropped read: still in it

    throws = 0
    end = time.monotonic() + plan.flee_timeout
    while not ctx.should_stop() and time.monotonic() < end:
        now = names()
        if "Battle" not in now:
            return True
        if "Bag" not in now:                # text still running
            ctx.input.tap("B", hold_s=0.06)
            ctx._stop_evt.wait(0.2)
            continue
        if throws == plan.catch_tries:
            return False
        for _ in range(plan.settle_taps):
            ctx.input.tap("B", hold_s=0.06)
            ctx._stop_evt.wait(plan.dismiss_gap)
        if "Battle" not in names():         # a slow Gotcha! just finished
            return True
        throws += 1
        log.info(f"  throwing ball {throws} of {plan.catch_tries}")
        for button in ("Y", "A", "A"):
            ctx.input.tap(button, hold_s=0.06)
            ctx._stop_evt.wait(plan.throw_gap)
        for _ in range(15):
            ctx.input.tap("B", hold_s=0.06)
            ctx._stop_evt.wait(plan.dismiss_gap)
        end = time.monotonic() + plan.flee_timeout   # per ball, not per catch
    return False


def _make_anchor(rpc, game):
    route = _PLAYER_ROUTE.get(getattr(game, "key", ""))
    home = read_xz(rpc, route) if route else None
    if home is None:
        log.info("  no player position for this game yet: the wiggle runs "
                 "uncorrected and can drift slowly.")
        return None
    log.info(f"  holding the spot at x={home[0]:.0f} z={home[1]:.0f} "
             f"(+-{_ANCHOR_TOL:.0f})")
    return Anchor(rpc, route, home)


def _is_hit(ctx, pkm) -> bool:
    return bool(pkm.shiny or (ctx.target and ctx.target.rules
                              and ctx.target.matches(pkm)))


def _leave_leftover_battle(ctx, mods, windows, plan) -> bool:
    """A restart can land inside a battle. Run from it only when no
    un-owned record in the windows is a shiny / target: any of them could
    be this battle's wild, so then it is left on screen. True once the
    overworld is back."""
    wilds = [p for _, p in scan_windows(ctx, windows, set()) if not p.ot_name]
    if not wilds or any(_is_hit(ctx, p) for p in wilds):
        log.error("  a battle is already on screen and it may be a shiny / "
                  "target (or no wild record could be read); finish it and "
                  "start again from the grass.")
        ctx.request_stop("started in a battle")
        return False
    log.info(f"  started inside a battle; none of the {len(wilds)} wild "
             f"record(s) is a shiny / target, so running from it first")
    if flee(ctx, mods, plan.flee_timeout):
        return True
    if not ctx.should_stop():
        log.error("  could not run from it; stopping with the battle on screen.")
        ctx.request_stop("flee failed")
    return False


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
    if "Battle" in mods.names() and not _leave_leftover_battle(
            ctx, mods, windows, plan):
        return
    seen = {p.encryption_key for _, p in scan_windows(ctx, windows, set())}
    log.info(f"  wiggling {plan.hold * 1000:.0f} ms Left / Right in place; "
             f"{len(seen)} old record(s) ignored. A shiny is "
             f"{'caught' if plan.catch else 'left on screen, all input stopped'}.")
    anchor = _make_anchor(ctx.rpc, game)
    encounters = caught = 0
    while not ctx.should_stop():
        for button in ("CircleLeft", "CircleRight"):
            ctx.input.tap(button, hold_s=plan.hold)
            if anchor:
                anchor.sample()
        try:
            in_battle = "Battle" in mods.names()
        except (RPCError, OSError):
            continue
        if not in_battle:
            if anchor:
                anchor.correct(ctx, plan.hold)
            continue
        if anchor:
            anchor.samples.clear()

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
        if hit is not None and plan.catch:
            what = "SHINY" if hit.shiny else "TARGET"
            log.info(f"  {what} #{hit.species}: catching it")
            if not catch(ctx, mods, plan):
                if ctx.should_stop():
                    return
                log.error(f"  {what} still in battle after "
                          f"{plan.catch_tries} ball(s); Bot STOPPED with the "
                          f"battle on screen. Catch it yourself.")
                ctx.request_stop("catch failed")
                return
            caught += 1
            log.info(f"  CAUGHT #{caught}: {what} #{hit.species} after "
                     f"{encounters} encounter(s); wiggling again")
            ctx.dashboard.broadcast("target_caught", species=hit.species,
                                    shiny=bool(hit.shiny), count=encounters,
                                    caught=caught)
            continue
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
