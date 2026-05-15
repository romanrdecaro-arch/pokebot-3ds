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
import re
import threading
import time
from pathlib import Path
from typing import Optional

from ..find_offsets import is_likely_pk7
from ..parser import decrypt_pkm, parse_pkm

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent


def _persist_party_base(addr: int) -> None:
    """Write party_base into config.yaml's offsets: block (line-aware).

    Once written, the bot reads it back via the config-offset override
    path on every subsequent run, so the LiveHeX probe + targeted scan
    can be skipped entirely.
    """
    cfg = ROOT / "config.yaml"
    if not cfg.exists():
        return
    try:
        text = cfg.read_text(encoding="utf-8")
    except Exception as e:
        log.warning(f"  could not read {cfg.name}: {e}")
        return
    new_line = f"  party_base: {addr:#010x}"
    out, n = re.subn(r"^( *)party_base:\s*[^\n]+", new_line, text,
                     count=1, flags=re.MULTILINE)
    if n == 0:
        log.warning(f"  party_base line not found in {cfg.name}; "
                    f"{addr:#010x} kept in memory only this run.")
        return
    try:
        cfg.write_text(out, encoding="utf-8")
        log.info(f"  saved party_base = {addr:#010x} to {cfg.name}")
    except Exception as e:
        log.warning(f"  could not write {cfg.name}: {e}")


_HOT_POLL_INTERVAL_S    = 0.6     # how often to re-read each known addr
_FULL_RESCAN_INTERVAL_S = 30.0    # min seconds between full-heap rescans


def run(ctx) -> None:
    log.info("Mode: observe (manual control + live detection)")

    seen_keys: set[int] = set()      # enc_keys we've already announced
    known_addrs: set[int] = set()    # addresses that hold a valid PK6
    addr_lock = threading.Lock()
    last_full_scan = [0.0]
    rescan_in_flight = [False]

    # ── Cached path: config.yaml already has a party_base ─────────────
    # Once _persist_party_base has written it (or the user set it
    # manually), bot.py applies it to ctx.game.offsets.party_base.
    # Read the 6 slots straight off it — no probe, no scan.
    cached_pb = ctx.game.offsets.party_base
    fast_addrs: list[tuple[int, int]] = []
    if cached_pb:
        stride = ctx.game.offsets.party_stride or 484
        log.info(f"  cached party_base = {cached_pb:#010x} (config) — "
                 f"reading slots directly.")
        for slot in range(6):
            a = cached_pb + slot * stride
            try:
                raw = ctx.rpc.read(a, 260)
            except Exception:
                continue
            if int.from_bytes(raw[:4], "little") == 0:
                continue
            ok, info = is_likely_pk7(raw)
            if ok and info:
                fast_addrs.append((a, info["enc_key"]))
                log.info(f"  cached: slot {slot} @ {a:#010x} "
                         f"species #{info.get('species', '?')}")
        if not fast_addrs:
            log.warning("  cached party_base read nothing valid — "
                        "falling back to the LiveHeX probe.")

    # ── Fast path: probe the LiveHeX-published addresses directly ──────
    # Heap scans waste minutes when we already know where the data is.
    # PKHeX-Plugins LiveHeX has verified addresses for X/Y / OR/AS /
    # SM / USUM. If the read at trainer_block lands valid data, we can
    # find party slots by candidate offsets without scanning at all.
    if not fast_addrs:
        fast_addrs = _try_livehex_fast_path(ctx)
    if not fast_addrs:
        # The old behaviour fell through to a full-heap scan here. With
        # Azahar's 1 KB-per-request read ceiling, scanning 200+ MB takes
        # 10-15 min — effectively "scanning indefinitely". We don't do
        # that any more: the fast path already dumped the save-block
        # region for analysis. Stop cleanly so the log is readable.
        log.error("Could not locate the party via the LiveHeX fast "
                  "path. See the tb+... dump lines above — paste them "
                  "back so we can pin the exact offset/format. "
                  "(No full-heap scan: it can't finish at the 1 KB "
                  "RPC read limit.)")
        return
    log.info(f"  fast path OK — {len(fast_addrs)} known address(es) "
             f"will hot-poll without a heap scan.")
    for addr, key in fast_addrs:
        seen_keys.add(key)
        known_addrs.add(addr)
    last_full_scan[0] = time.monotonic()

    # ── Foe finder ──────────────────────────────────────────────────────
    # The wild foe in a battle is NOT at party_base — it's written to a
    # battle buffer elsewhere in the save-block region. We can't full-
    # heap scan (1 KB read ceiling → 15 min), but a *bounded* targeted
    # diff-scan of the region around the known party/box block is fast
    # (~150 reads, ~2 s) and runs on a background thread so it doesn't
    # stall the 0.6 s hot-poll. Any valid PK6 there whose enc_key isn't
    # one of ours = a foe (or a caught/hatched/PC mon). Its address is
    # added to known_addrs so hot-poll tracks it from then on, and the
    # first foe address is persisted as foe_base for encounter mode.
    if cached_pb or fast_addrs:
        from ..livehex_compat import (livehex_version_for,
                                      get_b1s1_offset)
        pb = cached_pb or min(a for a, _ in fast_addrs)
        lv = livehex_version_for(ctx.game.key)
        b1 = get_b1s1_offset(lv) if lv else 0
        scan_lo = pb - 0x8000
        scan_hi = (b1 + 0x10000) if b1 else (pb + 0x20000)
        threading.Thread(
            target=_foe_finder,
            args=(ctx, seen_keys, known_addrs, addr_lock,
                  scan_lo, scan_hi),
            name="ObserveFoeFinder", daemon=True
        ).start()
        log.info(f"  foe finder watching "
                 f"[{scan_lo:#010x}, {scan_hi:#010x}) every 3 s.")

    # Push the party into the launcher's Party tab right away, then
    # refresh it periodically so catches / evolutions / level-ups show.
    party_base_now = ctx.game.offsets.party_base or cached_pb
    party_stride_now = ctx.game.offsets.party_stride or 484
    if party_base_now:
        _broadcast_party(ctx, party_base_now, party_stride_now)

    # ── Watch loop ──────────────────────────────────────────────────────
    loop_n = 0
    while not ctx.should_stop():
        loop_n += 1
        # Refresh the Party tab every ~5 s (8 hot-poll ticks).
        if party_base_now and loop_n % 8 == 0:
            _broadcast_party(ctx, party_base_now, party_stride_now)

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

        time.sleep(_HOT_POLL_INTERVAL_S)


