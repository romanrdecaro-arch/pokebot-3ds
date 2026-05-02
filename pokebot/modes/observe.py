"""
Observe mode.

Passively reads party and foe slots, decrypts and parses them, and
reports any changes to the dashboard. Sends NO inputs. Useful for:
  - First-time setup (verifying offsets are right)
  - Watching a manual playthrough with live stats
  - Debugging the data path end-to-end before enabling active modes
"""

from __future__ import annotations

import logging
import time

from ..parser import decrypt_pkm, parse_pkm

log = logging.getLogger(__name__)


def run(ctx):
    """ctx is a BotContext (see bot.py): rpc, game, dashboard, target, stop()."""
    log.info("Mode: observe")
    last_party_sig = None
    last_foe_sig = None
    poll_interval = 0.25

    while not ctx.should_stop():
        # ---- party ------------------------------------------------------
        if ctx.game.offsets.party_base:
            party_data = []
            for slot in range(6):
                addr = (ctx.game.offsets.party_base
                        + slot * ctx.game.offsets.party_stride)
                try:
                    raw = ctx.rpc.read(addr, 260)
                except Exception as e:
                    log.warning(f"party read failed: {e}")
                    break
                # An "empty" slot has zero encryption key
                if int.from_bytes(raw[:4], "little") == 0:
                    continue
                try:
                    pt = decrypt_pkm(raw)
                    pkm = parse_pkm(pt)
                    party_data.append(_summary(pkm, slot))
                except Exception as e:
                    log.debug(f"parse failed slot {slot}: {e}")
            sig = tuple((p["species"], p["pid"]) for p in party_data)
            if sig != last_party_sig:
                ctx.dashboard.broadcast("party", slots=party_data)
                last_party_sig = sig

        # ---- foe / wild encounter ---------------------------------------
        if ctx.game.offsets.foe_base:
            try:
                raw = ctx.rpc.read(ctx.game.offsets.foe_base, 260)
            except Exception:
                raw = b""
            if raw and int.from_bytes(raw[:4], "little") != 0:
                try:
                    pkm = parse_pkm(decrypt_pkm(raw))
                    sig = (pkm.species, pkm.pid, pkm.encryption_key)
                    if sig != last_foe_sig:
                        last_foe_sig = sig
                        s = _summary(pkm, 0)
                        # 'foe' goes to the web dashboard's foe panel.
                        # 'encounter' lights up the launcher's "Recently
                        # Seen" table so observe mode shows live wild
                        # spawns the same way encounter/soft_reset do.
                        ctx.dashboard.broadcast("foe", **s)
                        ctx.dashboard.broadcast(
                            "encounter",
                            species=pkm.species, nickname=pkm.nickname,
                            shiny=pkm.shiny, nature=pkm.nature,
                            gender=pkm.gender, ivs=pkm.ivs, pid=pkm.pid,
                            tsv=pkm.tsv, psv=pkm.psv,
                            ability_id=pkm.ability_id,
                            ability_num=pkm.ability_num,
                            level=pkm.party["level"] if pkm.party else None,
                            moves=pkm.moves,
                        )
                        if ctx.target and ctx.target.matches(pkm):
                            ctx.dashboard.broadcast(
                                "target_hit",
                                reason=ctx.target.describe(pkm),
                                **s,
                            )
                            log.info(f"TARGET HIT: {ctx.target.describe(pkm)}")
                except Exception as e:
                    log.debug(f"foe parse failed: {e}")

        time.sleep(poll_interval)


def _summary(pkm, slot: int) -> dict:
    return {
        "slot":      slot,
        "species":   pkm.species,
        "form":      pkm.form,
        "nickname":  pkm.nickname,
        "level":     pkm.party["level"] if pkm.party else None,
        "shiny":     pkm.shiny,
        "nature":    pkm.nature,
        "gender":    pkm.gender,
        "ability":   pkm.ability_id,
        "ability_num": pkm.ability_num,
        "held_item": pkm.held_item,
        "moves":     pkm.moves,
        "ivs":       pkm.ivs,
        "evs":       pkm.evs,
        "pid":       pkm.pid,
        "ot":        {"name": pkm.ot_name, "tid": pkm.ot_tid, "sid": pkm.ot_sid},
        "checksum_valid": pkm.checksum_valid,
    }
