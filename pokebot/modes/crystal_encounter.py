"""
Random-encounter hunting for Pokémon Crystal (Virtual Console).

Walks back and forth on the configured axis to trigger wild battles,
evaluates each one, and runs from anything that is not a target.

    random_encounters:
      movement: horizontal    # horizontal (Left/Right) | vertical (Up/Down)
      walk_hold: 0.20         # seconds per step
      flee_settle: 1.2        # pause before the RUN sequence

Detecting a shiny opponent, honestly
------------------------------------
The party structure is verified; the in-battle opponent structure is
not, so the exact address of its DVs is still unknown. That matters
here more than anywhere else: reading the wrong byte would silently
MISS shinies, which is the one failure a hunt cannot recover from.

So there are two checking modes:

* ``strict`` — read the one configured offset. Correct and quiet once
  scripts/find_enemy_dvs.py has confirmed it. False-stop rate 1/8192.
* ``paranoid`` (default while unconfirmed) — treat EVERY two-byte
  window in the opponent's region as a candidate DV word and stop if
  any of them reads shiny. It cannot miss a shiny wherever the real
  offset turns out to be; the cost is stopping on a coincidence
  roughly once every 200 encounters, which a human resolves in a
  glance.

Run scripts/find_enemy_dvs.py to confirm the offset and switch to
strict — that turns ~1 spurious stop per 200 encounters into 1 per
8192.
"""
from __future__ import annotations

import logging

from .. import gen2
from ..crystal import (CrystalSession, BATTLE_NONE, BATTLE_WILD,
                       BATTLE_TRAINER, GB_ENEMY_DVS)
from ..games import HEAP_RANGE_3DS, EXT_HEAP_RANGE_N3DS

log = logging.getLogger(__name__)

#: Walk axes, as 3DS d-pad buttons (the VC layer maps them to the GB pad).
_AXES = {"horizontal": ("DpadLeft", "DpadRight"),
         "vertical": ("DpadUp", "DpadDown")}

#: The opponent's region, per Data Crystal's Crystal RAM map. Paranoid
#: mode treats every 2-byte window in here as a possible DV word.
_ENEMY_REGION_LO = 0xD204
_ENEMY_REGION_HI = 0xD22E

_POLL_S = 0.25


def _shiny_candidates(buf: bytes) -> list:
    """Every offset in the opponent region that reads as a shiny DV word."""
    out = []
    for i in range(len(buf) - 1):
        word = int.from_bytes(buf[i:i + 2], "big")
        if gen2.is_shiny(gen2.parse_dvs(word)):
            out.append((_ENEMY_REGION_LO + i, word))
    return out


def _flee(ctx, settle: float) -> None:
    """Select RUN from the Gen 2 battle menu and clear the text.

    The menu is a 2x2 grid with the cursor starting on FIGHT:

        FIGHT  PKMN
        PACK   RUN

    so RUN is one step down and one step right, then A. Gen 2 has no
    touch screen, which is a small mercy — this needs no mouse at all.
    """
    ctx._stop_evt.wait(settle)
    for button in ("DpadDown", "DpadRight", "A"):
        if ctx.should_stop():
            return
        ctx.input.tap(button, hold_s=0.06)
        ctx._stop_evt.wait(0.35)
    # Clear "Got away safely!" and the fade back to the overworld.
    for _ in range(3):
        if ctx.should_stop():
            return
        ctx.input.tap("B", hold_s=0.06)
        ctx._stop_evt.wait(0.45)