def _foe_finder(ctx, seen_keys: set, known_addrs: set,
                lock: "threading.Lock", scan_lo: int, scan_hi: int) -> None:
    """Background loop: bounded targeted diff-scan for new PK6 records.

    Reads [scan_lo, scan_hi) in 1 KB-capped chunks (rpc.read handles
    that), walks it 4 bytes at a time for strict-valid PK6 records, and
    any whose enc_key isn't in seen_keys is announced + its address
    added to known_addrs (so the hot-poll thread tracks it cheaply
    from then on). The first such address is persisted as foe_base.
    """
    span = scan_hi - scan_lo
    foe_persisted = False
    while not ctx.should_stop():
        time.sleep(3.0)
        try:
            chunk = ctx.rpc.read(scan_lo, span)
        except Exception as e:
            log.debug(f"  foe finder read failed: {e}")
            continue
        for off in range(0, len(chunk) - 260 + 1, 4):
            rec = chunk[off:off + 260]
            ek = int.from_bytes(rec[:4], "little")
            if ek == 0 or ek == 0xFFFFFFFF or ek in seen_keys:
                continue
            ok, info = is_likely_pk7(rec)
            if not (ok and info):
                continue
            addr = scan_lo + off
            try:
                pkm = parse_pkm(decrypt_pkm(rec))
            except Exception:
                continue
            if not pkm.checksum_valid:
                continue
            seen_keys.add(ek)
            with lock:
                known_addrs.add(addr)
            _report(ctx, pkm, addr, source="foe-finder")
            if not foe_persisted:
                _persist_foe_base(addr)
                foe_persisted = True


