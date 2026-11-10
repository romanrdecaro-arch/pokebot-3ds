"""
Static encounter soft reset (built for Omega Ruby / Alpha Sapphire;
offered for Ultra Sun / Ultra Moon too, whose wild sits in the same
kind of foe window and whose L+R+Start the starter hunt already uses).

The loop, as asked for:

    spam A until an encounter is detected
    shiny      -> stop. Every input, for good.
    not shiny  -> soft reset, and immediately spam A again
    repeat

"Immediately" is literal. The first version borrowed X/Y's Snorlax
timings -- A every 0.4 s with a full foe-window scan between presses,
and after each reset a 12 s wait then six taps a second apart
(config.yaml's post_reset_wait / post_reset_taps) -- and a live Omega
Ruby run sat through 19 s of that before its second attempt. The boot
logos, the title, CONTINUE and the walk-up are all just A, so here the
reset and the next attempt are one continuous stream of presses, the
way the starter hunt does it.

**What waits is the reading, never the pressing.** For
``reload_read_grace`` seconds after L+R+Start nothing is read --
reading during Azahar's relaunch is what crashed it (see
soft_reset._RELOAD_READ_GRACE_S) -- so those presses are blind. That is
safe because the boot outlasts the grace: across 11,026 X/Y Noibat
resets at Azahar's 995% speed limit, a battle was up before the first
post-reset read once. If one ever is here, the log says so, because the
blind presses may then have reached it.

Once reading is allowed, the foe window is checked before EVERY press,
so a press only ever follows a check that found nothing. That matters:
in a battle A is "Fight", and the next A is "use move 1" -- on the
shiny. Checking that often is affordable because a check is one
232-byte read where the last wild appeared (foe_watch.FoeWatch), with a
full sweep every ``full_every`` checks in case the slot moved.

Gen 7 also has the game's own word for it: while the Battle module is
loaded -- or the module list cannot be read -- nothing is pressed until
that battle's wild has been read, however far from the known slots it
was written. A wild that cannot be read stops the hunt with the battle
on screen; it is never reset over, since it may be the shiny.

**There is no catch sequence.** On a shiny the bot stops sending input
entirely and leaves the battle for you. A static legendary is often a
one-shot, and the catch is worth doing by hand.

**Every window the game declares is watched.** Gen 7 writes its wild to
one of two (USUM: 0x3254F4AC in the main window, 0x3002F9A0 / 0x30039888
in the extra one, about one wild in six); watching only the first would
miss those and keep pressing A -- FIGHT, then a move -- in the battle.

The same loop runs named hunts (``Profile``): the Wimpod hunt is this
one with nothing to face -- the Wimpod runs into the player -- so its
presses stop at the field.

Azahar crashing in its relaunch (about 1 in 650-900 resets) is survived
the way the starter hunt does it: relaunched, or the hunt stops -- never
pressing into nothing. Three attempts in a row with no encounter stop
it too: that is the save, not bad luck.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace

from ..azahar_recover import AzaharGone, Recovery
from ..games import DEFAULT_OT_NAME
from ..citra_rpc import RPCError
from ..pk6_export import ensure_targets_dir
from .foe_watch import FoeWatch
from .gen7_encounter import Modules
from .observe import (_report_encounter, broadcast_party, foe_windows,
                      get_party, pick_opponent, read_pk6_at, scan_windows)
from .soft_reset import (_PRE_RESET_QUIET_S, _PRESS_HOLD_FLOOR,
                         _PRESS_HOLD_S, _RELOAD_READ_GRACE_S,
                         _RESET_COOLDOWN_S, _focus_if_needed, _note_path)

log = logging.getLogger(__name__)

#: Floors under the two silences around a reset -- the same ones
#: game_reset.ResetPlan applies. Lowering them is tuning; zeroing one
#: removes the only thing between a read and a process being freed.
_MIN_QUIET_S = 0.05
_MIN_GRACE_S = 0.5
#: How long a battle (or a spotted wild) may go unread, with nothing
#: pressed, before the hunt stops on it: gen7_encounter's find_timeout,
#: the wait for a wild record that 25,000 live USUM battles met.
_UNREAD_S = 8.0
#: Attempts in a row with no encounter before the hunt stops.
_MAX_MISSES = 3


@dataclass(frozen=True)
class StaticPlan:
    """How hard to press, how long to wait, how quiet to be."""
    #: Each A is key-down, sleep(hold), key-up, so the hold IS the
    #: rate: ~30 presses/s with the per-press check. The starter hunt's
    #: number, long enough to span a 3DS frame even at 100% speed.
    #:
    #: Its own key: config.yaml sets the shared press_hold for the
    #: starter hunt, and a default can never beat a key that is set.
    press_hold: float = _PRESS_HOLD_S
    #: Reset again after this long reading without an encounter.
    #: Bounds a stuck attempt; at 100% speed the boot alone is about
    #: 20 s, so this is generous on purpose.
    encounter_timeout: float = 45.0
    #: Between spotting the wild and reading it for the verdict, so the
    #: record judged is the finished one. Nothing is pressed in it.
    settle: float = 0.25
    #: Cheap checks between full sweeps of the foe window. A sweep is
    #: ~128 RPC round trips -- about half a second with A idle -- so
    #: this trades press rate against how long a wild that turned up
    #: somewhere new can go unseen.
    full_every: int = 20
    #: Shared with every reset hunt: these were paid for in crashes.
    pre_reset_quiet: float = _PRE_RESET_QUIET_S
    reload_grace: float = _RELOAD_READ_GRACE_S
    reset_cooldown: float = _RESET_COOLDOWN_S
    #: A Circle Pad push put between the A presses once the player is
    #: back on the field, for a battle that starts on a step, not on A:
    #: the Ultra Space Wilds legendaries and Ultra Beasts (mode
    #: static_step only). "" for none.
    step: str = ""

    @classmethod
    def from_config(cls, cfg: dict | None) -> "StaticPlan":
        cfg = cfg or {}
        d = cls()

        def num(key: str, default: float) -> float:
            try:
                return float(cfg.get(key, default))
            except (TypeError, ValueError):
                log.warning(f"  {key}={cfg.get(key)!r} is not a number; "
                            f"using {default}")
                return default

        return cls(
            press_hold=max(_PRESS_HOLD_FLOOR,
                           num("static_press_hold", d.press_hold)),
            encounter_timeout=max(1.0, num("static_timeout",
                                           d.encounter_timeout)),
            settle=max(0.0, num("static_settle", d.settle)),
            full_every=max(1, int(num("static_full_every", d.full_every))),
            pre_reset_quiet=max(_MIN_QUIET_S,
                                num("pre_reset_quiet", d.pre_reset_quiet)),
            reload_grace=max(_MIN_GRACE_S,
                             num("reload_read_grace", d.reload_grace)),
            reset_cooldown=max(0.0, num("reset_cooldown",
                                        d.reset_cooldown)),
        )


_STEPS = {"up": "CircleUp", "down": "CircleDown", "left": "CircleLeft",
          "right": "CircleRight"}


@dataclass(frozen=True)
class Profile:
    """What a named hunt on this loop changes: what it is called, the
    setup it asks for, the one species it expects (0: any) and whether
    the encounter needs any input once the save has loaded."""
    title: str = "static encounter soft reset"
    setup: str = ("standing in front of the Pokémon, facing it, game "
                  "SAVED there.")
    via: str = "static"
    species: int = 0
    #: The encounter comes to the player, so A is only for the title and
    #: Continue: nothing is pressed while reads are paused after a reset
    #: (the title waits for input), nor once the field is up, where A
    #: can only meet the scenery -- facing the water it rides off.
    hands_off: bool = False
    #: Stop ONLY for this species (0: any). A shiny of anything else is
    #: reset over -- its .pk7 is already in targets/ (_report_encounter)
    #: -- the way the X/Y Noibat hunt ignores the cave's other shinies.
    only: int = 0


STATIC = Profile()
#: Ultra Sun / Ultra Moon's Wimpod: one saved running at the player runs
#: into them about half a second after the field fades in; an idle one
#: ignores a player who stands still. A miss never comes late.
WIMPOD = Profile(
    title="Wimpod soft reset",
    setup=("SAVED in a Wimpod's path while it is already running at you "
           "(an idle one ignores a player who stands still). The bot never "
           "moves the player: A only through the title and Continue, "
           "nothing on the field."),
    via="wimpod", species=767, hands_off=True)
#: Ultra Sun / Ultra Moon's Sandygast (#769) on Hano Beach: the Ultra Space
#: legendary hunt (a step onto the encounter, mode static_step) for the
#: beach's sand, where Staryu (#120) turns up too and is always reset over.
SANDYGAST = Profile(
    title="Sandygast soft reset",
    setup=("SAVED on Hano Beach at the edge of the sand where Sandygast "
           "turns up, facing into it (soft_reset.static_step, default up)."),
    via="sandygast", only=769)


@dataclass(frozen=True)
class Hunt:
    """What the hunt reads. Fixed for the run, except ``seen``."""
    #: Every (base, length) the game can write its wild to.
    windows: list
    party_keys: set
    #: Every key already accounted for: the window as it was at the
    #: start, plus one wild per attempt. Shared with ``watch``. A reset
    #: does not clear the console's RAM, so last attempt's wild is
    #: still sitting in the slot after it -- this is what stops it
    #: counting as the next encounter.
    seen: set
    watch: FoeWatch
    #: Gen 7's loaded-module list: Battle there means a battle is up,
    #: FieldRo that the player stands on the field. None before Gen 7.
    modules: object = None


@dataclass(frozen=True)
class Mash:
    """How one stretch of A presses ended."""
    found: bool
    presses: int
    #: When the wild was spotted (time.monotonic), or 0.
    at: float = 0.0
    #: Spotted by the attempt's very first read. After a reset that
    #: means the battle came up while reading was paused.
    at_first_read: bool = False
    #: A battle was up, and its wild could not be read in _UNREAD_S.
    unread: bool = False


def _names(mods) -> set | None:
    """Gen 7's loaded modules: empty before Gen 7, None if unreadable."""
    if mods is None:
        return set()
    try:
        return mods.names()
    except (RPCError, OSError):
        return None


