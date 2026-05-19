"""
Soft-reset mode (starters / gifts).

Save in front of the starter table (see TUTORIAL.md), pick the
starter in the launcher, then per attempt the bot:

  1. Runs the X/Y input sequence (Tierno cutscene → cursor to the
     chosen starter → receive it).
  2. Detects the received Pokémon by CONTENT — observe.get_party()
     locates the player-owned party in RAM by scanning for
     checksum-valid PK6 whose OT is the trainer (the same
     relocation-proof method the shiny hunt uses). No party_base /
     offset hunting, no "run debug first".
  3. Evaluates: species must be the chosen starter, then the target
     filter (shiny / IVs / nature …).
  4. Hit → stop + alert (it's in your party, go save). Miss → soft
     reset (L+R+Start) and repeat.

config.yaml soft_reset.trainer_name MUST match your in-game OT (it's
how the party is found). Default "Roman".
"""
from __future__ import annotations

import logging
import time

from ..games import starter_species, starters_for
from ..platform_utils import focus_azahar
from .observe import get_party, broadcast_party

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-game starter input sequence
# ---------------------------------------------------------------------------

def _xy_starter_sequence(ctx, starter: str, gap: float,
                         pre_taps: int, post_taps: int,
                         receive_gap: float) -> bool:
    """Pokémon X/Y starter sequence (fixed, manually-timed). Returns
    False if a stop was requested mid-run.

      1× DpadLeft        — start Tierno's cutscene
      pre_taps× A        — clear setup dialogue          (gap)
      cursor + confirm:                                  (gap)
        Chespin  → 1×A, 2×DpadLeft,  2×A
        Fennekin → 3×A  (default cursor)
        Froakie  → 1×A, 2×DpadRight, 2×A
      post_taps× B       — receive (B avoids the nickname
                            prompt)                       (receive_gap)
    The main loop confirms the starter actually arrived via
    get_party(), so post_taps is just a generous floor.
    """
    starter = (starter or "").lower()

    def _tap(button: str, sleep_for: float) -> bool:
        if ctx.should_stop():
            return False
        ctx.input.tap(button, hold_s=0.05)
        ctx._stop_evt.wait(sleep_for)
        return not ctx.should_stop()

    if not _tap("DpadLeft", gap):
        return False
    log.info(f"  X/Y: {pre_taps}× A (clear Tierno, gap {gap}s)")
    for _ in range(pre_taps):
        if not _tap("A", gap):
            return False

    if starter == "chespin":
        log.info("  X/Y: cursor → Chespin")
        seq = [("A", gap), ("DpadLeft", gap), ("DpadLeft", gap),
               ("A", gap), ("A", gap)]
    elif starter == "froakie":
        log.info("  X/Y: cursor → Froakie")
        seq = [("A", gap), ("DpadRight", gap), ("DpadRight", gap),
               ("A", gap), ("A", gap)]
    else:
        log.info("  X/Y: cursor on Fennekin (default)")
        seq = [("A", gap), ("A", gap), ("A", gap)]
    for btn, g in seq:
        if not _tap(btn, g):
            return False

    log.info(f"  X/Y: {post_taps}× B to receive (gap {receive_gap}s)")
    for _ in range(post_taps):
        if not _tap("B", receive_gap):
            return False
    return True


_SEQUENCES = {"X-USA": _xy_starter_sequence, "Y-USA": _xy_starter_sequence}


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------

