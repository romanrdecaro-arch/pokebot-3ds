"""
Static encounter soft reset (built for Omega Ruby / Alpha Sapphire).

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

**There is no catch sequence.** On a shiny the bot stops sending input
entirely and leaves the battle for you. A static legendary is often a
one-shot, and the catch is worth doing by hand.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from ..games import DEFAULT_OT_NAME
from ..pk6_export import ensure_targets_dir
from .foe_watch import FoeWatch
from .observe import (_report_encounter, broadcast_party, get_party,
                      pick_opponent, read_pk6_at, scan_nonparty)
from .soft_reset import (_PRE_RESET_QUIET_S, _PRESS_HOLD_FLOOR,
                         _PRESS_HOLD_S, _RELOAD_READ_GRACE_S,
                         _RESET_COOLDOWN_S, _focus_if_needed, _note_path)

log = logging.getLogger(__name__)

#: Floors under the two silences around a reset -- the same ones
#: game_reset.ResetPlan applies. Lowering them is tuning; zeroing one
#: removes the only thing between a read and a process being freed.
_MIN_QUIET_S = 0.05
_MIN_GRACE_S = 0.5


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


@dataclass(frozen=True)
class Hunt:
    """What the hunt reads. Fixed for the run, except ``seen``."""
    foe_base: int
    foe_len: int
    party_keys: set
    #: Every key already accounted for: the window as it was at the
    #: start, plus one wild per attempt. Shared with ``watch``. A reset
    #: does not clear the console's RAM, so last attempt's wild is
    #: still sitting in the slot after it -- this is what stops it
    #: counting as the next encounter.
    seen: set
    watch: FoeWatch


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


def mash_until_wild(ctx, plan: StaticPlan, watch: FoeWatch,
                    reads_from: float) -> Mash:
    """Press A until the foe window holds a wild the hunt has not seen.

    Presses never wait. Reads wait for ``reads_from`` -- the end of the
    reload grace -- and from then on the window is checked before every
    press, so no press follows a check that saw a battle.
    """
    deadline = max(time.monotonic(), reads_from) + plan.encounter_timeout
    presses = reads = 0
    while not ctx.should_stop() and time.monotonic() < deadline:
        if time.monotonic() >= reads_from:
            reads += 1
            if watch.check():
                return Mash(True, presses, time.monotonic(), reads == 1)
        _note_path(ctx, ctx.input.tap("A", hold_s=plan.press_hold))
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
             scan_nonparty(ctx, hunt.foe_base, hunt.foe_len,
                           hunt.party_keys)
             if p.encryption_key not in hunt.seen]
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


def run(ctx) -> None:
    cfg = ctx.config.get("soft_reset", {}) or {}
    plan = StaticPlan.from_config(cfg)

    ensure_targets_dir()
    # broadcast_party suppresses identical signatures, so a stale one
    # would freeze the launcher strip on the previous run's team.
    if hasattr(ctx, "_party_sig"):
        ctx._party_sig = None

    log.info("Mode: static encounter soft reset")
    log.info("  Setup: standing in front of the Pokémon, facing it, "
             "game SAVED there.")
    log.info(f"  Spamming A ({plan.press_hold * 1000:.0f} ms presses) "
             f"until the battle starts. Not shiny: L+R+Start and "
             f"straight back to spamming. SHINY: the bot STOPS ALL "
             f"INPUT — no catch, the battle is left for you.")
    hunt = _start(ctx, cfg, plan)
    if hunt is None:
        return

    attempt = 0
    reset_at = 0.0
    reads_from = 0.0              # nothing reset yet: reading is safe
    while not ctx.should_stop():
        attempt += 1
        log.info(f"Static encounter attempt #{attempt}")
        ctx.dashboard.broadcast("soft_reset_attempt", count=attempt)
        if _attempt(ctx, plan, hunt, attempt, reset_at, reads_from):
            return
        nxt = reset(ctx, plan, reset_at)
        if nxt is None:
            return
        reset_at, reads_from = nxt

    log.info(f"Static encounter hunt stopped after {attempt} attempt(s).")


def _start(ctx, cfg: dict, plan: StaticPlan) -> Hunt | None:
    """Read the team and the foe window once, before any press."""
    o = ctx.game.offsets
    if not o.foe_base:
        log.error("foe_base not configured; aborting.")
        ctx.request_stop("foe_base not configured")
        return None
    foe_len = getattr(o, "foe_scan_len", 0) or 0x20000
    _focus_if_needed(ctx)

    # Once, not every attempt. A reset reloads the same save, so the
    # team never changes -- and re-reading it cost the old loop over a
    # second of idle A per attempt.
    party = get_party(ctx, o.party_base, o.party_stride or 484,
                      cfg.get("trainer_name", DEFAULT_OT_NAME))
    party_keys = broadcast_party(ctx, party)

    # Whatever is in the foe window now predates the hunt.
    before = scan_nonparty(ctx, o.foe_base, foe_len, party_keys)
    seen = {p.encryption_key for _, p in before}
    watch = FoeWatch(ctx, o.foe_base, foe_len, party_keys, seen,
                     full_every=plan.full_every)
    # A leftover wild sits in the wild slot, so look there first.
    stale = pick_opponent(before)
    if stale is not None:
        watch.hot = stale[0]
    log.info(f"  {len(seen)} record(s) already in the foe window — "
             f"ignored.")
    return Hunt(o.foe_base, foe_len, party_keys, seen, watch)


def _attempt(ctx, plan: StaticPlan, hunt: Hunt, attempt: int,
             reset_at: float, reads_from: float) -> bool:
    """One encounter. True means the hunt is over: a hit, or stopped."""
    mash = mash_until_wild(ctx, plan, hunt.watch, reads_from)
    if ctx.should_stop():
        return True
    wild = None
    if mash.found:
        ctx._stop_evt.wait(plan.settle)          # nothing pressed here
        if ctx.should_stop():
            return True
        wild = read_wild(ctx, hunt)
    if wild is None:
        _report_miss(ctx, plan, attempt, mash)
        return False

    addr, pkm = wild
    hunt.seen.add(pkm.encryption_key)
    hunt.watch.hot = addr
    _log_timing(mash, reset_at)
    if pkm.shiny or (ctx.target and ctx.target.matches(pkm)):
        _stop_on(ctx, pkm, addr, attempt)
        return True
    _report_encounter(ctx, pkm, addr, attempt, "static")
    return False


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


def _report_miss(ctx, plan: StaticPlan, attempt: int, mash: Mash) -> None:
    if mash.found:
        log.warning(f"  attempt {attempt}: the encounter was gone before "
                    f"it could be read. Resetting.")
        reason = "encounter vanished before it was read"
    else:
        log.warning(f"  attempt {attempt}: no encounter in "
                    f"{plan.encounter_timeout:.0f}s ({mash.presses} A "
                    f"presses). Check you are standing in front of the "
                    f"Pokémon, facing it, with the game saved there. "
                    f"Resetting.")
        reason = "no encounter while pressing A"
    ctx.dashboard.broadcast("read_failure", attempt=attempt, reason=reason)


def _stop_on(ctx, pkm, addr: int, attempt: int) -> None:
    """Stop, then say so. Nothing in here sends input."""
    # The stop is the instruction, so it goes first.
    ctx.request_stop("shiny static encounter")
    # Logs it and writes its .pk6 to targets/ -- a read, not an input.
    _report_encounter(ctx, pkm, addr, attempt, "static")
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