def mash_until_wild(ctx, plan: StaticPlan, watch: FoeWatch,
                    reads_from: float, mods=None,
                    hands_off: bool = False) -> Mash:
    """Press A until the foe window holds a wild the hunt has not seen.

    Presses never wait for reads. Reads wait for ``reads_from`` -- the
    end of the reload grace -- and from then on the window is checked
    before every press, so no press follows a check that saw a battle.

    ``mods`` (Gen 7): no press while the Battle module is loaded, or the
    list cannot be read. Every window is swept instead until the wild
    turns up; after _UNREAD_S without one the Mash says ``unread``.

    With ``plan.step``, every other press becomes that push while FieldRo
    says the player stands on the field -- never before: on the title and
    Continue screens a push would move the cursor off Continue. (The
    player's position cannot say it: the player object exists from
    boot.) ``hands_off`` presses nothing during the grace or on the field.

    No Azahar window to press into -- or to read from -- raises
    AzaharGone.
    """
    deadline = max(time.monotonic(), reads_from) + plan.encounter_timeout
    presses = reads = 0
    on_field = False
    held_at = 0.0               # when the battle not yet read was first seen
    while not ctx.should_stop():
        now = time.monotonic()
        if now >= reads_from:
            reads += 1
            names = _names(mods)
            if names is None and not ctx.input.target_alive():
                raise AzaharGone()       # unreadable because it is gone
            held = names is None or "Battle" in names
            if watch.check(full=held):
                return Mash(True, presses, time.monotonic(), reads == 1)
            if held:
                held_at = held_at or now
                if now - held_at > _UNREAD_S:
                    return Mash(False, presses, unread=True)
                ctx._stop_evt.wait(plan.press_hold)
                continue
            held_at = 0.0
            on_field = "FieldRo" in names
        if now >= deadline:
            break
        if hands_off and (now < reads_from or on_field):
            ctx._stop_evt.wait(plan.press_hold)
            continue
        button = plan.step if plan.step and on_field and presses % 2 else "A"
        path = ctx.input.tap(button, hold_s=plan.press_hold)
        _note_path(ctx, path)
        if path == "none":
            # No window to press into: Azahar crashed. Pressing on would
            # only burn the timeout, then reset into nothing, forever.
            raise AzaharGone()
        presses += 1
    return Mash(False, presses)


