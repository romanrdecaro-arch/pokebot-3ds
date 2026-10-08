"""
Static encounter soft reset (built for Omega Ruby / Alpha Sapphire).

    save in front of the Pokémon -> mash A -> the battle starts ->
    shiny: STOP everything, battle left on screen; not shiny: reset.

The button presses are X/Y's Snorlax hunt, unchanged: A every
``a_gap`` seconds (0.4), up to ``a_max`` presses (60), the foe window
checked between them, then L+R+Start and the same post-reset taps.
Nothing in that sequence was ever specific to Snorlax -- walking up to
a legendary and pressing A is the same interaction everywhere.

**There is no catch sequence.** On a shiny the bot stops sending input
entirely and leaves the battle for you. That is deliberate: a static
legendary is often a one-shot, and the catch is worth doing by hand.

No press is ever issued at a battle that is already up: the foe window
is checked straight after every wait, and the next press only follows
a check that found nothing. So the X/Y loop needed no change to honour
"stop all actions on shiny".
"""
from __future__ import annotations

import logging

from ..games import DEFAULT_OT_NAME
from ..pk6_export import ensure_targets_dir, save_target_pk6
from ..platform_utils import focus_azahar
from .observe import (_report_encounter, broadcast_party, get_party,
                      scan_nonparty)
from .soft_reset import _do_reset

log = logging.getLogger(__name__)


def run(ctx) -> None:
    cfg = ctx.config.get("soft_reset", {}) or {}
    o = ctx.game.offsets
    foe_base = o.foe_base
    foe_len = getattr(o, "foe_scan_len", 0) or 0x20000
    party_base = o.party_base
    party_stride = o.party_stride or 484
    player_ot = cfg.get("trainer_name", DEFAULT_OT_NAME)
    # The Snorlax hunt's values, read under the same keys first so a
    # user who tuned it for X/Y gets the same behaviour here.
    post_reset = float(cfg.get("post_reset_wait", 3.5))
    post_reset_taps = int(cfg.get("post_reset_taps", 4))
    post_reset_gap = float(cfg.get("post_reset_gap", 0.6))
    a_gap = float(cfg.get("static_a_gap", cfg.get("snorlax_a_gap", 0.4)))
    a_max = int(cfg.get("static_a_max", cfg.get("snorlax_a_max", 60)))

    ensure_targets_dir()
    log.info("Mode: static encounter soft reset")
    log.info("  Setup: standing in front of the Pokémon, facing it, "
             "game SAVED there.")
    log.info(f"  A every {a_gap:.2f}s, up to {a_max} presses. On a "
             f"shiny the bot STOPS ALL INPUT — no catch, the battle "
             f"is left for you.")
    if not foe_base:
        log.error("foe_base not configured; aborting.")
        ctx.request_stop("foe_base not configured")
        return

    try:
        focus_azahar()
    except Exception:
        pass

    attempt = 0
    while not ctx.should_stop():
        attempt += 1
        log.info(f"Static encounter attempt #{attempt}")
        ctx.dashboard.broadcast("soft_reset_attempt", count=attempt)

        # The party's own records are not wilds.
        party = get_party(ctx, party_base, party_stride, player_ot)
        party_keys = broadcast_party(ctx, party)

        # Whatever is already in the foe window is left over from the
        # last attempt -- only a key not in here is this encounter.
        baseline = {p.encryption_key for _, p in
                    scan_nonparty(ctx, foe_base, foe_len, party_keys)}

        def new_wild():
            cands = scan_nonparty(ctx, foe_base, foe_len, party_keys)
            fresh = sorted(((a, p) for a, p in cands
                            if p.encryption_key not in baseline),
                           key=lambda ap: ap[0])
            return fresh[0] if fresh else None

        wild = None
        for i in range(a_max):
            if ctx.should_stop():
                return
            ctx.input.tap("A", hold_s=0.05)
            ctx._stop_evt.wait(a_gap)
            wild = new_wild()
            if wild is not None:
                log.info(f"  encounter after {i + 1} A press(es).")
                break

        if ctx.should_stop():
            return
        if wild is None:
            log.warning(f"  attempt {attempt}: no encounter after "
                        f"{a_max} A presses. Check you are standing in "
                        f"front of the Pokémon. Resetting.")
            ctx.dashboard.broadcast(
                "read_failure", attempt=attempt,
                reason="no foe after A-mash")
            _do_reset(ctx, post_reset, post_reset_taps, post_reset_gap)
            continue

        addr, pkm = wild
        _report_encounter(ctx, pkm, addr, attempt, "static-A")

        if pkm.shiny or (ctx.target and ctx.target.matches(pkm)):
            # Stop FIRST, then do the bookkeeping. Nothing below sends
            # input, but the stop is the instruction and it should not
            # wait on a file write.
            ctx.request_stop("shiny static encounter")
            save_target_pk6(ctx, addr, pkm,
                            "shiny" if pkm.shiny else "static")
            reason = "shiny" if pkm.shiny else ctx.target.describe(pkm)
            bar = "*" * 30
            for line in (
                bar,
                f"  {reason.upper()} #{pkm.species} — attempt #{attempt}",
                "  Bot STOPPED — no further input. The battle is on "
                "screen: catch it yourself.",
                bar,
            ):
                log.info(line)
            ctx.dashboard.broadcast(
                "target_hit", attempt=attempt, count=attempt,
                reason=reason, species=pkm.species, shiny=pkm.shiny,
                nature=pkm.nature, ivs=pkm.ivs)
            return

        _do_reset(ctx, post_reset, post_reset_taps, post_reset_gap)

    log.info(f"Static encounter hunt stopped after {attempt} attempt(s).")