def _do_reset(ctx, post_wait: float, post_taps: int, post_gap: float):
    """L+R+Start to the title, wait out the boot logos, then mash A
    (title → Continue → save-data confirm → welcome dialog)."""
    ctx.input.soft_reset()
    ctx._stop_evt.wait(post_wait)
    try:
        focus_azahar()
    except Exception:
        pass
    for _ in range(post_taps):
        if ctx.should_stop():
            return
        ctx.input.tap("A", hold_s=0.05)
        ctx._stop_evt.wait(post_gap)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(ctx):
    log.info("Mode: soft_reset (starter)")
    try:
        if focus_azahar():
            log.info("  Azahar window focused.")
    except Exception as e:
        log.warning(f"  couldn't focus Azahar: {e}")

    cfg = ctx.config.get("soft_reset", {}) or {}
    player_ot = cfg.get("trainer_name", "Roman")
    advance_taps = int(cfg.get("advance_taps", 60))
    advance_gap = float(cfg.get("advance_gap", 1.0))
    post_reset = float(cfg.get("post_reset_wait", 12.0))
    post_reset_taps = int(cfg.get("post_reset_taps", 6))
    post_reset_gap = float(cfg.get("post_reset_gap", 1.0))
    starter_name = cfg.get("starter")
    xy_pre_taps = int(cfg.get("xy_pre_taps", 25))
    xy_post_taps = int(cfg.get("xy_post_taps", 40))
    xy_receive_gap = float(cfg.get("xy_receive_gap", 1.0))
    # How long to wait for the starter to show up in the party after
    # the input sequence before declaring the attempt a miss.
    detect_tries = int(cfg.get("detect_tries", 12))
    detect_gap = float(cfg.get("detect_gap", 1.5))
    party_base = ctx.game.offsets.party_base
    party_stride = ctx.game.offsets.party_stride or 484

    starter_id = None
    if starter_name:
        starter_id = starter_species(ctx.game.key, str(starter_name))
        if starter_id:
            log.info(f"  hunting starter {starter_name} "
                     f"(#{starter_id}); OT {player_ot!r}")
        else:
            log.warning(f"  unknown starter {starter_name!r} for "
                        f"{ctx.game.key}; known: "
                        f"{list(starters_for(ctx.game.key))}")
    seq_fn = _SEQUENCES.get(ctx.game.key)
    if not (seq_fn and starter_name):
        log.info("  no game-specific starter sequence — generic A-mash.")

    attempt = 0
    while not ctx.should_stop():
        attempt += 1
        log.info(f"Soft reset attempt #{attempt}")
        ctx.dashboard.broadcast("soft_reset_attempt", count=attempt)
        try:
            focus_azahar()
        except Exception:
            pass

        # 1. Drive the game to "starter received".
        if seq_fn and starter_name:
            if not seq_fn(ctx, starter_name, advance_gap,
                          xy_pre_taps, xy_post_taps, xy_receive_gap):
                return
        else:
            for _ in range(advance_taps):
                if ctx.should_stop():
                    return
                ctx.input.tap("A", hold_s=0.05)
                ctx._stop_evt.wait(advance_gap)

        # 2. Detect the received starter by content (relocation-proof;
        #    no offsets needed). Poll until it lands in the party.
        pkm = None
        for _ in range(detect_tries):
            if ctx.should_stop():
                return
            party = get_party(ctx, party_base, party_stride, player_ot)
            if party:
                broadcast_party(ctx, party)
                lead = party[0]
                if starter_id is None or lead.species == starter_id:
                    pkm = lead
                    break
            ctx._stop_evt.wait(detect_gap)

        if pkm is None:
            log.warning(f"  attempt {attempt}: starter never appeared "
                        f"in the party (sequence likely missed the "
                        f"cursor / save not in front of the table). "
                        f"Resetting.")
            ctx.dashboard.broadcast(
                "read_failure", attempt=attempt,
                reason="starter not found in party after sequence")
            _do_reset(ctx, post_reset, post_reset_taps, post_reset_gap)
            continue

        # 3. Report + evaluate.
        ctx.dashboard.broadcast(
            "candidate", attempt=attempt,
            species=pkm.species, nickname=pkm.nickname,
            shiny=pkm.shiny, nature=pkm.nature, gender=pkm.gender,
            ivs=pkm.ivs, pid=pkm.pid, tsv=pkm.tsv, psv=pkm.psv,
            ability_id=pkm.ability_id, ability_num=pkm.ability_num,
            level=pkm.party["level"] if pkm.party else None,
            moves=pkm.moves)
        log.info(f"  starter: #{pkm.species} {pkm.nickname or ''} "
                 f"{'★SHINY★ ' if pkm.shiny else ''}"
                 f"nature={pkm.nature} IVs={pkm.ivs} "
                 f"PID={pkm.pid:08X} PSV={pkm.psv} TSV={pkm.tsv}")

        has_rules = bool(ctx.target and ctx.target.rules)
        hit = (ctx.target.matches(pkm) if has_rules
               else starter_id is not None)
        if hit:
            reason = (ctx.target.describe(pkm) if has_rules
                      else f"starter #{pkm.species}")
            bar = "*" * 30
            for line in (bar, f"  TARGET — attempt #{attempt}: {reason}",
                         "  Bot STOPPED — it's in your party. Go SAVE!",
                         bar):
                log.info(line)
            ctx.dashboard.broadcast(
                "target_hit", attempt=attempt, count=attempt,
                reason=reason, species=pkm.species, shiny=pkm.shiny,
                nature=pkm.nature, ivs=pkm.ivs)
            ctx.request_stop("target hit")
            return

        _do_reset(ctx, post_reset, post_reset_taps, post_reset_gap)