def read_wild(ctx, hunt: Hunt):
    """The wild just spotted, as (addr, pkm), or None if it is gone.

    One read where the check found it; a full sweep only when that is
    not a fresh, unowned record. A wild has no OT until it is caught.
    """
    hot = hunt.watch.hot
    if hot:
        pkm = read_pk6_at(ctx, hot)
        if (pkm is not None and not (pkm.ot_name or "")
                and pkm.encryption_key not in hunt.seen
                and pkm.encryption_key not in hunt.party_keys):
            return hot, pkm
    fresh = [(a, p) for a, p in
             scan_windows(ctx, hunt.windows, hunt.party_keys)
             if p.encryption_key not in hunt.seen and not (p.ot_name or "")]
    return pick_opponent(fresh)


def reset(ctx, plan: StaticPlan, last_reset_at: float):
    """Quiet, L+R+Start, and straight back to pressing.

    Returns ``(reset_at, reads_from)`` -- when the combo went out and
    when reading is safe again -- or None if the bot was stopped. There
    is deliberately no wait for the boot in here: getting through it is
    the next attempt's A presses.
    """
    if plan.reset_cooldown and last_reset_at:
        left = plan.reset_cooldown - (time.monotonic() - last_reset_at)
        if left > 0:
            log.info(f"  waiting {left:.1f}s before the next reset "
                     f"(reset_cooldown)")
            ctx._stop_evt.wait(left)
    # Let the last reads drain. Azahar services RPC on its own thread,
    # and one still in flight when the teardown starts is the crash.
    ctx._stop_evt.wait(plan.pre_reset_quiet)
    if ctx.should_stop():
        return None
    ctx.input.soft_reset()
    reset_at = time.monotonic()
    _focus_if_needed(ctx)
    return reset_at, time.monotonic() + plan.reload_grace


