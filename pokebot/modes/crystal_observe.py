"""
Manual control for Pokémon Crystal (Virtual Console).

The bot sends NO input — you play normally. It watches Crystal's Game
Boy WRAM and reports what it sees: your party, any wild battle you
walk into, and whether anything is shiny.

Crystal shares nothing with the Gen 6/7 modes. There is no encryption
key to key detection off, so a wild encounter is detected from the
battle-mode byte going 0 -> 1, and party changes from the party block
itself. Shininess is DVs: Def/Spe/Spc all 10 and Atk in
{2,3,6,7,10,11,14,15}, i.e. 1 in 8192.
"""
from __future__ import annotations

import logging

from ..crystal import (CrystalSession, BATTLE_NONE, BATTLE_WILD,
                       BATTLE_TRAINER)
from ..games import HEAP_RANGE_3DS, EXT_HEAP_RANGE_N3DS

log = logging.getLogger(__name__)

_POLL_S = 1.0

#: Gen 2 has ONE Special stat; Gen 6 split it into SpA/SpD. The
#: launcher's table expects the six-stat shape, so Special is reported
#: under both — that is what it governs, not an invented value.
def _iv_payload(dvs: dict) -> dict:
    spc = dvs.get("Spc", 0)
    return {"HP": dvs.get("HP", 0), "Atk": dvs.get("Atk", 0),
            "Def": dvs.get("Def", 0), "Spe": dvs.get("Spe", 0),
            "SpA": spc, "SpD": spc}


def _payload(p, **extra) -> dict:
    """A Gen 2 record shaped for the dashboard and the event log.

    Takes either a party ``Gen2Pokemon`` or an in-battle
    ``EnemyReading``. They genuinely carry different fields — an
    opponent has no Original Trainer or experience until it is caught —
    so the ones only a party record has are read defensively rather
    than assumed.
    """
    payload = {
        "species": p.species,
        "level": p.level,
        "shiny": p.shiny,
        "ivs": _iv_payload(p.dvs),
        "dvs": dict(p.dvs),          # the real Gen 2 values, unmapped
        "generation": 2,
        # Gen 2 has no PID and no TSV, so the shiny-value columns do
        # not apply. Sent explicitly so nothing downstream invents one.
        "pid": 0,
        "psv": None,
        "tsv": None,
    }
    ot_id = getattr(p, "ot_id", None)
    if ot_id is not None:
        payload["ot_id"] = ot_id
    payload.update(extra)
    return payload


def _party_signature(party: list) -> tuple:
    return tuple((p.species, p.level, p.ot_id, p.exp) for p in party)


def _describe(p) -> str:
    d = p.dvs
    return (f"#{p.species} Lv{p.level} "
            f"DVs HP{d['HP']}/At{d['Atk']}/Df{d['Def']}/"
            f"Sp{d['Spe']}/Sc{d['Spc']}"
            f"{'  *** SHINY ***' if p.shiny else ''}")


def run(ctx) -> None:
    ranges = [HEAP_RANGE_3DS, EXT_HEAP_RANGE_N3DS]
    session = CrystalSession(ctx.rpc, ranges)

    log.info("Mode: Crystal manual control — the bot sends NO input.")
    log.info("  Locating Crystal's WRAM (first run scans the heap)…")
    base = session.ensure_base()
    if base is None:
        log.error("Could not find Crystal's WRAM.")
        log.error("Load the Crystal VC title and make sure your save has "
                  "at least one Pokemon in the party, then start again.")
        ctx.dashboard.broadcast("read_failure", reason="crystal wram not found")
        ctx.request_stop("crystal wram not found")
        return
    log.info(f"  WRAM base {base:#010x}")
    # "status", not "ready": bot.py already broadcasts ready, and two
    # of them printed the launcher's "bot is ready" line twice.
    ctx.dashboard.broadcast("status", mode="crystal_observe",
                            wram_base=f"{base:#010x}")

    last_sig = None
    last_mode = BATTLE_NONE
    seen_shiny: set = set()
    encounters = 0

    while not ctx.should_stop():
        party = session.party()
        if not party:
            # Losing the base mid-session is normal if the emulator is
            # restarted; ensure_base re-locates on the next pass.
            ctx._stop_evt.wait(_POLL_S)
            continue

        sig = _party_signature(party)
        if sig != last_sig:
            last_sig = sig
            log.info(f"Party ({len(party)}):")
            for i, p in enumerate(party, 1):
                log.info(f"  slot {i}: {_describe(p)}")
            ctx.dashboard.broadcast(
                "party",
                slots=[{"slot": i, **_payload(p)}
                       for i, p in enumerate(party)])

            for i, p in enumerate(party):
                key = (p.species, p.ot_id, p.exp)
                if p.shiny and key not in seen_shiny:
                    seen_shiny.add(key)
                    encounters += 1
                    bar = "*" * 30
                    for line in (bar,
                                 f"  SHINY IN YOUR PARTY — slot {i + 1}",
                                 f"  {_describe(p)}",
                                 bar):
                        log.info(line)
                    ctx.dashboard.broadcast(
                        "target_hit", count=encounters,
                        reason="shiny (Gen 2 DVs)", **_payload(p))

        mode = session.battle_mode()
        if mode != last_mode:
            # Report inside a guard: this is a watch that should run for
            # a whole play session, and one malformed reading must not
            # end it. The error is logged in full so it stays visible
            # rather than being silently swallowed.
            try:
                _report_battle(ctx, session, mode, encounters + 1)
                if mode == BATTLE_WILD:
                    encounters += 1
            except Exception:
                log.exception("failed to report a battle transition "
                              f"(mode {mode}); continuing")
            last_mode = mode

        ctx._stop_evt.wait(_POLL_S)


def _report_battle(ctx, session, mode: int, count: int) -> None:
    """Log and broadcast one battle-state change."""
    if mode == BATTLE_WILD:
        enemy = session.enemy()
        if enemy is None:
            log.info(f"Wild battle #{count} (opponent unreadable)")
            return
        log.info(f"Wild battle #{count}: #{enemy.species} "
                 f"Lv{enemy.level} DVs={enemy.dvs}"
                 f"{'  *** SHINY? ***' if enemy.shiny else ''}")
        log.info("  (opponent DV offsets are UNVERIFIED — catch it and "
                 "compare the party reading)")
        ctx.dashboard.broadcast("encounter", count=count,
                                confirmed=False, **_payload(enemy))
    elif mode == BATTLE_TRAINER:
        log.info("Trainer battle")
    elif mode == BATTLE_NONE:
        log.info("Back on the overworld")
