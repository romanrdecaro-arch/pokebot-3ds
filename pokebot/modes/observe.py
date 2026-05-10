"""
Observe mode — manual control with live encounter detection.

The bot sends NO inputs; the user plays the game normally. The bot
watches RAM for valid PK6/PK7 records and reports any *new* Pokémon
it sees (wild battle foes, gifts, hatched eggs, caught Pokémon
landing in the party).

Strategy: an adaptive scanner.

  Phase 1 — Baseline:
    One full-heap pass to catalogue every valid PK6 currently in RAM
    (the player's party + any box slots). Their encryption keys go
    into ``seen_keys`` so we don't re-announce them.

  Phase 2 — Hot-poll + periodic rescan:
    Tight loop reads each ``known_addr`` (every PK6 the baseline
    found) every 0.6 s. If a hot-poll reads an unfamiliar enc_key,
    that address is now hosting a different Pokémon (a fresh wild
    foe, the next box slot user clicked into, etc.) — report it
    immediately. Hot-poll is instant.

    Periodically (every ~30 s of wallclock) trigger a full-heap
    rescan in a background thread to find Pokémon that landed at
    new addresses. Hot-poll keeps running while the rescan goes,
    so wild encounters happening DURING the rescan still get
    caught the moment their address joins ``known_addrs``.

No offsets required. Works in dry-run, never sends keys.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from ..parser import decrypt_pkm, parse_pkm

log = logging.getLogger(__name__)


_HOT_POLL_INTERVAL_S    = 0.6     # how often to re-read each known addr
_FULL_RESCAN_INTERVAL_S = 30.0    # min seconds between full-heap rescans


def run(ctx) -> None:
    log.info("Mode: observe (manual control + live detection)")

    seen_keys: set[int] = set()      # enc_keys we've already announced
    known_addrs: set[int] = set()    # addresses that hold a valid PK6
    addr_lock = threading.Lock()
    last_full_scan = [0.0]
    rescan_in_flight = [False]

    # ── Fast path: probe the LiveHeX-published addresses directly ──────
    # Heap scans waste minutes when we already know where the data is.
    # PKHeX-Plugins LiveHeX has verified addresses for X/Y / OR/AS /
    # SM / USUM. If the read at trainer_block lands valid data, we can
    # find party slots by candidate offsets without scanning at all.
    fast_addrs = _try_livehex_fast_path(ctx)
    if fast_addrs:
        log.info(f"  fast path OK — {len(fast_addrs)} known address(es) "
                 f"will hot-poll without a heap scan.")
        for addr, key in fast_addrs:
            seen_keys.add(key)
            known_addrs.add(addr)
    else:
        # ── Baseline (heap scan) ────────────────────────────────────────
        log.info("Baseline scan — cataloguing existing party + box Pokémon "
                 "(can take 30-90 s).")
        initial = _full_scan(ctx)
        if initial is None:
            log.error("Baseline scan found no Pokémon and no LiveHeX "
                      "mapping is set for this game. Stop.")
            return
        for addr, key in initial:
            seen_keys.add(key)
            known_addrs.add(addr)
        log.info(f"Baseline: {len(known_addrs)} Pokémon catalogued. "
                 f"Watching for new encounters now.")
    last_full_scan[0] = time.monotonic()

    # ── Watch loop ──────────────────────────────────────────────────────
    while not ctx.should_stop():
        # Hot-poll every known address. Cheap (one 260-byte RPC read each).
        # If the data changed (different enc_key), we caught a new
        # Pokémon at an existing slot — typical for the foe slot during
        # a wild battle, or a box slot the user clicked into.
        with addr_lock:
            addrs = list(known_addrs)
        for addr in addrs:
            if ctx.should_stop():
                return
            try:
                raw = ctx.rpc.read(addr, 260)
            except Exception:
                continue
            enc_key = int.from_bytes(raw[:4], "little")
            if enc_key == 0 or enc_key in seen_keys:
                continue
            # Fresh enc_key at a known address. Decrypt and report.
            try:
                pkm = parse_pkm(decrypt_pkm(raw))
            except Exception:
                continue
            if not pkm.checksum_valid:
                continue
            seen_keys.add(enc_key)
            _report(ctx, pkm, addr, source="hot-poll")

        # Trigger a background full-heap rescan if it's time and one
        # isn't already running.
        now = time.monotonic()
        if (now - last_full_scan[0] > _FULL_RESCAN_INTERVAL_S
                and not rescan_in_flight[0]):
            rescan_in_flight[0] = True
            last_full_scan[0] = now
            threading.Thread(
                target=_background_rescan,
                args=(ctx, seen_keys, known_addrs, addr_lock,
                      rescan_in_flight),
                name="ObserveRescan", daemon=True
            ).start()

        time.sleep(_HOT_POLL_INTERVAL_S)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _try_livehex_fast_path(ctx) -> list[tuple[int, int]]:
    """Read the published LiveHeX addresses directly. Skip the heap scan
    when we get hits.

    Returns ``[(data_addr, enc_key), ...]`` of every PK6 we found
    (party slots + box1 slot1 if non-empty). Empty list when:
      - the game has no LiveHeX mapping
      - the trainer-block read returns unmapped/zero bytes (the
        published address doesn't apply to the user's setup)
      - none of the candidate party addresses parse as a PK6
    """
    from ..livehex_compat import (
        livehex_version_for, get_trainer_block_offset,
        get_trainer_block_size, get_b1s1_offset, get_slot_size,
        get_gap_size, LiveHeXVersion,
    )
    from ..find_offsets import is_likely_pk7
    from ..games import party_base_candidates

    game_key = ctx.game.key
    lv = livehex_version_for(game_key)
    if lv == LiveHeXVersion.UNKNOWN:
        log.info("  fast path: no LiveHeX mapping for this game.")
        return []

    tb_addr = get_trainer_block_offset(lv)
    tb_size = get_trainer_block_size(lv) or 0x100
    b1 = get_b1s1_offset(lv)            # box1 slot1 — used by brute-scan below

    # 1. Trainer block sanity probe — confirms the published address
    #    actually maps to something in this user's emulator session.
    log.info(f"  fast path: probing trainer block @ {tb_addr:#010x}")
    try:
        tb = ctx.rpc.read(tb_addr, tb_size)
    except Exception as e:
        log.info(f"  fast path: trainer block read failed: {e}")
        return []
    if not tb or tb == b"\x00" * len(tb) or tb == b"\xFF" * len(tb):
        log.info("  fast path: trainer block read returned unmapped/zero "
                 "bytes; falling back to heap scan.")
        return []
    log.info(f"  fast path: trainer block looks live "
             f"(first 8 bytes = {tb[:8].hex()})")

    found: list[tuple[int, int]] = []

    # 2. Try each candidate party_base. The first one that parses as a
    #    valid PK6 is the player's party.
    candidates = party_base_candidates(game_key) or []
    party_base: Optional[int] = None
    party_stride = ctx.game.offsets.party_stride or 484
    for cand in candidates:
        try:
            raw = ctx.rpc.read(cand, 260)
        except Exception as e:
            log.info(f"  fast path: candidate {cand:#010x} read failed: {e}")
            continue
        ok, _ = is_likely_pk7(raw)
        ek = int.from_bytes(raw[:4], "little")
        if ok:
            party_base = cand
            log.info(f"  fast path: party_base = {cand:#010x}")
            break
        log.info(f"  fast path: candidate {cand:#010x} not PK6 "
                 f"(enc_key={ek:#010x}, first16={raw[:16].hex()})")
        # Empty slot 0 still tells us this is the right anchor IF the
        # next slot reads valid. Try slot 1 as confirmation.
        if ek == 0:
            try:
                raw1 = ctx.rpc.read(cand + party_stride, 260)
            except Exception:
                continue
            ok1, _ = is_likely_pk7(raw1)
            if ok1:
                party_base = cand
                log.info(f"  fast path: party_base = {cand:#010x} "
                         f"(slot 0 empty, slot 1 valid)")
                break

    if party_base is None:
        # Candidates didn't match — but trainer block IS live, so the
        # data is somewhere in the save block. Brute-force a targeted
        # scan: walk tb..b1+0x100 in 4-byte steps, looking for any
        # valid PK6 record. Probe-and-skip is OFF here (we know the
        # region is mapped), so it can't skip past data hiding in a
        # zero-padded 1 MB block.
        scan_lo = min(tb_addr, b1) - 0x100
        scan_hi = max(tb_addr, b1) + 0x500
        log.info(f"  fast path: targeted PK6 scan in "
                 f"[{scan_lo:#010x}, {scan_hi:#010x}) ({scan_hi - scan_lo:#x} bytes)")
        try:
            chunk = ctx.rpc.read(scan_lo, scan_hi - scan_lo)
        except Exception as e:
            log.warning(f"  fast path: targeted read failed: {e}")
            chunk = b""
        if chunk:
            from ..parser import decrypt_pkm, calc_checksum
            strict_hits: list[tuple[int, dict]] = []
            relaxed_hits: list[tuple[int, int]] = []  # (addr, species)
            for off in range(0, len(chunk) - 260 + 1, 4):
                rec = chunk[off:off + 260]
                ok, info = is_likely_pk7(rec)
                if ok and info:
                    strict_hits.append((scan_lo + off, info))
                    continue
                # Relaxed path: skip the sanity==0 / level<=100 / nature<=24
                # checks and JUST verify the checksum after decrypt. The
                # X/Y in-RAM party slot might carry extra metadata that
                # makes is_likely_pk7's strict filters reject it even when
                # the underlying PK6 is fine.
                ek = int.from_bytes(rec[:4], "little")
                if ek == 0 or ek == 0xFFFFFFFF:
                    continue
                try:
                    pt = decrypt_pkm(rec)
                    stored = int.from_bytes(pt[6:8], "little")
                    if calc_checksum(pt) == stored:
                        species = int.from_bytes(pt[8:10], "little")
                        if 0 < species <= 1000:
                            relaxed_hits.append((scan_lo + off, species))
                except Exception:
                    pass
            log.info(f"  fast path: strict PK6 hits: {len(strict_hits)}, "
                     f"relaxed (checksum-only) hits: {len(relaxed_hits)}")
            for hit_addr, hit_info in strict_hits[:6]:
                log.info(f"    strict  {hit_addr:#010x} species=#{hit_info.get('species')}")
            for hit_addr, sp in relaxed_hits[:6]:
                log.info(f"    relaxed {hit_addr:#010x} species=#{sp}")
            picks = strict_hits or [(a, {"species": s}) for a, s in relaxed_hits]
            if picks:
                # Lowest address is most likely party slot 0; party
                # comes before boxes in the save layout.
                party_base = picks[0][0]
                tag = "strict" if strict_hits else "relaxed"
                log.info(f"  fast path: party_base = {party_base:#010x} "
                         f"({tag} brute-force pick)")

    if party_base is not None:
        ctx.game.offsets.party_base = party_base
        # Read all 6 slots
        for slot in range(6):
            addr = party_base + slot * party_stride
            try:
                raw = ctx.rpc.read(addr, 260)
            except Exception:
                continue
            if int.from_bytes(raw[:4], "little") == 0:
                continue
            ok, info = is_likely_pk7(raw)
            if ok and info:
                found.append((addr, info["enc_key"]))
                log.info(f"  fast path: slot {slot} @ {addr:#010x} "
                         f"species #{info.get('species', '?')}")

    # 3. Box 1 slot 1 — gives us a stable hot-poll target during play
    #    (the user might move Pokémon between PC slots while observing).
    if b1:
        try:
            raw = ctx.rpc.read(b1, 232)
            ok, info = is_likely_pk7(raw + b"\x00" * 28)
            if ok and info:
                found.append((b1, info["enc_key"]))
                log.info(f"  fast path: box1 slot1 @ {b1:#010x} "
                         f"species #{info.get('species', '?')}")
        except Exception:
            pass

    return found


def _full_scan(ctx) -> Optional[list[tuple[int, int]]]:
    """One full-heap pass. Returns [(addr, enc_key), ...] or None on error.

    Walks every heap range in priority order (hot save-block region
    first, then wider fallbacks) looking for Pokemon Accessor structs.
    Stops early as soon as a range yields hits — the ranges that
    follow are fallbacks for when the priority range is empty.
    """
    from ..find_offsets import scan_accessors
    from ..games import scan_ranges_for
    out: list[tuple[int, int]] = []
    seen: set[int] = set()
    for r_lo, r_hi in scan_ranges_for(ctx.game.generation):
        log.info(f"  scan range {r_lo:#x}-{r_hi:#x}…")
        before = len(out)
        try:
            for data_addr, info in scan_accessors(ctx.rpc, r_lo, r_hi,
                                                  chunk=0x4000, throttle_s=0.005):
                if ctx.should_stop():
                    return out
                k = info["enc_key"]
                if k in seen:
                    continue
                seen.add(k)
                out.append((data_addr, k))
        except Exception as e:
            log.warning(f"  range {r_lo:#x} failed: {e}")
            continue
        new_in_range = len(out) - before
        log.info(f"  range {r_lo:#x}-{r_hi:#x}: +{new_in_range} accessor(s)")
        if new_in_range > 0:
            # Found data here; subsequent ranges are fallbacks. Stop.
            break
    return out if out else None


def _background_rescan(ctx, seen_keys: set[int], known_addrs: set[int],
                       lock: threading.Lock, flag: list) -> None:
    """Run a full-heap pass off the hot-poll thread, merge results."""
    try:
        results = _full_scan(ctx)
        if results is None:
            return
        new_addr_count = 0
        new_pkm_count = 0
        for addr, enc_key in results:
            if enc_key in seen_keys:
                # Already announced — but the address might be new
                # (Pokémon got moved). Track it for hot-polling.
                with lock:
                    if addr not in known_addrs:
                        known_addrs.add(addr)
                        new_addr_count += 1
                continue
            # New enc_key — read the full record and report it.
            try:
                raw = ctx.rpc.read(addr, 260)
                pkm = parse_pkm(decrypt_pkm(raw))
            except Exception:
                continue
            if not pkm.checksum_valid:
                continue
            seen_keys.add(enc_key)
            with lock:
                known_addrs.add(addr)
            new_pkm_count += 1
            _report(ctx, pkm, addr, source="rescan")
        if new_pkm_count or new_addr_count:
            log.info(f"  rescan: {new_pkm_count} new Pokémon, "
                     f"{new_addr_count} relocated address(es).")
    finally:
        flag[0] = False


def _report(ctx, pkm, addr: int, source: str) -> None:
    """Broadcast one encounter event for a freshly-seen Pokémon."""
    log.info(f"NEW Pokémon @ {addr:#010x} ({source}): "
             f"#{pkm.species} {pkm.nickname or ''} "
             f"{'★ SHINY ' if pkm.shiny else ''}"
             f"PID={pkm.pid:08X}")
    from ..parser import encounter_payload
    ctx.dashboard.broadcast(
        "encounter",
        source=source, address=f"{addr:#010x}",
        **encounter_payload(pkm))
    if ctx.target and ctx.target.matches(pkm):
        ctx.dashboard.broadcast(
            "target_hit",
            reason=ctx.target.describe(pkm),
            species=pkm.species, shiny=pkm.shiny,
            nature=pkm.nature, ivs=pkm.ivs,
        )
        log.info(f"TARGET HIT: {ctx.target.describe(pkm)}")