def run_step(ctx) -> None:
    """The same hunt for a Pokemon whose battle starts on a step toward
    it: the Ultra Space Wilds legendaries and Ultra Beasts. Save one step
    short of it, facing it; soft_reset.static_step picks the push
    (default up)."""
    cfg = ctx.config.get("soft_reset", {}) or {}
    run(ctx, step=step_from(cfg))


def run_wimpod(ctx) -> None:
    """Ultra Sun / Ultra Moon: stand still and let a Wimpod come. Not
    shiny: soft reset and spam A until the next one. Shiny (any species):
    stop all input with the battle on screen."""
    run(ctx, profile=WIMPOD)


def run_sandygast(ctx) -> None:
    """Ultra Sun / Ultra Moon, Hano Beach: step into the sand until a
    battle; a shiny Sandygast stops all input, anything else (a shiny
    Staryu included) is soft reset."""
    cfg = ctx.config.get("soft_reset", {}) or {}
    run(ctx, step=step_from(cfg), profile=SANDYGAST)


def step_from(cfg: dict) -> str:
    """soft_reset.static_step as a Circle Pad button; up when unset."""
    return _STEPS.get(str(cfg.get("static_step") or "up").lower(), "CircleUp")


def run(ctx, step: str = "", profile: Profile = STATIC) -> None:
    cfg = ctx.config.get("soft_reset", {}) or {}
    plan = StaticPlan.from_config(cfg)
    if step:
        plan = replace(plan, step=step)

    ensure_targets_dir()
    # broadcast_party suppresses identical signatures, so a stale one
    # would freeze the launcher strip on the previous run's team.
    if hasattr(ctx, "_party_sig"):
        ctx._party_sig = None

    log.info(f"Mode: {profile.title}")
    log.info(f"  Setup: {profile.setup}")
    log.info(f"  Spamming A ({plan.press_hold * 1000:.0f} ms presses)"
             f"{' through the title and Continue' if profile.hands_off else ''}"
             f" until the battle starts. Not shiny: L+R+Start and "
             f"straight back to spamming. SHINY: the bot STOPS ALL "
             f"INPUT — no catch, the battle is left for you.")
    hunt = _start(ctx, cfg, plan)
    if hunt is None:
        return
    if (plan.step or profile.hands_off) and hunt.modules is None:
        log.error("  this hunt tells the field from the title by Gen 7's "
                  "module list (Sun / Moon / Ultra Sun / Ultra Moon).")
        ctx.request_stop("not a Gen 7 game")
        return
    if plan.step:
        log.info(f"  Once the field is back after a reset, every other "
                 f"press is {plan.step[6:]} on the Circle Pad: a step onto "
                 f"the encounter.")
    # Worked out now, while Azahar is alive to say where it lives.
    recovery = Recovery.for_hunt(ctx)

    attempt = misses = 0
    reset_at = 0.0
    reads_from = 0.0              # nothing reset yet: reading is safe
    while not ctx.should_stop():
        attempt += 1
        log.info(f"{profile.via.capitalize()} attempt #{attempt}")
        ctx.dashboard.broadcast("soft_reset_attempt", count=attempt)
        try:
            outcome = _attempt(ctx, plan, hunt, attempt, reset_at,
                               reads_from, profile)
        except AzaharGone:
            from .usum_starters import _recover   # it imports this module
            nxt = _recover(ctx, plan, recovery, attempt)
            if nxt is None:
                return
            # The fresh boot IS the reset: no L+R+Start on top of it.
            reset_at, reads_from = nxt
            continue
        if outcome == "over":
            return
        if outcome != "miss":
            misses = 0
        else:
            misses += 1
            if recovery is not None and recovery.dismiss_dialogs():
                misses = 0               # it was the dialog, not the save
            elif misses >= _MAX_MISSES:
                log.error(f"  no encounter in {misses} attempts in a row. "
                          f"Check the save: {profile.setup} Stopping.")
                ctx.request_stop("no encounter")
                return
        nxt = reset(ctx, plan, reset_at)
        if nxt is None:
            return
        reset_at, reads_from = nxt

    log.info(f"{profile.title.capitalize()} stopped after {attempt} "
             f"attempt(s).")


