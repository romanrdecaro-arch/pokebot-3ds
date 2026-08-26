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

from .. import gen2
from ..crystal import (CrystalSession, BATTLE_NONE, BATTLE_WILD,
                       BATTLE_TRAINER)
from ..games import GB_VC_SCAN_RANGES

log = logging.getLogger(__name__)

#: Poll fast enough to catch a battle that starts and ends
#: quickly — at 660% emulation speed a fled encounter is only
#: a second or two of wall clock.
_POLL_S = 0.3
#: Read the (much larger) party block every Nth poll.
_PARTY_EVERY = 4

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
    held = getattr(p, "held_item", None)
    if held is not None:
        payload["held_item"] = held
    # Gen 2 Hidden Power uses its own formula; sending it computed here
    # stops the launcher applying the Gen 3+ one and showing a wrong
    # type and power.
    hp_type, hp_power = gen2.hidden_power(p.dvs)
    payload["hp_type"] = hp_type
    payload["hp_power"] = hp_power
    # Gen 2 stores no gender: it is derived from the Attack DV against
    # the species gender ratio, so it is computed here rather than read.
    payload["gender"] = gen2.gender(p.species, p.dvs.get("Atk", 0))
    payload.update(extra)
    return payload


def _party_signature(party: list) -> tuple:
    return tuple((p.species, p.level, p.ot_id, p.exp) for p in party)


def party_key(p) -> tuple:
    """Identity that survives levelling up, so only genuinely NEW
    Pokemon are reported as caught."""
    return (p.species, p.ot_id, p.exp // 1000)


def _describe(p) -> str:
    d = p.dvs
    return (f"#{p.species} Lv{p.level} "
            f"DVs HP{d['HP']}/At{d['Atk']}/Df{d['Def']}/"
            f"Sp{d['Spe']}/Sc{d['Spc']}"
            f"{'  *** SHINY ***' if p.shiny else ''}")


def run(ctx) -> None:
    session = CrystalSession(ctx.rpc, GB_VC_SCAN_RANGES)

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
    last_known = None
    last_mode = BATTLE_NONE
    last_foe = None
    seen_shiny: set = set()
    encounters = 0

    poll = 0
    while not ctx.should_stop():
        # Battle state FIRST, and never gated behind the party read.
        # It used to sit after an `if not party: continue`, so a single
        # unreadable party poll — which happens around a battle
        # starting — skipped the battle check entirely, and by the next
        # poll the encounter had been missed. Two Oddish went unlogged
        # exactly this way.
        mode = session.battle_mode()
        if mode != last_mode or (mode == BATTLE_WILD
                                 and _opponent_changed(session, last_foe)):
            try:
                if mode == BATTLE_WILD:
                    encounters += 1
                last_foe = _report_battle(ctx, session, mode, encounters)
            except Exception:
                log.exception("failed to report a battle transition "
                              f"(mode {mode}); continuing")
            last_mode = mode

        # The party is a much bigger read, and only changes on a catch
        # or a level-up, so it does not need checking every tick.
        poll += 1
        if poll % _PARTY_EVERY != 0:
            ctx._stop_evt.wait(_POLL_S)
            continue

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

            # A newly caught Pokemon belongs in the encounter table,
            # not only in the party strip — otherwise manual mode shows
            # an empty table for a whole session unless a wild battle
            # happens to start while it is watching.
            if last_known is not None:
                for p in party:
                    if party_key(p) not in last_known:
                        encounters += 1
                        log.info(f"  new in party: {_describe(p)}")
                        ctx.dashboard.broadcast(
                            "candidate", count=encounters, **_payload(p))
            last_known = {party_key(p) for p in party}

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

        ctx._stop_evt.wait(_POLL_S)


def _opponent_changed(session, last_foe) -> bool:
    """True when a different wild is on screen than the one reported.

    Back-to-back encounters can both fall inside one polling window,
    and mode alone would not change between them.
    """
    enemy = session.enemy()
    if enemy is None:
        return False
    return (enemy.species, enemy.level) != last_foe


def _report_battle(ctx, session, mode: int, count: int):
    """Log and broadcast one battle-state change; return the opponent."""
    if mode == BATTLE_WILD:
        enemy = session.enemy()
        if enemy is None:
            # Still report it. The encounter definitely happened, and a
            # row saying "unreadable" is far better than the table
            # silently missing an encounter the player just saw.
            log.info(f"Wild battle #{count} (opponent unreadable)")
            ctx.dashboard.broadcast(
                "encounter", count=count, confirmed=False,
                generation=2, species=0, level=None, shiny=False,
                dvs={}, ivs={}, pid=0, psv=None, tsv=None,
                note="opponent unreadable")
            return None
        log.info(f"Wild battle #{count}: #{enemy.species} "
                 f"Lv{enemy.level} DVs={enemy.dvs}"
                 f"{'  *** SHINY? ***' if enemy.shiny else ''}")
        log.info("  (opponent DV offsets are UNVERIFIED — catch it and "
                 "compare the party reading)")
        ctx.dashboard.broadcast("encounter", count=count,
                                confirmed=False, **_payload(enemy))
        return (enemy.species, enemy.level)
    if mode == BATTLE_TRAINER:
        log.info("Trainer battle")
    elif mode == BATTLE_NONE:
        log.info("Back on the overworld")
    return None
