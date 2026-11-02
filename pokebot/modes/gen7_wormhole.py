"""
Ultra Sun / Ultra Moon: shiny hunting through the Ultra Warp Ride.

Built from guides and 58 videos of the ride (2026-11-02), NOT yet run
live. What they show, and what the bot does with it:

* A ride starts only at the warp hole on the Altar of the Sunne / Moone:
  A, "Would you like to leap into the warp hole?", then a menu whose
  first entry, Return to the Ultra Wormhole, is under the cursor. So: A
  only, never a direction, until the ride's own module (LunaSolDive) is
  loaded. About 16 s of cutscene follow before the ride can be steered.
* The ride shows no distance; light-years build up at ~33-42 a second
  (5,000 takes ~130-150 s) and only the arrival text says how far it
  went. Holes start as dots at the vanishing point, grow outward, and
  pull you in when you pass close or run out of energy; guides fly the
  edges and corners to keep clear. The bot cannot see the holes, so it
  holds a corner of the Circle Pad for ``fly`` seconds, then lets go --
  the mount drifts back to the centre -- and takes whatever hole pulls
  it in.
* The shiny roll happens on entering the hole and rises with distance
  and the hole's rings (Type 3 at 5,000+: 19%; Type 1 always 1%). Each
  world has ONE Pokemon, hidden at a fixed spot at the far end of a
  fixed layout (5-8 s of running), marked by a splash, dust or shadow;
  stepping on it starts the battle. A shiny looks the same until the
  battle. Legendaries and Ultra Beasts get no boost (1/4096).
* The way back is the hole you came through, right behind where you
  land: A, "Would you like to go back to where you came from?" (Yes
  under the cursor), and you land on the altar a few steps south of its
  hole.

The bot reads, never sees: the loaded modules say ride / field / battle,
the player's position says where it is (Gen 7's Circle Pad is the only
thing that walks), and the battle's own record says shiny. A new world is
explored; its path to the Pokemon and its way back into the hole are
remembered for the rest of the session, keyed by the spot it drops you.

shortcut: blind steering -- a corner, then the centre. Holes and orbs are
not seen, so a hole cannot be picked by its rings or colour and an early
one cannot be dodged; reading them from the screen or from the ride's
memory would buy distance and better holes.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field

from ..citra_rpc import RPCError
from ..pk6_export import ensure_targets_dir
from .gen7_encounter import (_PLAYER_ROUTE, Modules, WigglePlan, _balls_left,
                             _is_hit, catch, find_wild, flee, read_xz)
from .observe import _report_encounter, foe_windows, scan_windows
from .soft_reset import _focus_if_needed

log = logging.getLogger(__name__)

#: The ride's CRO module (romfs LunaSolDive.cro).
RIDE = "LunaSolDive"
#: The 20 species of the wormhole worlds, the ones the distance boosts.
#: Any other shiny here is a legendary or an Ultra Beast: a Poke Ball
#: barely dents those, so the bot stops and leaves it to you.
WORLD_SPECIES = frozenset({
    195, 271, 419, 618, 689,    # blue: Quagsire Lombre Floatzel Stunfisk Barbaracle
    274, 326, 452, 531, 695,    # green: Nuzleaf Grumpig Drapion Audino Heliolisk
    277, 334, 469, 561, 581,    # red: Swellow Altaria Yanmega Sigilyph Swanna
    219, 308, 450, 460, 558,    # yellow: Magcargo Medicham Hippowdon Abomasnow Crustle
})
CORNERS = {
    "down_left": ("CircleDown", "CircleLeft"),
    "down_right": ("CircleDown", "CircleRight"),
    "up_left": ("CircleUp", "CircleLeft"),
    "up_right": ("CircleUp", "CircleRight"),
}
#: Which way each push moves the player before anything is learned:
#: +x east, +z south (toward the camera).
_DIRS = {"CircleUp": (0.0, -1.0), "CircleDown": (0.0, 1.0),
         "CircleLeft": (-1.0, 0.0), "CircleRight": (1.0, 0.0)}
_STILL = 0.5            # a push that moved less than this hit something
#: Every stop reason starts with this, so the watchdog leaves them all:
#: a restart would take wherever the player stands for the altar.
STOP = "wormhole: "


@dataclass(frozen=True)
class WormholePlan:
    #: Seconds in a corner before letting a hole take the ride.
    fly: float = 150.0
    corner: tuple = CORNERS["down_left"]
    #: Catch a shiny of the 20 world species; on_target: stop leaves it.
    catch: bool = True
    altar_timeout: float = 60.0     # A presses until the ride is up
    ride_timeout: float = 480.0     # the whole ride, cutscenes included
    world_timeout: float = 40.0     # until the world is up and still
    walk_timeout: float = 90.0      # finding the world's Pokemon
    return_timeout: float = 12.0    # Yes until the altar is back (~7 s)
    prompt_gap: float = 1.0         # for a question to come up
    push: float = 0.15              # one walking push
    poll: float = 0.4
    #: How close (world units, ~50 a tile) counts as on a spot.
    near: float = 30.0
    battle: WigglePlan = field(default_factory=WigglePlan)

    @classmethod
    def from_config(cls, cfg: dict | None) -> "WormholePlan":
        cfg, d = cfg or {}, cls()
        try:
            fly = float(cfg.get("fly_seconds", d.fly))
        except (TypeError, ValueError):
            fly = d.fly
        corner = CORNERS.get(str(cfg.get("corner", "down_left")).lower(),
                             d.corner)
        return cls(fly=min(max(fly, 0.0), 400.0), corner=corner,
                   catch=str(cfg.get("on_target", "catch")).lower() != "stop")


class Walker:
    """Pushes the Circle Pad and watches the player's position. Which way
    each push goes is re-learned from every push: a world's camera swings
    as you walk, so Up is not one direction there."""

    def __init__(self, ctx, route, plan: WormholePlan):
        self.ctx, self.route, self.plan = ctx, route, plan
        self.speed = 0.0                # units per second of push, learned
        self.reset()

    def reset(self) -> None:
        """A new map, a new camera: forget which way each push goes."""
        self.dirs = dict(_DIRS)
        self.learned: set = set()       # pushes seen to move this map

    def pos(self):
        try:
            return read_xz(self.ctx.rpc, self.route)
        except (RPCError, OSError):
            return None

    def push(self, button: str, hold: float | None = None):
        """One push; how far it moved the player, or None off the field."""
        hold = hold or self.plan.push
        before = self.pos()
        self.ctx.input.tap(button, hold_s=hold)
        after = self.pos()
        if before is None or after is None:
            return None
        dx, dz = after[0] - before[0], after[1] - before[1]
        d = math.hypot(dx, dz)
        if d > _STILL:
            self.dirs[button] = (dx / d, dz / d)
            self.learned.add(button)
            self.speed = d / hold if not self.speed else (
                0.7 * self.speed + 0.3 * d / hold)
        return d

    def toward(self, vec) -> str:
        """The push that last went most nearly along ``vec``."""
        return max(self.dirs, key=lambda b: self.dirs[b][0] * vec[0]
                   + self.dirs[b][1] * vec[1])

    def until_blocked(self, button: str, most: int = 12) -> bool:
        """Push one way until something stops the player (twice running)."""
        still = 0
        for _ in range(most):
            if self.ctx.should_stop():
                return False
            d = self.push(button)
            if d is None:
                return False
            still = still + 1 if d <= _STILL else 0
            if still >= 2:
                return True
        return False

    def to(self, target, done=lambda: False, clear_text: bool = False,
           most: int = 80) -> bool:
        """Walk to ``target`` (x, z), each push sized from the learned
        speed. ``clear_text`` presses A when a push goes nowhere (an item
        found after a battle leaves text up); never used beside a hole,
        where A asks to leave."""
        tried: set = set()              # pushes that went nowhere from here
        for _ in range(most):
            if self.ctx.should_stop() or done():
                return False
            p = self.pos()
            if p is None:
                return False
            vec = (target[0] - p[0], target[1] - p[1])
            dist = math.hypot(*vec)
            # the shortest push still moves ~0.03 s worth at the
            # emulator's speed, so "there" cannot be tighter than that
            if dist <= max(self.plan.near, 0.6 * 0.03 * self.speed):
                return True
            hold = (min(max(dist / self.speed, 0.03), self.plan.push)
                    if self.speed else self.plan.push)
            left = [b for b in self.dirs if b not in tried]
            if not left:
                return False
            button = max(left, key=lambda b: self.dirs[b][0] * vec[0]
                         + self.dirs[b][1] * vec[1])
            d = self.push(button, hold)
            if d is not None and d <= _STILL:
                tried.add(button)       # a wall, or a way not learned yet
                if clear_text:
                    self.ctx.input.tap("A", hold_s=0.06)
            else:
                tried.clear()
        return False


@dataclass
class World:
    """What earlier trips learned about one world, found again by the
    spot it drops you on (each colour's layout is fixed)."""
    arrival: tuple
    path: list = field(default_factory=list)    # arrival -> the encounter
    trail: list = field(default_factory=list)   # this trip's way out
    #: The push from the arrival spot that reached the hole. A button, not
    #: a direction: a blocked push shows no direction, and the camera at
    #: a given spot is the same every visit.
    hole: str | None = None
    out: tuple | None = None                    # the first step away


def _names(mods: Modules) -> set | None:
    try:
        return mods.names()
    except (RPCError, OSError):
        return None                     # one dropped read: unknown


def _thin(path: list, gap: float) -> list:
    """Points at least ``gap`` apart, the first and last always kept."""
    out = []
    for p in path:
        if not out or math.dist(p, out[-1]) >= gap:
            out.append(p)
    if path and out[-1] != path[-1]:
        out.append(path[-1])
    return out


def enter_ride(ctx, mods: Modules, plan: WormholePlan) -> bool:
    """A, and only A, until the ride's module is loaded."""
    end = time.monotonic() + plan.altar_timeout
    while not ctx.should_stop() and time.monotonic() < end:
        if RIDE in (_names(mods) or ()):
            return True
        ctx.input.tap("A", hold_s=0.06)
        ctx._stop_evt.wait(plan.poll)
    return False


def ride(ctx, mods: Modules, plan: WormholePlan) -> bool:
    """Hold the corner for ``plan.fly`` s, then let go; True once a hole
    has taken the ride and the field is back."""
    start = time.monotonic()
    held = [b for b in plan.corner if ctx.input.hold(b)]
    try:
        while not ctx.should_stop():
            now = time.monotonic() - start
            names = _names(mods)
            if names is not None and RIDE not in names and "FieldRo" in names:
                log.info(f"  a hole took the ride after {now:.0f}s")
                return True
            if held and now >= plan.fly:
                for b in held:
                    ctx.input.release(b)
                held = []
                log.info(f"  {now:.0f}s in the corner: letting a hole take it")
            if now > plan.ride_timeout:
                return False
            ctx._stop_evt.wait(plan.poll)
        return False
    finally:
        for b in held:
            ctx.input.release(b)


def arrive(ctx, walker: Walker, plan: WormholePlan):
    """A through the arrival text (the player faces away from the hole,
    at nothing) until the player stands still; that spot, or None."""
    end = time.monotonic() + plan.world_timeout
    last, still = None, 0
    while not ctx.should_stop() and time.monotonic() < end:
        ctx.input.tap("A", hold_s=0.06)
        ctx._stop_evt.wait(plan.poll)
        p = walker.pos()
        still = still + 1 if (p and last and math.dist(p, last) <= _STILL) else 0
        if still >= 3:
            return p
        last = p
    return None


def explore(ctx, mods: Modules, walker: Walker, world: World,
            plan: WormholePlan) -> bool:
    """Walk until a battle starts. A known world is walked along its
    path; a new one is explored, each push toward the least-visited
    ground and then away from the arrival (the Pokemon waits at the far
    end), walls marked as they are hit. No A here: beside the hole it
    would ask to leave. Fills ``world.path``; True on a battle."""
    def battle_up() -> bool:
        return "Battle" in (_names(mods) or ())

    if world.path:
        world.trail = list(world.path)
        for wp in _thin(world.path, 2 * plan.near)[1:]:
            walker.to(wp, done=battle_up)
            if battle_up():
                return True
        log.info("  the remembered path found no Pokemon; exploring")
    path = list(world.path) or [world.arrival]
    world.trail = path
    visits: dict = {}
    blocked: set = set()
    size = 0.0                          # one push, once one has moved

    def cell(p):
        return (round(p[0] / size), round(p[1] / size)) if size else (0, 0)

    end = time.monotonic() + plan.walk_timeout
    while not ctx.should_stop() and time.monotonic() < end:
        if battle_up():
            world.path = path
            return True
        p = walker.pos()
        if p is None:
            ctx._stop_evt.wait(plan.poll)
            continue
        here = cell(p)
        visits[here] = visits.get(here, 0) + 1

        def score(b):
            # the neighbour cell this push should reach; then a push not
            # tried yet (its way is a guess), then the farther ground
            nxt = (here[0] + round(walker.dirs[b][0]),
                   here[1] + round(walker.dirs[b][1]))
            return (-visits.get(nxt, 0), b not in walker.learned,
                    math.dist((nxt[0] * size, nxt[1] * size), world.arrival))
        options = [b for b in walker.dirs if (here, b) not in blocked]
        if not options:                 # boxed in: try every way again
            blocked = {k for k in blocked if k[0] != here}
            continue
        button = max(options, key=score)
        d = walker.push(button)
        if d is None:
            continue
        if d <= _STILL:
            blocked.add((here, button))
            continue
        if not size:                    # a cell is one push from here on
            size = max(d, 1.0)
            visits.clear()
            blocked.clear()
        world.out = world.out or walker.dirs[button]
        q = walker.pos()
        if q is not None:               # a loop in the path is cut out
            seen = next((i for i, r in enumerate(path)
                         if cell(r) == cell(q)), None)
            path = path[:seen + 1] if seen is not None else path + [q]
            world.trail = path
    if battle_up():
        world.path = path
        return True
    return False


def battle(ctx, mods: Modules, windows, seen: set, plan: WormholePlan,
           stats: dict) -> bool:
    """Read the wild; catch a shiny world species, leave any other shiny
    on screen, run from the rest. True when the hunt goes on."""
    wilds = find_wild(ctx, windows, seen, plan.battle.find_timeout)
    seen.update(p.encryption_key for _, p in wilds)
    if not wilds:
        log.warning("  battle started but no wild record turned up; running")
    hit = None
    for addr, pkm in wilds:
        stats["encounters"] += 1
        _report_encounter(ctx, pkm, addr, stats["encounters"], "wormhole")
        if _is_hit(ctx, pkm) and hit is None:
            hit = pkm
    if hit is not None:
        what = "SHINY" if hit.shiny else "TARGET"
        if not plan.catch or hit.species not in WORLD_SPECIES:
            why = ("" if hit.species in WORLD_SPECIES else
                   " (a legendary or Ultra Beast: bring better balls)")
            log.info("*" * 30)
            log.info(f"  {what} #{hit.species} on trip {stats['trips']}. Bot "
                     f"STOPPED with the battle on screen: catch it "
                     f"yourself{why}.")
            log.info("*" * 30)
            ctx.request_stop(f"{STOP}{what.lower()} on screen")
            return False
        log.info(f"  {what} #{hit.species}: catching it")
        if not catch(ctx, mods, plan.battle):
            if not ctx.should_stop():
                log.error(f"  {what} still in battle after "
                          f"{plan.battle.catch_tries} ball(s); Bot STOPPED "
                          f"with the battle on screen. Catch it yourself.")
                ctx.request_stop(f"{STOP}catch failed")
            return False
        stats["caught"] += 1
        log.info(f"  CAUGHT #{stats['caught']}: {what} #{hit.species} on trip "
                 f"{stats['trips']}")
        ctx.dashboard.broadcast("target_caught", species=hit.species,
                                shiny=bool(hit.shiny),
                                count=stats["encounters"],
                                caught=stats["caught"])
        return True
    if flee(ctx, mods, plan.battle.flee_timeout):
        return True
    if not ctx.should_stop():
        log.error("  could not run; stopping with the battle on screen.")
        ctx.request_stop(f"{STOP}flee failed")
    return False


def _back_on_altar(walker: Walker, home, before) -> bool:
    """The altar again: the position jumped, and landed near the spot the
    hunt started on (~5 tiles south of its hole)."""
    p = walker.pos()
    return (p is not None and math.dist(p, home) <= 12 * walker.plan.near
            and (before is None or math.dist(p, before) > 4 * walker.plan.near))


def go_back(ctx, walker: Walker, world: World, home,
            plan: WormholePlan) -> bool:
    """Back along the trail, then into the hole: A, and A again for Yes,
    which the cursor starts on. The hole's push is learned -- first back
    against the first step out, then the rest. True on the altar."""
    for wp in reversed(_thin(world.trail, 2 * plan.near)[1:-1]):
        walker.to(wp, clear_text=True)
    out = world.out or (0.0, 1.0)
    ways = [world.hole] if world.hole else []
    for vec in ((-out[0], -out[1]), (-out[1], out[0]), (out[1], -out[0]), out):
        ways.append(walker.toward(vec))
    ways += list(_DIRS)
    for way in dict.fromkeys(ways):     # each push once, in that order
        walker.to(world.arrival)
        before = walker.pos()
        walker.until_blocked(way, most=6)
        for _ in range(2):              # the hole, then Yes
            ctx.input.tap("A", hold_s=0.06)
            ctx._stop_evt.wait(plan.prompt_gap)
        end = time.monotonic() + plan.return_timeout
        while not ctx.should_stop() and time.monotonic() < end:
            if _back_on_altar(walker, home, before):
                world.hole = way
                return True
            ctx._stop_evt.wait(plan.poll)
        if ctx.should_stop():
            return False
        ctx.input.tap("B", hold_s=0.06)  # a prompt still open: No
        ctx._stop_evt.wait(plan.prompt_gap / 2)
    return False


def to_altar_hole(ctx, walker: Walker, home) -> bool:
    """From where the altar drops you to the spot the hunt started on,
    then Up into the hole so A reaches it."""
    walker.to(home)
    return walker.until_blocked("CircleUp")


def _status(ctx, stats: dict, phase: str) -> None:
    # Also the watchdog's sign of life: a trip can outlast its 5-min stall.
    ctx.dashboard.broadcast("status", mode="gen7_wormhole", phase=phase,
                            trips=stats["trips"],
                            encounters=stats["encounters"],
                            caught=stats["caught"])


def _world_for(worlds: list, arrival, plan: WormholePlan) -> World:
    for w in worlds:
        if math.dist(w.arrival, arrival) <= 4 * plan.near:
            return w
    worlds.append(World(arrival))
    return worlds[-1]


def run(ctx) -> None:
    log.info("Mode: Ultra Warp Ride shiny hunt (Ultra Sun / Ultra Moon)")
    game = ctx.game
    route = _PLAYER_ROUTE.get(getattr(game, "key", ""))
    if getattr(game, "generation", None) != 7 or route is None:
        log.error("  this mode is for Ultra Sun / Ultra Moon (US v1.2).")
        ctx.request_stop(f"{STOP}not Ultra Sun / Ultra Moon")
        return
    plan = WormholePlan.from_config(ctx.config.get("ultra_wormhole"))
    ensure_targets_dir()
    _focus_if_needed(ctx)
    mods = Modules(ctx.rpc)
    names = _names(mods) or set()
    walker = Walker(ctx, route, plan)
    home = walker.pos()
    if "Battle" in names or RIDE in names or home is None:
        log.error("  start on the altar, standing at its warp hole and "
                  "facing it (not in a battle or a ride).")
        ctx.request_stop(f"{STOP}not at the altar")
        return
    windows = foe_windows(game.offsets)
    seen = {p.encryption_key for _, p in scan_windows(ctx, windows, set())}
    log.info(f"  altar spot x={home[0]:.0f} z={home[1]:.0f}; each ride holds "
             f"{'+'.join(b[6:] for b in plan.corner)} for {plan.fly:.0f}s, then "
             f"takes the first hole that pulls. A shiny world Pokemon is "
             f"{'caught' if plan.catch else 'left on screen'}; a shiny "
             f"legendary / Ultra Beast is always left on screen.")
    if plan.catch:
        balls = _balls_left(ctx)
        log.info(f"  {balls} Poke Ball(s) in the bag" if balls is not None
                 else "  the bag could not be read: balls are not counted")
    stats = {"trips": 0, "encounters": 0, "caught": 0}
    worlds: list = []
    began = time.monotonic()
    while not ctx.should_stop():
        stats["trips"] += 1
        trip_at = time.monotonic()
        _status(ctx, stats, "altar")
        if not enter_ride(ctx, mods, plan):
            if not ctx.should_stop():
                log.error("  no ride after A at the altar: stand at the warp "
                          "hole, facing it.")
                ctx.request_stop(f"{STOP}ride did not start")
            break
        _status(ctx, stats, "ride")
        if not ride(ctx, mods, plan):
            if not ctx.should_stop():
                log.error(f"  still riding after {plan.ride_timeout:.0f}s.")
                ctx.request_stop(f"{STOP}ride did not end")
            break
        walker.reset()
        arrival = arrive(ctx, walker, plan)
        if arrival is None:
            if not ctx.should_stop():
                log.error("  the world never settled after the ride.")
                ctx.request_stop(f"{STOP}lost after the ride")
            break
        world = _world_for(worlds, arrival, plan)
        _status(ctx, stats, "world")
        log.info(f"  landed at x={arrival[0]:.0f} z={arrival[1]:.0f} "
                 f"({'a world seen before' if world.path else 'a new world'})")
        if explore(ctx, mods, walker, world, plan):
            if not battle(ctx, mods, windows, seen, plan, stats):
                break
        elif not ctx.should_stop():
            log.warning(f"  no Pokemon found in {plan.walk_timeout:.0f}s; "
                        f"going back without one")
        if ctx.should_stop():
            break
        if not go_back(ctx, walker, world, home, plan):
            if not ctx.should_stop():
                log.error("  could not get back through the hole to the "
                          "altar; stopping in the world.")
                ctx.request_stop(f"{STOP}could not get back")
            break
        walker.reset()
        if not to_altar_hole(ctx, walker, home):
            if not ctx.should_stop():
                log.error("  back on the altar but could not reach its hole.")
                ctx.request_stop(f"{STOP}could not reach the altar hole")
            break
        log.info(f"  trip {stats['trips']} took "
                 f"{time.monotonic() - trip_at:.0f}s; {stats['encounters']} "
                 f"encounter(s), {stats['caught']} caught in "
                 f"{(time.monotonic() - began) / 60:.0f} min")
    log.info(f"Wormhole hunt stopped after {stats['trips']} trip(s), "
             f"{stats['encounters']} encounter(s), {stats['caught']} caught.")