def run(ctx) -> None:
    cfg = (ctx.config.get("random_encounters") or {})
    movement = str(cfg.get("movement", "horizontal")).lower()
    if movement not in _AXES:
        movement = "horizontal"
    walk_hold = float(cfg.get("walk_hold", 0.20))
    flee_settle = float(cfg.get("flee_settle", 1.2))
    check = str(cfg.get("enemy_dv_check", "paranoid")).lower()
    step_a, step_b = _AXES[movement]

    session = CrystalSession(ctx.rpc, [HEAP_RANGE_3DS, EXT_HEAP_RANGE_N3DS])
    log.info(f"Mode: Crystal random encounters ({movement}, "
             f"{walk_hold:.2f}s steps)")
    base = session.ensure_base()
    if base is None:
        log.error("Could not find Crystal's WRAM — is the game loaded "
                  "with a Pokemon in your party?")
        ctx.dashboard.broadcast("read_failure",
                                reason="crystal wram not found")
        ctx.request_stop("crystal wram not found")
        return
    log.info(f"  WRAM base {base:#010x}")

    # Bring Azahar to the front once, as the Gen 6 modes do: the
    # emulator only acts on input while its window is focused.
    try:
        from ..platform_utils import focus_azahar
        if not focus_azahar():
            log.warning("  could not focus Azahar — keypresses may not "
                        "register. Click the emulator window once.")
    except Exception as exc:
        log.warning(f"  focus_azahar failed: {exc}")

    if check == "strict":
        log.info(f"  shiny check: strict, opponent DVs at "
                 f"{GB_ENEMY_DVS:#06x}")
    else:
        log.info("  shiny check: PARANOID — every candidate offset in the "
                 "opponent region is tested, so a shiny cannot be missed "
                 "while the real offset is unconfirmed.")
        log.info("  Expect a spurious stop roughly every 200 encounters. "
                 "Run scripts/find_enemy_dvs.py to confirm the offset, "
                 "then set random_encounters.enemy_dv_check: strict.")
    ctx.dashboard.broadcast("status", mode="crystal_encounter",
                            wram_base=f"{base:#010x}", movement=movement)

    encounters = 0
    step = 0
    last_mode = session.battle_mode()

    while not ctx.should_stop():
        mode = session.battle_mode()

        if mode == BATTLE_WILD and last_mode != BATTLE_WILD:
            encounters += 1
            enemy = session.enemy()
            region = session._read(
                _ENEMY_REGION_LO, _ENEMY_REGION_HI - _ENEMY_REGION_LO)
            hits = _shiny_candidates(region) if region else []

            species = enemy.species if enemy else "?"
            level = enemy.level if enemy else "?"
            log.info(f"Encounter #{encounters}: #{species} Lv{level}")

            is_target = bool(hits) if check != "strict" else bool(
                enemy and enemy.shiny)
            if enemy is not None:
                ctx.dashboard.broadcast(
                    "encounter", count=encounters, confirmed=False,
                    species=enemy.species, level=enemy.level,
                    shiny=is_target, generation=2,
                    dvs=dict(enemy.dvs), pid=0, psv=None, tsv=None,
                    ivs={"HP": enemy.dvs["HP"], "Atk": enemy.dvs["Atk"],
                         "Def": enemy.dvs["Def"], "Spe": enemy.dvs["Spe"],
                         "SpA": enemy.dvs["Spc"], "SpD": enemy.dvs["Spc"]},
                    gender=gen2.gender(enemy.species, enemy.dvs["Atk"]))

            if is_target:
                bar = "*" * 34
                for line in (bar,
                             f"  POSSIBLE SHINY — encounter #{encounters}",
                             f"  #{species} Lv{level}",
                             *(f"    shiny DV word {w:04X} at {a:#06x}"
                               for a, w in hits),
                             "  Bot STOPPED — check the screen.",
                             bar):
                    log.info(line)
                ctx.dashboard.broadcast(
                    "target_hit", count=encounters,
                    reason="possible shiny (Gen 2 DVs)",
                    species=species, generation=2)
                ctx.request_stop("possible shiny found")
                return

            _flee(ctx, flee_settle)
            last_mode = session.battle_mode()
            continue

        if mode == BATTLE_TRAINER:
            if last_mode != BATTLE_TRAINER:
                log.info("Trainer battle — pausing until it ends.")
            last_mode = mode
            ctx._stop_evt.wait(1.0)
            continue

        if mode == BATTLE_NONE:
            # tap() holds the button for hold_s, which is exactly a
            # walking step. Gen 2 has no "run" modifier to hold.
            ctx.input.tap(step_a if step % 2 == 0 else step_b,
                          hold_s=walk_hold)
            step += 1

        last_mode = mode
        ctx._stop_evt.wait(_POLL_S)