def _start(ctx, cfg: dict, plan: StaticPlan) -> Hunt | None:
    """Read the team and the foe window once, before any press."""
    o = ctx.game.offsets
    if not o.foe_base:
        log.error("foe_base not configured; aborting.")
        ctx.request_stop("foe_base not configured")
        return None
    windows = foe_windows(o)
    mods = Modules(ctx.rpc) if getattr(ctx.game, "generation", None) == 7 \
        else None
    names = _names(mods)
    if names is None or "Battle" in names:
        # Its wild would go into ``seen`` unjudged, and A into its menu.
        log.error("  a battle is on screen (or the game's module list "
                  "could not be read). The bot never presses into a "
                  "battle it has not read: finish or reset it yourself, "
                  "then start again from the field.")
        ctx.request_stop("started in a battle")
        return None
    _focus_if_needed(ctx)

    # Once, not every attempt. A reset reloads the same save, so the
    # team never changes -- and re-reading it cost the old loop over a
    # second of idle A per attempt.
    party = get_party(ctx, o.party_base, o.party_stride or 484,
                      cfg.get("trainer_name", DEFAULT_OT_NAME))
    party_keys = broadcast_party(ctx, party)

    # Whatever is in the foe windows now predates the hunt.
    before = scan_windows(ctx, windows, party_keys)
    seen = {p.encryption_key for _, p in before}
    watch = FoeWatch(ctx, windows[0][0], windows[0][1], party_keys, seen,
                     full_every=plan.full_every, windows=windows)
    # A leftover wild sits in the wild slot, so look there first -- and
    # at every slot the game is known to write its wild to.
    stale = pick_opponent(before)
    if stale is not None:
        watch.hot = stale[0]
    watch.slots = list(getattr(o, "wild_slots", ()) or ())
    log.info(f"  {len(seen)} record(s) already in {len(windows)} foe "
             f"window(s) — ignored.")
    return Hunt(windows, party_keys, seen, watch, mods)


