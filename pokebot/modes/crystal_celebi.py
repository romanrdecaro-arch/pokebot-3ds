"""
Soft-reset shiny hunt for Celebi (Pokemon Crystal, Virtual Console).

One attempt is: spam A until a battle starts, read the opponent, and
if it is not shiny soft-reset and do it again. When a shiny turns up
the bot STOPS COMPLETELY and hands the fight back to the player — it
does not throw a ball, does not run, does not press another button.
Catching a 1-in-8192 encounter is not something to automate on top of
an input path this indirect.

    celebi_hunt:
      press_interval: 0.5     # seconds between A presses
      encounter_timeout: 120  # give up A-spamming after this long
      reset_hold: 0.6         # how long to hold A+B+Start+Select
      boot_timeout: 90        # wait this long for the save to reload
      enemy_dv_check: paranoid

Setup
-----
Save standing in front of the Ilex Forest shrine with the GS Ball
already placed, so that one A press starts the Celebi encounter. After
a soft reset the game returns to the title screen; the same A-spam
walks it through the copyright, the title and CONTINUE, and back to
the shrine, which is why the whole loop is just "press A".

Why the reset is the Game Boy combo
-----------------------------------
A+B+Start+Select is handled by the Gen 2 ROM's own joypad routine, so
it resets the emulated Game Boy and leaves Azahar, the Virtual Console
process, and the located WRAM base untouched. That last part is what
makes the hunt cheap: the base is found once and reused for every
attempt, instead of re-scanning the heap thousands of times.
"""
from __future__ import annotations

import logging
import time

from .. import gen2
from ..crystal import (CrystalSession, BATTLE_NONE, BATTLE_TRAINER,
                       GB_ENEMY_DVS, SIGNATURE_LEN)
from ..games import GB_VC_SCAN_RANGES

log = logging.getLogger(__name__)

#: How often to check the battle byte while spamming A.
_POLL_S = 0.15

#: The opponent's region, per Data Crystal's Crystal RAM map. Paranoid
#: mode treats every 2-byte window in here as a possible DV word.
_ENEMY_REGION_LO = 0xD204
_ENEMY_REGION_HI = 0xD22E

#: An opponent record is only trustworthy once the game has actually
#: filled it in; right as the battle byte flips it can still be blank.
_OPPONENT_SETTLE_S = 2.5


def _shiny_candidates(buf: bytes) -> list:
    """Every offset in the opponent region that reads as a shiny DV word."""
    out = []
    for i in range(len(buf) - 1):
        word = int.from_bytes(buf[i:i + 2], "big")
        if gen2.is_shiny(gen2.parse_dvs(word)):
            out.append((_ENEMY_REGION_LO + i, word))
    return out


def _read_opponent(ctx, session, settle: float = _OPPONENT_SETTLE_S):
    """Wait for a plausible opponent record, then return it.

    The battle byte flips before the game has written wEnemyMon, so
    reading immediately can catch a half-filled struct — which for a
    shiny hunt would be a made-up answer to the only question that
    matters.
    """
    deadline = time.monotonic() + settle
    best = None
    while time.monotonic() < deadline and not ctx.should_stop():
        enemy = session.enemy()
        if enemy is not None:
            best = enemy
            if (1 <= enemy.species <= gen2.MAX_SPECIES
                    and 1 <= enemy.level <= 100 and enemy.max_hp > 0):
                return enemy
        ctx._stop_evt.wait(0.1)
    return best


def _spam_a_until_battle(ctx, session, press_interval: float,
                         timeout: float) -> int:
    """Press A until a battle starts. Returns the battle mode, or NONE.

    A battle only counts once the overworld has been seen first. Coming
    out of a soft reset the game is on its title screen, where the
    battle byte holds whatever survived the reset rather than a real
    battle state — and acting on that would read a garbage opponent and
    reset straight past whatever the next attempt would have been.
    """
    deadline = time.monotonic() + timeout
    presses = 0
    seen_overworld = False
    while not ctx.should_stop() and time.monotonic() < deadline:
        for _ in range(2):
            mode = session.battle_mode()
            if mode == BATTLE_NONE:
                seen_overworld = True
            elif seen_overworld:
                log.info(f"  battle after {presses} A presses")
                return mode
            # First half of the loop presses, second half just re-checks
            # so a fast encounter is not missed for a whole interval.
            if not _:
                ctx.input.tap("A", hold_s=0.06)
                presses += 1
                ctx._stop_evt.wait(press_interval)
            else:
                ctx._stop_evt.wait(_POLL_S)
    return BATTLE_NONE


