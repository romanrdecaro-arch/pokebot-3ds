"""
Encounter mode.

Walks back-and-forth in tall grass, watches for the in-battle flag to
flip, reads the foe Pokémon, evaluates against the target. On a target
hit: stops inputs and broadcasts. On a non-hit: flees and resumes
walking.

Active inputs require the input_driver (pynput); without it the mode
will run in dry-run and only ever observe (won't actually walk).

Required offsets:
  - foe_base
  - in_battle_flag
"""

from __future__ import annotations

import logging
import time

from ..parser import decrypt_pkm, parse_pkm

log = logging.getLogger(__name__)


def run(ctx):
    log.info("Mode: encounter")
    if not ctx.game.offsets.foe_base:
        log.error("foe_base offset is 0 -- cannot run encounter mode. "
                  "Use the offset finder to populate it.")
        return
    if not ctx.game.offsets.in_battle_flag:
        log.warning("in_battle_flag is 0 -- falling back to "
                    "polling foe block for changes (less reliable).")

    encounters = 0
    walking = True
    last_foe_key = None

    # primitive walking loop — alternates left/right while not in battle
    walk_dir_iter = _alternating_dirs()

    while not ctx.should_stop():
        in_battle = _check_in_battle(ctx)

        if not in_battle:
            if walking:
                d = next(walk_dir_iter)
                ctx.input.tap(d, hold_s=0.30)
            time.sleep(0.05)
            continue

        # In battle: stop walking, read the foe.
        try:
            raw = ctx.rpc.read(ctx.game.offsets.foe_base, 260)
        except Exception as e:
            log.warning(f"foe read failed: {e}")
            time.sleep(0.5)
            continue
        if int.from_bytes(raw[:4], "little") == 0:
            # The flag flipped but the foe block isn't populated yet.
            time.sleep(0.1)
            continue
        try:
            pkm = parse_pkm(decrypt_pkm(raw))
        except Exception as e:
            log.debug(f"foe parse failed: {e}")
            time.sleep(0.2)
            continue
        if not pkm.checksum_valid:
            time.sleep(0.1)
            continue

        key = (pkm.encryption_key, pkm.pid)
        if key == last_foe_key:
            # Same encounter we already evaluated; wait for it to end.
            time.sleep(0.2)
            continue
        last_foe_key = key
        encounters += 1
        ctx.dashboard.broadcast(
            "encounter",
            count=encounters,
            species=pkm.species, nickname=pkm.nickname,
            shiny=pkm.shiny, nature=pkm.nature, gender=pkm.gender,
            ivs=pkm.ivs, pid=pkm.pid,
            tsv=pkm.tsv, psv=pkm.psv,
            ability_id=pkm.ability_id, ability_num=pkm.ability_num,
            level=pkm.party["level"] if pkm.party else None,
            moves=pkm.moves,
        )

        if ctx.target and ctx.target.matches(pkm):
            log.info(f"TARGET! enc#{encounters}: {ctx.target.describe(pkm)}")
            ctx.dashboard.broadcast(
                "target_hit",
                count=encounters,
                reason=ctx.target.describe(pkm),
                species=pkm.species, shiny=pkm.shiny,
                nature=pkm.nature, ivs=pkm.ivs,
            )
            walking = False
            ctx.request_stop("target hit")
            return

        # Not a hit: flee. Press B+Down repeatedly to navigate the run menu.
        # Real menu navigation will need refinement per game; this is a
        # generic best-effort.
        for _ in range(5):
            ctx.input.tap("B", 0.05)
            time.sleep(0.1)
        # wait for battle flag to clear
        for _ in range(100):
            if not _check_in_battle(ctx):
                break
            time.sleep(0.1)


def _check_in_battle(ctx) -> bool:
    addr = ctx.game.offsets.in_battle_flag
    if not addr:
        # fallback: presence of a non-zero foe encryption key
        try:
            return ctx.rpc.read_u32(ctx.game.offsets.foe_base) != 0
        except Exception:
            return False
    try:
        # read a u8 by default; some games use u32 -- adjust per-game later
        return ctx.rpc.read_u8(addr) != 0
    except Exception:
        return False


def _alternating_dirs():
    while True:
        yield "DpadLeft"
        yield "DpadRight"
