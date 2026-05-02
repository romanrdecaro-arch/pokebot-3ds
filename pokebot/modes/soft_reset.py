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
from pathlib import Path

from ..games import starter_species, starters_for
from ..parser import decrypt_pkm, parse_pkm

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# First-run offset auto-discovery
# ---------------------------------------------------------------------------
# Starter hunts begin with an empty party, so the user can't run the offset
# finder up-front (no party data to find). We solve the chicken-and-egg by
# running a memory scan after the FIRST starter pickup, when the party has
# been written exactly once. The scan locates party_base, persists it to
# config.yaml, and applies it to ctx.game.offsets so every subsequent reset
# just reads from the known address.

def _discover_offsets_inline(ctx) -> bool:
    """Scan memory for the freshly-populated party block.

    Returns True if party_base was found, applied, and saved. Logs and
    returns False on failure (caller should reset and try again).
    """
    # Imported lazily so importing soft_reset doesn't drag in find_offsets.
    from .. import find_offsets as fo

    log.info("Auto-discovering party_base — first-run scan, ~30-90s. "
             "This only happens once per Azahar session.")
    try:
        hits = list(fo.scan(ctx.rpc))
    except Exception as e:
        log.warning(f"Memory scan failed: {e}")
        return False
    clusters = fo.cluster_hits(hits)
    discovered = fo.derive_offsets_from_clusters(clusters)
    if "party_base" not in discovered:
        log.warning("Scan finished but couldn't isolate the party block. "
                    "Make sure the bot's input sequence actually placed a "
                    "Pokémon in slot 0 — try saving in front of the table "
                    "and re-running.")
        return False

    for k, v in discovered.items():
        if hasattr(ctx.game.offsets, k):
            setattr(ctx.game.offsets, k, v)
    log.info("Discovered: " + ", ".join(
        f"{k}={v:#010x}" for k, v in discovered.items()))

    # Persist so the offset survives across launcher restarts.
    cfg_path = Path(__file__).resolve().parent.parent.parent / "config.yaml"
    if cfg_path.exists():
        try:
            written = fo.write_offsets_to_config(cfg_path, discovered)
            if written:
                log.info(f"Saved offsets to {cfg_path.name}: {written}")
        except Exception as e:
            log.warning(f"Could not write to {cfg_path}: {e}")
    return True


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

    # No early-exit on missing offsets — starter hunts begin with an empty
    # party, so we discover party_base AFTER the first pickup. Set a flag
    # so the loop below knows to scan once.
    needs_discovery = not ctx.game.offsets.party_base
    if needs_discovery:
        log.info("party_base not configured — will auto-discover after the "
                 "first starter is in the party.")

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
        # Phase 1b (one-time) — discover party_base after the very first
        # starter pickup, when the party block has just been written.
        # ------------------------------------------------------------------
        if needs_discovery:
            ctx.dashboard.broadcast("offset_scan",
                                    state="started", attempt=attempt)
            ok = _discover_offsets_inline(ctx)
            ctx.dashboard.broadcast("offset_scan",
                                    state=("ok" if ok else "fail"),
                                    party_base=ctx.game.offsets.party_base)
            if not ok:
                # Couldn't find a party block. Reset and try again — usually
                # the input sequence didn't actually receive the starter.
                _do_reset(ctx, post_reset)
                continue
            needs_discovery = False

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