def _wait_for_reload(ctx, session, timeout: float) -> bool:
    """Wait for the save to come back after a soft reset.

    Reads the party block DIRECTLY rather than through ensure_base.
    That matters: while the game sits on the title screen there is no
    party to find, and ensure_base would read that as a lost base and
    start scanning the heap — thousands of times over a long hunt, for
    a base that never actually moved.
    """
    deadline = time.monotonic() + timeout
    while not ctx.should_stop() and time.monotonic() < deadline:
        buf = session._read(gen2.GB_PARTY_COUNT, SIGNATURE_LEN)
        if buf and gen2.looks_like_party(buf, 0):
            return True
        ctx.input.tap("A", hold_s=0.06)
        ctx._stop_evt.wait(0.5)
    return False


def _payload(enemy, attempt: int, shiny: bool) -> dict:
    spc = enemy.dvs.get("Spc", 0)
    hp_type, hp_power = gen2.hidden_power(enemy.dvs)
    return {
        "count": attempt,
        "confirmed": False,
        "generation": 2,
        "species": enemy.species,
        "level": enemy.level,
        "shiny": shiny,
        "dvs": dict(enemy.dvs),
        "ivs": {"HP": enemy.dvs.get("HP", 0), "Atk": enemy.dvs.get("Atk", 0),
                "Def": enemy.dvs.get("Def", 0), "Spe": enemy.dvs.get("Spe", 0),
                "SpA": spc, "SpD": spc},
        "pid": 0, "psv": None, "tsv": None,
        "hp_type": hp_type,
        "hp_power": hp_power,
        "gender": gen2.gender(enemy.species, enemy.dvs.get("Atk", 0)),
    }


def _stop_for_shiny(ctx, enemy, attempt: int, hits: list) -> None:
    bar = "*" * 44
    lines = [bar,
             f"  SHINY on attempt #{attempt}",
             f"  #{enemy.species} Lv{enemy.level} DVs {enemy.dvs}",
             *(f"    shiny DV word {w:04X} at {a:#06x}" for a, w in hits),
             "",
             "  The bot has STOPPED and will not press another button.",
             "  Azahar is yours — go and catch it.",
             bar]
    for line in lines:
        log.info(line)
    ctx.dashboard.broadcast("target_hit", reason="shiny Celebi (Gen 2 DVs)",
                            **_payload(enemy, attempt, True))
    ctx.request_stop("shiny found — over to you")


def _dv_check_failed(ctx, enemy) -> None:
    """The opponent's DVs do not explain its max HP, so stop.

    A wrong DV offset does not crash and does not look wrong — it
    quietly calls every Celebi ordinary and resets past the shiny.
    Max HP is derived from the HP DV by the game's own formula, so a
    disagreement means the reading is worthless and so is the hunt.
    """
    expected = gen2.max_hp(gen2.BASE_HP[enemy.species],
                           enemy.dvs["HP"], enemy.level)
    bar = "*" * 44
    for line in (bar,
                 "  DV READING FAILED ITS OWN CHECK — hunt stopped.",
                 f"  #{enemy.species} Lv{enemy.level}: these DVs mean max HP "
                 f"should be {expected}, but the game says {enemy.max_hp}.",
                 "  Shininess cannot be trusted, so nothing is being reset",
                 "  past. Re-run scripts/find_enemy_dvs.py.",
                 bar):
        log.error(line)
    ctx.dashboard.broadcast("read_failure",
                            reason="enemy DV offset failed its max-HP check")
    ctx.request_stop("DV reading failed its max-HP check")


