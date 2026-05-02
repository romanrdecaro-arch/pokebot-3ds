"""
Soft reset mode.

For starter / legendary / gift Pokémon. The user saves the game in
the position documented in TUTORIAL.md, then the bot:

  1. Runs the game-specific input sequence to advance dialogue and
     select the chosen starter (or accept the generic gift).
  2. Reads the relevant party slot, decrypts and parses it.
  3. Evaluates against target rules (and a hard species gate when a
     starter is configured).
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


# ---------------------------------------------------------------------------
# Per-game starter input sequences
# ---------------------------------------------------------------------------

def _xy_starter_sequence(ctx, starter: str, gap: float) -> bool:
    """Pokémon X / Y starter selection.

    The player must save in Aquacorde Town one tile south of the
    Pokéball table (the position pictured in TUTORIAL.md).

    Sequence:
      1. Press DpadLeft once to face the table.
      2. Mash A through Tierno's setup dialogue until the starter
         selection menu appears.
      3. From the default cursor (Fennekin, middle):
           - Chespin:   2× DpadLeft, 2× A
           - Fennekin:  2× A
           - Froakie:   2× DpadRight, 2× A
      4. Mash A through the confirmation + receive dialogue until the
         party slot has been written.

    Returns True when complete; False if a stop was requested mid-run.
    """
    starter = (starter or "").lower()

    # Step 1 — face the table
    ctx.input.tap("DpadLeft", hold_s=0.1)
    time.sleep(0.35)

    # Step 2 — mash A until the starter selection menu opens.
    # Tierno's pre-selection dialogue is around 15-18 lines on the
    # English script; 30 A presses with a 0.4s gap is enough margin
    # to reach the menu without overshooting it.
    for _ in range(30):
        if ctx.should_stop():
            return False
        ctx.input.tap("A", hold_s=0.05)
        time.sleep(gap)

    # Step 3 — starter-specific cursor navigation.
    if starter == "chespin":
        for _ in range(2):
            if ctx.should_stop(): return False
            ctx.input.tap("DpadLeft", hold_s=0.1)
            time.sleep(0.25)
    elif starter == "froakie":
        for _ in range(2):
            if ctx.should_stop(): return False
            ctx.input.tap("DpadRight", hold_s=0.1)
            time.sleep(0.25)
    # Fennekin: cursor already on it; no movement.

    # Confirm twice (open Pokéball + "Yes, take this one").
    for _ in range(2):
        if ctx.should_stop():
            return False
        ctx.input.tap("A", hold_s=0.05)
        time.sleep(0.6)

    # Step 4 — mash A through the receive dialogue until party fills.
    # A modest fixed budget here; the main loop's checksum-retry below
    # will keep mashing if the slot still isn't written.
    for _ in range(35):
        if ctx.should_stop():
            return False
        ctx.input.tap("A", hold_s=0.05)
        time.sleep(gap)
    return True


# game key -> sequence callable. Other games fall back to generic mash-A.
_SEQUENCES = {
    "X-USA": _xy_starter_sequence,
    "Y-USA": _xy_starter_sequence,
}


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(ctx):
    log.info("Mode: soft_reset")
    cfg = ctx.config.get("soft_reset", {})
    slot         = int(cfg.get("read_slot", 0))     # which party slot to read
    advance_taps = int(cfg.get("advance_taps", 60)) # generic-mode mashes
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

    seq_fn = _SEQUENCES.get(ctx.game.key)
    if seq_fn and starter_name:
        log.info(f"Using {ctx.game.key} starter sequence for "
                 f"{starter_name}.")
    elif starter_name:
        log.info(f"No game-specific sequence registered for {ctx.game.key}; "
                 f"falling back to generic A-mash.")

    attempt = 0
    while not ctx.should_stop():
        attempt += 1
        log.info(f"Soft reset attempt #{attempt}")
        ctx.dashboard.broadcast("soft_reset_attempt", count=attempt)

        # ------------------------------------------------------------------
        # Phase 1 — drive the game from save screen to populated party slot.
        # ------------------------------------------------------------------
        if seq_fn and starter_name:
            if not seq_fn(ctx, starter_name, advance_gap):
                return
        else:
            # Fallback: just mash A until the slot probably exists.
            for _ in range(advance_taps):
                if ctx.should_stop():
                    return
                ctx.input.tap("A", hold_s=0.05)
                time.sleep(advance_gap)

        # ------------------------------------------------------------------
        # Phase 2 — read and parse the resulting party slot.
        # ------------------------------------------------------------------
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
            for _ in range(25):
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
            tsv=pkm.tsv, psv=pkm.psv,
            ability_id=pkm.ability_id, ability_num=pkm.ability_num,
            level=pkm.party["level"] if pkm.party else None,
            moves=pkm.moves,
        )

        # ------------------------------------------------------------------
        # Phase 3 — evaluate. Hard gate on starter species, then target rules.
        # ------------------------------------------------------------------
        if starter_id is not None and pkm.species != starter_id:
            log.info(f"wrong species (#{pkm.species}); resetting")
            _do_reset(ctx, post_reset)
            continue

        target_has_rules = bool(ctx.target and ctx.target.rules)
        is_hit = ctx.target.matches(pkm) if target_has_rules \
                  else (starter_id is not None)
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
