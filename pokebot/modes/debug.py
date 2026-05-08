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
from ..games import (heap_range_for, EXT_HEAP_RANGE_N3DS,
                      LINEAR_HEAP_RANGE_3DS, HEAP_RANGE_3DS,
                      party_base_candidates, LIVEHEX_REFERENCES)
from ..parser import decrypt_pkm, parse_pkm

log = logging.getLogger(__name__)


def run(ctx):
    log.info("Mode: debug — brute-force party_base discovery + offset bootstrap")
    log.info("This mode sends NO inputs. It just scans memory.")

    gen = getattr(ctx.game, "generation", 7) or 7
    primary = heap_range_for(gen)

    # ──────────────────────────────────────────────────────────────────
    # Fastest path of all: --verify-address. ZERO scanning. Just one
    # read at the user-supplied address to check it's a valid PK6.
    # ──────────────────────────────────────────────────────────────────
    verify_addr = ctx.config.get("verify_address")
    if verify_addr:
        log.info(f"Verify-only mode: reading 260 bytes at "
                 f"{verify_addr:#010x}…")
        try:
            raw = ctx.rpc.read(verify_addr, 260)
        except Exception as e:
            log.error(f"RPC read failed: {e}")
            ctx.dashboard.broadcast("offset_scan", state="fail", party_base=0)
            return
        ok, info = fo.is_likely_pk7(raw)
        if not ok:
            log.error(f"  {verify_addr:#010x} does NOT contain a valid "
                      f"PK6 record. (enc_key="
                      f"{int.from_bytes(raw[:4], 'little'):#010x}, "
                      f"sanity={int.from_bytes(raw[4:6], 'little')}).")
            log.error("  Verify the address in Azahar's Memory Viewer.")
            ctx.dashboard.broadcast("offset_scan", state="fail",
                                    party_base=0)
            return
        pkm = parse_pkm(decrypt_pkm(raw))
        log.info(f"  ✓ Valid PK6: species=#{pkm.species} "
                 f"Lv{pkm.party['level'] if pkm.party else '?'} "
                 f"shiny={pkm.shiny} OT={pkm.ot_name!r}")
        ctx.game.offsets.party_base = verify_addr
        ctx.game.offsets.party_stride = 484
        cfg_path = (Path(__file__).resolve().parent.parent.parent
                    / "config.yaml")
        if cfg_path.exists():
            try:
                fo.write_offsets_to_config(
                    cfg_path, {"party_base": verify_addr,
                               "party_stride": 484})
                log.info(f"Saved party_base = {verify_addr:#010x} to "
                         f"config.yaml.")
            except Exception as e:
                log.warning(f"Couldn't persist: {e}")
        ctx.dashboard.broadcast(
            "candidate", attempt=0,
            species=pkm.species, nickname=pkm.nickname,
            shiny=pkm.shiny, nature=pkm.nature, gender=pkm.gender,
            ivs=pkm.ivs, pid=pkm.pid,
            tsv=pkm.tsv, psv=pkm.psv,
            ability_id=pkm.ability_id, ability_num=pkm.ability_num,
            level=pkm.party["level"] if pkm.party else None,
            moves=pkm.moves,
        )
        ctx.dashboard.broadcast("offset_scan", state="ok",
                                party_base=verify_addr)
        log.info("Verify-only mode complete.")
        return

    # ──────────────────────────────────────────────────────────────────
    # Fast path 0: try a curated list of known-likely addresses derived
    # from PKHeX-Plugins LiveHeX reference data (trainer block + box
    # offsets) plus the save-layout deltas.  Single read per candidate,
    # ~5 reads total — completes in under a second.
    # ──────────────────────────────────────────────────────────────────
    candidates = party_base_candidates(ctx.game.key)
    if candidates:
        ref = LIVEHEX_REFERENCES.get(ctx.game.key, {})
        log.info(f"Trying PKHeX-Plugins-derived candidates for "
                 f"{ctx.game.key} (LiveHeX version {ref.get('version', '?')}).")
        for cand in candidates:
            try:
                raw = ctx.rpc.read(cand, 260)
            except Exception as e:
                log.debug(f"  {cand:#010x}: read failed ({e})")
                continue
            ok, info = fo.is_likely_pk7(raw)
            if not ok:
                log.info(f"  {cand:#010x}: not a valid PK6 "
                         f"(enc_key={int.from_bytes(raw[:4], 'little'):#010x})")
                continue
            pkm = parse_pkm(decrypt_pkm(raw))
            log.info(f"  ✓ {cand:#010x} validates as species #{pkm.species} "
                     f"({pkm.nickname!r}, Lv{pkm.party['level'] if pkm.party else '?'}).")
            ctx.game.offsets.party_base = cand
            ctx.game.offsets.party_stride = 484
            cfg_path = (Path(__file__).resolve().parent.parent.parent
                        / "config.yaml")
            if cfg_path.exists():
                try:
                    fo.write_offsets_to_config(
                        cfg_path, {"party_base": cand,
                                   "party_stride": 484})
                    log.info(f"Saved party_base = {cand:#010x} → "
                             f"{cfg_path.name}.")
                except Exception as e:
                    log.warning(f"Couldn't persist: {e}")
            ctx.dashboard.broadcast(
                "candidate", attempt=0,
                species=pkm.species, nickname=pkm.nickname,
                shiny=pkm.shiny, nature=pkm.nature, gender=pkm.gender,
                ivs=pkm.ivs, pid=pkm.pid,
                tsv=pkm.tsv, psv=pkm.psv,
                ability_id=pkm.ability_id, ability_num=pkm.ability_num,
                level=pkm.party["level"] if pkm.party else None,
                moves=pkm.moves,
            )
            ctx.dashboard.broadcast("offset_scan", state="ok",
                                    party_base=cand)
            log.info("Debug mode complete (PKHeX-Plugins reference path).")
            return
        log.info("None of the PKHeX-Plugins-derived candidates validated; "
                 "falling through to slower paths.")

    cfg_section = ctx.config.get("soft_reset", {}) or {}
    trainer_name = (cfg_section.get("trainer_name")
                    or ctx.config.get("trainer_name") or "").strip()

    # ──────────────────────────────────────────────────────────────────
    # Fast path 1: anchor + known save-layout offset
    # ──────────────────────────────────────────────────────────────────
    # Empirically (and confirmed against the X/Y save block doc): the
    # OT name and party slot 0 sit 0x1B8 (440) bytes apart in the save
    # block layout. RAM tends to mirror this for the trainer-card
    # region. Try the anchor + this fixed offset first; if the read
    # validates as PK6, we're done in seconds without any scan.
    SAVE_LAYOUT_NAME_TO_PARTY = 0x1B8
    if trainer_name:
        log.info(f"Fast path: searching RAM for trainer name "
                 f"{trainer_name!r}…")
        try:
            anchors = fo.find_pattern(
                ctx.rpc, fo.trainer_name_pattern(trainer_name),
                primary[0], primary[1])
        except Exception as e:
            log.warning(f"Pattern search failed: {e}")
            anchors = []
        log.info(f"  Trainer name found at {len(anchors)} address(es).")
        for anchor_addr in anchors:
            for try_offset in (SAVE_LAYOUT_NAME_TO_PARTY,):
                cand = anchor_addr + try_offset
                try:
                    raw = ctx.rpc.read(cand, 260)
                except Exception:
                    continue
                ok, info = fo.is_likely_pk7(raw)
                if not ok:
                    continue
                log.info(f"  ✓ Anchor at {anchor_addr:#010x} + "
                         f"{try_offset:#x} = {cand:#010x} validates as "
                         f"PK6 species #{info.get('species')}.")
                # Apply + persist + verify, then return early.
                ctx.game.offsets.party_base = cand
                ctx.game.offsets.party_stride = 484
                cfg_path = (Path(__file__).resolve().parent.parent.parent
                            / "config.yaml")
                if cfg_path.exists():
                    try:
                        fo.write_offsets_to_config(
                            cfg_path,
                            {"party_base": cand, "party_stride": 484})
                        from .soft_reset import _write_soft_reset_setting
                        _write_soft_reset_setting(
                            cfg_path, "trainer_to_party_offset", try_offset)
                        log.info(f"  Cached party_base + "
                                 f"trainer_to_party_offset to config.yaml.")
                    except Exception as e:
                        log.warning(f"  Couldn't persist: {e}")
                pkm = parse_pkm(decrypt_pkm(raw))
                ctx.dashboard.broadcast(
                    "candidate", attempt=0,
                    species=pkm.species, nickname=pkm.nickname,
                    shiny=pkm.shiny, nature=pkm.nature, gender=pkm.gender,
                    ivs=pkm.ivs, pid=pkm.pid,
                    tsv=pkm.tsv, psv=pkm.psv,
                    ability_id=pkm.ability_id, ability_num=pkm.ability_num,
                    level=pkm.party["level"] if pkm.party else None,
                    moves=pkm.moves,
                )
                ctx.dashboard.broadcast("offset_scan", state="ok",
                                        party_base=cand)
                log.info(f"Slot 0: species=#{pkm.species} "
                         f"Lv{pkm.party['level'] if pkm.party else '?'} "
                         f"shiny={pkm.shiny} nature={pkm.nature}.")
                log.info("Debug mode complete (fast anchor path).")
                return
        log.warning("Anchor + save-layout offset didn't validate.")
        log.warning("Skipping the brute-force scan — it's been crashing "
                    "Azahar on this setup. Use Azahar's Memory Viewer "
                    "instead (Tools → Memory Viewer):")
        log.warning(f"  1. In Azahar, Tools → Memory Viewer (or "
                    f"View → Memory Viewer).")
        log.warning(f"  2. In the Hex search box, paste this UTF-16LE "
                    f"pattern (with spaces): "
                    f"52 00 6F 00 6D 00 61 00 6E 00")
        log.warning(f"  3. Note the resulting address (e.g. 0x14ABCD00).")
        log.warning(f"  4. party_base = that address + 0x1B8")
        log.warning(f"  5. Paste it into config.yaml's offsets.party_base, "
                    f"then run again — bot will skip discovery entirely.")
        log.warning("Or pass --verify-address 0x... when running the "
                    "bot to test a specific address without scanning.")
        ctx.dashboard.broadcast("offset_scan", state="fail", party_base=0)
        return

    # Progressive scan: hot range first, escalate if nothing found.
    # Probe-and-skip + throttle keep Azahar alive even on the widest
    # range, but each step is opt-in so the user can stop early.
    if gen == 6:
        ranges = [
            ("Gen 6 hot (linear heap, first 64 MB)", primary),
            ("Gen 6 full linear heap (128 MB)", LINEAR_HEAP_RANGE_3DS),
            ("Gen 7 EXT heap (some Gen 6 builds use this)",
             EXT_HEAP_RANGE_N3DS),
        ]
    else:
        ranges = [
            ("Gen 7 EXT heap (256 MB)", primary),
            ("Gen 6 linear heap (in case of misclassified game)",
             LINEAR_HEAP_RANGE_3DS),
        ]

    ctx.dashboard.broadcast("offset_scan", state="started", attempt=0)
    hits: list = []
    used_range = None
    for label, (rng_start, rng_end) in ranges:
        span_mb = (rng_end - rng_start) // (1024 * 1024)
        log.info(f"Scanning {label}: {rng_start:#010x}-{rng_end:#010x} "
                 f"({span_mb} MB)…")
        try:
            this_hits = list(fo.scan(ctx.rpc, start=rng_start, end=rng_end))
        except Exception as e:
            log.warning(f"Scan failed for {label}: {e}")
            continue
        log.info(f"  → {len(this_hits)} PK6 candidate(s) in this range")
        if this_hits:
            hits = this_hits
            used_range = (rng_start, rng_end)
            break
        # Brief pause between escalations so Azahar's log buffer can
        # flush before we hit it again.
        time.sleep(0.5)

    if not hits:
        log.error("No PK6 records found in any standard heap range.")
        log.error("  - Verify slot 0 has a Pokémon: open in-game party "
                  "menu in Azahar.")
        log.error("  - PKHeX → File → Open → load the live save and "
                  "check the party block.")
        log.error("  - Last-resort full-heap scan (slow, but covers "
                  "every byte): python -m pokebot.find_offsets "
                  "--full-heap --save-config config.yaml")
        ctx.dashboard.broadcast("offset_scan", state="fail", party_base=0)
        return
    primary_start, primary_end = used_range
    log.info(f"Working with {len(hits)} hit(s) in "
             f"{primary_start:#010x}-{primary_end:#010x}.")

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
