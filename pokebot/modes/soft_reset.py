"""
Soft reset mode.

For starter, legendary, and gift Pokémon. The user must save the game
in front of the Pokémon-receiving cutscene first. The bot then:

  1. Mashes A to advance dialogue and accept the gift.
  2. Reads the new party slot.
  3. Evaluates against target.
  4. If target hit: stop. Otherwise: soft reset (L+R+Start) and repeat.

Required offsets:
  - party_base (we read slot N, configurable; default 0 = first slot)
"""

from __future__ import annotations

import logging
import time

from ..games import starter_species, starters_for
from ..parser import decrypt_pkm, parse_pkm

log = logging.getLogger(__name__)


def run(ctx):
    log.info("Mode: soft_reset")
    cfg = ctx.config.get("soft_reset", {})
    slot         = int(cfg.get("read_slot", 0))     # which party slot to read
    advance_taps = int(cfg.get("advance_taps", 60)) # button mashes after reset
    advance_gap  = float(cfg.get("advance_gap", 0.3))
    post_reset   = float(cfg.get("post_reset_wait", 4.0))
    starter_name = cfg.get("starter")

    if not ctx.game.offsets.party_base:
        log.error("party_base offset is 0 -- cannot run soft_reset.")
        return

    starter_id = None
    if starter_name:
        starter_id = starter_species(ctx.game.key, str(starter_name))
        if starter_id:
            log.info(f"Hunting starter: {starter_name} (species #{starter_id})")
        else:
            known = list(starters_for(ctx.game.key).keys())
            log.warning(f"Unknown starter '{starter_name}' for {ctx.game.key}. "
                        f"Known: {known or '(none registered)'}")

    attempt = 0
    while not ctx.should_stop():
        attempt += 1
        log.info(f"Soft reset attempt #{attempt}")
        ctx.dashboard.broadcast("soft_reset_attempt", count=attempt)

        # Mash A to advance dialogue and accept gift.
        for _ in range(advance_taps):
            if ctx.should_stop():
                return
            ctx.input.tap("A", hold_s=0.05)
            time.sleep(advance_gap)

        # Read the slot we expect the new Pokémon to land in.
        addr = (ctx.game.offsets.party_base
                + slot * ctx.game.offsets.party_stride)
        try:
            raw = ctx.rpc.read(addr, 260)
            pkm = parse_pkm(decrypt_pkm(raw))
        except Exception as e:
            log.warning(f"could not read/parse slot {slot}: {e}")
            _do_reset(ctx, post_reset)
            continue

        if not pkm.checksum_valid:
            log.debug("checksum invalid; mashing more then retrying")
            for _ in range(20):
                ctx.input.tap("A", hold_s=0.05)
                time.sleep(0.2)
            try:
                raw = ctx.rpc.read(addr, 260)
                pkm = parse_pkm(decrypt_pkm(raw))
            except Exception:
                _do_reset(ctx, post_reset)
                continue

        ctx.dashboard.broadcast(
            "candidate",
            attempt=attempt,
            species=pkm.species, nickname=pkm.nickname,
            shiny=pkm.shiny, nature=pkm.nature, gender=pkm.gender,
            ivs=pkm.ivs, pid=pkm.pid,
            level=pkm.party["level"] if pkm.party else None,
            moves=pkm.moves,
        )

        # Hard gate on starter species. If the user picked a different
        # starter than expected (saved in front of wrong Pokéball), reset
        # without bothering to evaluate target criteria.
        if starter_id is not None and pkm.species != starter_id:
            log.info(f"wrong species (#{pkm.species}); resetting")
            _do_reset(ctx, post_reset)
            continue

        # If only a starter is set and there are no extra rules, every
        # correctly-species candidate counts as a hit.
        target_has_rules = bool(ctx.target and ctx.target.rules)
        is_hit = ctx.target.matches(pkm) if target_has_rules else (starter_id is not None)
        if is_hit:
            reason = ctx.target.describe(pkm) if target_has_rules \
                else f"starter #{pkm.species}"
            log.info(f"TARGET! attempt {attempt}: {reason}")
            ctx.dashboard.broadcast(
                "target_hit",
                attempt=attempt,
                reason=reason,
                species=pkm.species, shiny=pkm.shiny,
                nature=pkm.nature, ivs=pkm.ivs,
            )
            ctx.request_stop("target hit")
            return

        _do_reset(ctx, post_reset)


def _do_reset(ctx, post_wait: float):
    ctx.input.soft_reset()
    time.sleep(post_wait)