def run(ctx) -> None:
    cfg = (ctx.config.get("celebi_hunt") or {})
    press_interval = float(cfg.get("press_interval", 0.5))
    encounter_timeout = float(cfg.get("encounter_timeout", 120.0))
    reset_hold = float(cfg.get("reset_hold", 0.6))
    boot_timeout = float(cfg.get("boot_timeout", 90.0))
    check = str(cfg.get("enemy_dv_check", "paranoid")).lower()

    session = CrystalSession(ctx.rpc, GB_VC_SCAN_RANGES)
    log.info("Mode: Crystal Celebi soft-reset hunt")
    log.info("  Save in front of the Ilex Forest shrine with the GS Ball "
             "placed, so one A press starts the encounter.")

    base = session.ensure_base()
    if base is None:
        log.error("Could not find Crystal's WRAM — is the game loaded with "
                  "a Pokemon in your party?")
        ctx.dashboard.broadcast("read_failure",
                                reason="crystal wram not found")
        ctx.request_stop("crystal wram not found")
        return
    log.info(f"  WRAM base {base:#010x}")

    try:
        from ..platform_utils import focus_azahar
        if not focus_azahar():
            log.warning("  could not focus Azahar — keypresses may not "
                        "register. Click the emulator window once.")
    except Exception as exc:
        log.warning(f"  focus_azahar failed: {exc}")

    if check == "strict":
        log.info(f"  shiny check: strict, opponent DVs at {GB_ENEMY_DVS:#06x}")
    else:
        log.info("  shiny check: PARANOID — every candidate offset in the "
                 "opponent region is tested, so a shiny cannot be missed.")
    ctx.dashboard.broadcast("status", mode="crystal_celebi",
                            wram_base=f"{base:#010x}")

    attempt = 0
    while not ctx.should_stop():
        attempt += 1
        log.info(f"Attempt #{attempt}: pressing A…")

        mode = _spam_a_until_battle(ctx, session, press_interval,
                                    encounter_timeout)
        if ctx.should_stop():
            return
        if mode == BATTLE_NONE:
            log.error(f"No battle after {encounter_timeout:.0f}s of A "
                      f"presses. Either the save is not in front of the "
                      f"shrine, or Azahar is not receiving input — run "
                      f"scripts/test_input.py to tell those apart.")
            ctx.dashboard.broadcast("read_failure",
                                    reason="no encounter from A presses")
            ctx.request_stop("no encounter — check input and save position")
            return
        if mode == BATTLE_TRAINER:
            log.warning("  that is a TRAINER battle, not Celebi. Stopping "
                        "rather than resetting blind.")
            ctx.request_stop("unexpected trainer battle")
            return

        enemy = _read_opponent(ctx, session)
        if enemy is None:
            log.warning("  opponent unreadable; resetting and retrying.")
            ctx.input.gb_soft_reset(hold_s=reset_hold)
            _wait_for_reload(ctx, session, boot_timeout)
            continue

        region = session._read(_ENEMY_REGION_LO,
                               _ENEMY_REGION_HI - _ENEMY_REGION_LO)
        hits = _shiny_candidates(region) if region else []
        is_shiny = enemy.shiny if check == "strict" else bool(hits)

        note = ""
        if enemy.species != gen2.CELEBI_SPECIES:
            note = f"   (expected Celebi, got #{enemy.species})"
        log.info(f"  #{enemy.species} Lv{enemy.level} maxHP {enemy.max_hp} "
                 f"DVs {enemy.dvs}{note}")

        if enemy.dv_check is False:
            _dv_check_failed(ctx, enemy)
            return
        if enemy.dv_check is True:
            log.info(f"  DV reading confirmed against max HP "
                     f"({enemy.max_hp})")

        ctx.dashboard.broadcast("encounter",
                                **_payload(enemy, attempt, is_shiny))
        if is_shiny:
            _stop_for_shiny(ctx, enemy, attempt, hits)
            return

        log.info(f"  not shiny — soft resetting (attempt {attempt} done)")
        ctx.input.gb_soft_reset(hold_s=reset_hold)
        if not _wait_for_reload(ctx, session, boot_timeout):
            log.error(f"  save did not come back within {boot_timeout:.0f}s "
                      f"of the reset. Stopping so the game is not left in "
                      f"an unknown state.")
            ctx.request_stop("game did not reload after soft reset")
            return
