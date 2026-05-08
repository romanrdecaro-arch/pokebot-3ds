"""
Debug mode — one-shot offset bootstrap.

Sends NO inputs to Azahar. Runs a brute-force PK6 record scan of the
gen-appropriate heap, identifies party_base, and (when the user has
set their in-game trainer name in config) records the offset between
that name's RAM address and party_base. The offset gets written back
to config.yaml so subsequent bot runs (starters, encounters, etc.)
can use the fast trainer-name anchor path instead of brute-forcing.

Run this once after any of:
  - first ever bot run on a new game / version
  - changing trainer name in-game
  - moving to a wildly different game state (the anchor offset
    occasionally drifts across major scene transitions)

The user must have a Pokémon in slot 0 of their party for this to
succeed — no party means no PK6 record means no party_base to find.
For starter hunts: just play through the starter cutscene manually
once, then save and run debug mode.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from .. import find_offsets as fo
from ..games import heap_range_for
from ..parser import decrypt_pkm, parse_pkm

log = logging.getLogger(__name__)


def run(ctx):
    log.info("Mode: debug — brute-force party_base discovery + offset bootstrap")
    log.info("This mode sends NO inputs. It just scans memory.")

    gen = getattr(ctx.game, "generation", 7) or 7
    primary_start, primary_end = heap_range_for(gen)
    span_mb = (primary_end - primary_start) // (1024 * 1024)

    cfg_section = ctx.config.get("soft_reset", {}) or {}
    trainer_name = (cfg_section.get("trainer_name")
                    or ctx.config.get("trainer_name") or "").strip()

    log.info(f"Scanning {primary_start:#010x}-{primary_end:#010x} "
             f"({span_mb} MB) for valid PK6 records…")
    ctx.dashboard.broadcast("offset_scan", state="started", attempt=0)
    try:
        hits = list(fo.scan(ctx.rpc, start=primary_start, end=primary_end))
    except Exception as e:
        log.error(f"Scan failed: {e}")
        ctx.dashboard.broadcast("offset_scan", state="fail", party_base=0)
        return

    log.info(f"Scan finished: {len(hits)} PK6 candidate(s) found.")
    if not hits:
        log.error("No PK6 records anywhere in the scan range.")
        log.error("  - Is there a Pokémon in slot 0 of your party?")
        log.error("  - PKHeX → load the live save → check the party block.")
        log.error("  - Try `python -m pokebot.find_offsets --full-heap "
                  "--save-config config.yaml` for a wider scan.")
        ctx.dashboard.broadcast("offset_scan", state="fail", party_base=0)
        return

    clusters = fo.cluster_hits(hits)
    log.info(f"Hits grouped into {len(clusters)} cluster(s).")
    for c in clusters[:8]:
        n = len(c["members"])
        if n >= 2:
            log.info(f"  cluster: start={c['start']:#010x} "
                     f"stride={c['stride']} members={n}")
        else:
            addr, info = c["members"][0]
            log.info(f"  loner:   {addr:#010x}  species=#{info['species']}")

    # Pick the strongest party-base candidate.
    discovered = fo.derive_offsets_from_clusters(clusters)
    if "party_base" not in discovered:
        # Single-loner = slot 0 in a hunt with one Pokémon.
        if len(hits) >= 1:
            addr, info = hits[0]
            discovered["party_base"] = addr
            discovered["party_stride"] = 484
            log.info(f"No 5+ cluster; using single-PK6 fallback at "
                     f"{addr:#010x} (species #{info['species']}) as "
                     f"party_base.")
        else:
            log.error("Could not derive a plausible party_base.")
            ctx.dashboard.broadcast("offset_scan", state="fail", party_base=0)
            return
    if discovered.get("foe_base") == discovered["party_base"]:
        del discovered["foe_base"]

    party_base = discovered["party_base"]
    log.info(f"party_base = {party_base:#010x} (stride 484)")

    # Apply to runtime context.
    for k, v in discovered.items():
        if hasattr(ctx.game.offsets, k):
            setattr(ctx.game.offsets, k, v)

    # Persist offsets to config.yaml.
    cfg_path = Path(__file__).resolve().parent.parent.parent / "config.yaml"
    if cfg_path.exists():
        try:
            written = fo.write_offsets_to_config(cfg_path, discovered)
            if written:
                log.info(f"Saved offsets to {cfg_path.name}: {written}")
        except Exception as e:
            log.warning(f"Could not write offsets: {e}")

    # Verify the read path — read + parse slot 0, broadcast as a
    # 'candidate' event so the user sees it in Recently Seen.
    try:
        raw = ctx.rpc.read(party_base, 260)
        pkm = parse_pkm(decrypt_pkm(raw))
    except Exception as e:
        log.warning(f"Verification read failed: {e}")
        pkm = None
    if pkm and pkm.checksum_valid:
        log.info(f"Slot 0 verifies: species=#{pkm.species} "
                 f"Lv{pkm.party['level'] if pkm.party else '?'} "
                 f"shiny={pkm.shiny} nature={pkm.nature}")
        ctx.dashboard.broadcast(
            "candidate",
            attempt=0,
            species=pkm.species, nickname=pkm.nickname,
            shiny=pkm.shiny, nature=pkm.nature, gender=pkm.gender,
            ivs=pkm.ivs, pid=pkm.pid,
            tsv=pkm.tsv, psv=pkm.psv,
            ability_id=pkm.ability_id, ability_num=pkm.ability_num,
            level=pkm.party["level"] if pkm.party else None,
            moves=pkm.moves,
        )
    else:
        log.warning("Slot 0 didn't verify after applying party_base — "
                    "the address is most likely correct anyway, but "
                    "double-check your save state.")

    # Trainer-name anchor: cache offset for fast future runs.
    if trainer_name:
        log.info(f"Searching RAM for trainer name {trainer_name!r}…")
        try:
            anchors = fo.find_pattern(
                ctx.rpc, fo.trainer_name_pattern(trainer_name),
                primary_start, primary_end)
        except Exception as e:
            log.warning(f"Trainer-name search failed: {e}")
            anchors = []
        log.info(f"Trainer name found at {len(anchors)} address(es).")
        if anchors:
            offset = party_base - anchors[0]
            log.info(f"trainer_to_party_offset = {offset:#x} "
                     f"({anchors[0]:#010x} → {party_base:#010x})")
            try:
                from .soft_reset import _write_soft_reset_setting
                _write_soft_reset_setting(
                    cfg_path, "trainer_to_party_offset", offset)
                log.info(f"Offset cached to {cfg_path.name}. "
                         f"Future runs will skip the brute-force scan.")
            except Exception as e:
                log.warning(f"Couldn't persist offset: {e}")
    else:
        log.info("No trainer_name set — skipping anchor offset caching. "
                 "Set soft_reset.trainer_name in config.yaml so future "
                 "runs can use the fast anchor path.")

    ctx.dashboard.broadcast("offset_scan", state="ok", party_base=party_base)
    log.info("Debug mode complete. Switch to Starters / Encounter mode "
             "for actual hunting — discovery is now cached.")