def _broadcast_party(ctx, party_base: int, stride: int) -> None:
    """Read the 6 party slots and emit a 'party' event for the launcher's
    Party tab.

    Only non-empty slots are included; the launcher's _PartyStrip keys
    by the 'slot' field and renders the rest empty. Cheap: 6 × 260-byte
    reads (each one well under the 1 KB RPC ceiling).
    """
    slots: list[dict] = []
    for slot in range(6):
        addr = party_base + slot * stride
        try:
            raw = ctx.rpc.read(addr, 260)
        except Exception:
            continue
        if int.from_bytes(raw[:4], "little") == 0:
            continue
        try:
            pkm = parse_pkm(decrypt_pkm(raw))
        except Exception:
            continue
        if not pkm.checksum_valid:
            continue
        slots.append({
            "slot":     slot,
            "species":  pkm.species,
            "form":     pkm.form,
            "nickname": pkm.nickname,
            "level":    pkm.party["level"] if pkm.party else None,
            "shiny":    pkm.shiny,
            "nature":   pkm.nature,
            "gender":   pkm.gender,
            "ivs":      pkm.ivs,
            "pid":      pkm.pid,
        })
    ctx.dashboard.broadcast("party", slots=slots)


def _persist_foe_base(addr: int) -> None:
    """Write foe_base into config.yaml's offsets: block (line-aware)."""
    cfg = ROOT / "config.yaml"
    if not cfg.exists():
        return
    try:
        text = cfg.read_text(encoding="utf-8")
    except Exception:
        return
    out, n = re.subn(r"^( *)foe_base:\s*[^\n]+",
                     f"  foe_base: {addr:#010x}", text,
                     count=1, flags=re.MULTILINE)
    if n:
        try:
            cfg.write_text(out, encoding="utf-8")
            log.info(f"  saved foe_base = {addr:#010x} to {cfg.name}")
        except Exception:
            pass


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
            # Third interpretation: the decrypted "Pokemon Temp Data"
            # layout (dex@0x18, form@0x1A, nature@0x20, IVs@0x24..0x2E).
            # X/Y keeps the active party as plaintext working structs
            # in some builds — if so, no PK6 record will ever validate
            # but the temp layout reads cleanly.
            temp_hits: list[tuple[int, dict]] = []
            for off in range(0, len(chunk) - 0x40, 4):
                seg = chunk[off:off + 0x40]
                dex = int.from_bytes(seg[0x18:0x1A], "little")
                nature = int.from_bytes(seg[0x20:0x22], "little")
                ivs = [int.from_bytes(seg[o:o + 2], "little")
                       for o in range(0x24, 0x30, 2)]
                if (0 < dex <= 721 and nature <= 24
                        and all(v <= 31 for v in ivs)
                        and any(v > 0 for v in ivs)):
                    temp_hits.append((scan_lo + off, {
                        "dex": dex, "nature": nature, "ivs": ivs}))
            if temp_hits:
                log.info(f"  fast path: TempData-format hits: "
                         f"{len(temp_hits)}")
                for ha, hi in temp_hits[:6]:
                    log.info(f"    temp    {ha:#010x} dex=#{hi['dex']} "
                             f"nature={hi['nature']} IVs={hi['ivs']}")

            picks = strict_hits or [(a, {"species": s}) for a, s in relaxed_hits]
            if picks:
                # Lowest address is most likely party slot 0; party
                # comes before boxes in the save layout.
                party_base = picks[0][0]
                tag = "strict" if strict_hits else "relaxed"
                log.info(f"  fast path: party_base = {party_base:#010x} "
                         f"({tag} brute-force pick)")
            elif not temp_hits:
                # Nothing found in ANY format. Dump the region so we
                # can analyse it, rather than fall through to a
                # 200+ MB heap scan that takes 15 min at the 1 KB
                # RPC read ceiling.
                log.warning("  fast path: no PK6 or TempData records in "
                            "the save-block region. Dumping for analysis:")
                for dump_off in (0x100, 0x1B8, 0x1C0, 0x200, 0x300,
                                 0x400, 0x600):
                    a = tb_addr + dump_off
                    rel = a - scan_lo
                    if 0 <= rel < len(chunk) - 0x30:
                        seg = chunk[rel:rel + 0x30]
                        log.warning(f"    tb+{dump_off:#05x} "
                                    f"({a:#010x}): {seg.hex()}")

    if party_base is not None:
        ctx.game.offsets.party_base = party_base
        _persist_party_base(party_base)
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