def _attempt(ctx, plan: StaticPlan, hunt: Hunt, attempt: int,
             reset_at: float, reads_from: float,
             profile: Profile = STATIC) -> str:
    """One encounter: "over" (a hit, or stopped), "miss" (no battle in
    the timeout) or "next" (judged, not wanted: reset)."""
    mash = mash_until_wild(ctx, plan, hunt.watch, reads_from, hunt.modules,
                           profile.hands_off)
    if ctx.should_stop():
        return "over"
    if not (mash.found or mash.unread):
        _report_miss(ctx, plan, attempt, mash, profile)
        return "miss"
    wild = None
    if mash.found:
        ctx._stop_evt.wait(plan.settle)          # nothing pressed here
        end = time.monotonic() + _UNREAD_S
        while not ctx.should_stop():
            wild = read_wild(ctx, hunt)
            if wild is not None or time.monotonic() > end:
                break
            ctx._stop_evt.wait(0.2)              # nor here
        if ctx.should_stop():
            return "over"
    if wild is None:
        log.error(f"  attempt {attempt}: a battle is up (or the game stopped "
                  f"answering reads) and its wild could not be read. It may "
                  f"be the shiny, so it is NOT reset over: Bot STOPPED with "
                  f"it on screen. Check it yourself.")
        ctx.dashboard.broadcast("read_failure", attempt=attempt,
                                reason="wild not read")
        ctx.request_stop("wild not read")
        return "over"

    addr, pkm = wild
    hunt.seen.add(pkm.encryption_key)
    hunt.watch.hot = addr
    _log_timing(mash, reset_at)
    hit = pkm.shiny or (ctx.target and ctx.target.matches(pkm))
    if hit and profile.only and pkm.species != profile.only:
        _report_encounter(ctx, pkm, addr, attempt, profile.via)  # saves it
        log.warning(f"  SHINY #{pkm.species} -- but this hunt only wants "
                    f"#{profile.only}, so it is being RESET OVER (its .pk7 "
                    f"is in targets/).")
        ctx.dashboard.broadcast("read_failure", attempt=attempt,
                                reason=f"shiny #{pkm.species}, not "
                                       f"#{profile.only}: reset over")
        return "next"
    if hit:
        _stop_on(ctx, pkm, addr, attempt, profile)
        return "over"
    _report_encounter(ctx, pkm, addr, attempt, profile.via)
    if profile.species and pkm.species != profile.species:
        log.warning(f"  expected #{profile.species}, got #{pkm.species}. "
                    f"Check the save: {profile.setup}")
    return "next"


def _log_timing(mash: Mash, reset_at: float) -> None:
    if not reset_at:
        log.info(f"  encounter after {mash.presses} A press(es)")
        return
    log.info(f"  encounter {mash.at - reset_at:.1f}s after the reset, "
             f"{mash.presses} A press(es)")
    if mash.at_first_read:
        log.warning("  the battle was ALREADY UP at the first read after "
                    "the reset. Reading pauses for reload_read_grace "
                    "after L+R+Start, so the A presses before it were "
                    "blind and may have reached the battle menu. If this "
                    "keeps happening the game is booting faster than the "
                    "grace: lower Azahar's speed limit.")


def _report_miss(ctx, plan: StaticPlan, attempt: int, mash: Mash,
                 profile: Profile = STATIC) -> None:
    log.warning(f"  attempt {attempt}: no encounter in "
                f"{plan.encounter_timeout:.0f}s ({mash.presses} A "
                f"presses). Check the save: {profile.setup}")
    ctx.dashboard.broadcast("read_failure", attempt=attempt,
                            reason="no encounter while pressing A")


def _stop_on(ctx, pkm, addr: int, attempt: int,
             profile: Profile = STATIC) -> None:
    """Stop, then say so. Nothing in here sends input."""
    # The stop is the instruction, so it goes first.
    ctx.request_stop(f"shiny {profile.via} encounter")
    # Logs it and writes its .pk6 to targets/ -- a read, not an input.
    _report_encounter(ctx, pkm, addr, attempt, profile.via)
    filter_hit = bool(ctx.target and ctx.target.matches(pkm))
    reason = ctx.target.describe(pkm) if filter_hit else "shiny"
    bar = "*" * 30
    for line in (
        bar,
        f"  {reason.upper()} #{pkm.species} — attempt #{attempt}",
        "  Bot STOPPED — no further input. The battle is on screen: "
        "catch it yourself.",
        bar,
    ):
        log.info(line)
    if not filter_hit:
        # _report_encounter already announced a filter match.
        ctx.dashboard.broadcast(
            "target_hit", attempt=attempt, count=attempt, reason=reason,
            species=pkm.species, shiny=pkm.shiny, nature=pkm.nature,
            ivs=pkm.ivs)
