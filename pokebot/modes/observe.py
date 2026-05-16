"""
Observe mode — manual control with live encounter detection.

The bot sends NO inputs; the user plays normally. Detection works off
the game's own ``pml::pokepara::Accessor`` structs, NOT the save block.

Why: the save block (PKHeX-Plugins' trainer/party addresses) is a
*stale* snapshot for X/Y — it only syncs on save, so a leveled-up or
freshly-caught Pokémon never appears there. The game's LIVE party and
the wild battle foe are reached through Accessor objects:

    0x0  u32  vtable pointer        (into the code segment)
    0x4  u32  is_pkm_in_party       (0 / 1)
    0x8  u32  pkm_data_ptr          (heap pointer to the PK6 record)
    0xC  u8   is_pkm_data_encrypted (0 / 1)
    0xD  u8   encrypt_pkm_data      (0 / 1)

Strategy:
  1. Locate accessors by signature scan (tight: vtable in code seg,
     bool fields, heap-pointing data ptr → near-zero false positives).
     Cached to .pokebot_accessors.json; re-validated cheaply on the
     next run so the slow scan only happens once.
  2. is_in_party=1 accessors → the live party (Party tab).
  3. Hot-poll every known accessor: re-read the 14-byte struct, follow
     its (possibly moved) data_ptr, decrypt+parse. Level-ups, catches,
     evolutions and the wild foe slot all surface here.
  4. Periodic bounded rescan picks up accessors that appear later
     (e.g. the wild foe's accessor when a battle starts).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Optional

from ..find_offsets import is_likely_accessor
from ..parser import calc_checksum, decrypt_pkm, encounter_payload, parse_pkm

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
_ACCESSOR_CACHE = ROOT / ".pokebot_accessors.json"

_HOT_POLL_INTERVAL_S = 0.6
_RESCAN_INTERVAL_S   = 20.0


# ---------------------------------------------------------------------------
# pkm validation (relaxed — tolerates the non-zero sanity word that live
# in-RAM slots carry, but still rejects misaligned garbage)
# ---------------------------------------------------------------------------

def _parse_valid(pt: bytes):
    """Return a ParsedPokemon if ``pt`` (decrypted 260 bytes) is a sane
    record, else None. Checksum + species/level/nature/ability gates.
    """
    try:
        if calc_checksum(pt) != int.from_bytes(pt[6:8], "little"):
            return None
        species = int.from_bytes(pt[8:10], "little")
        if not (0 < species <= 721):
            return None
        pkm = parse_pkm(pt)
    except Exception:
        return None
    lvl = pkm.party["level"] if pkm.party else None
    if lvl is None or not (1 <= lvl <= 100):
        return None
    if pkm.nature_id > 24 or pkm.ability_num not in (0, 1, 2, 4):
        return None
    return pkm


# ---------------------------------------------------------------------------
# Accessor read / dereference
# ---------------------------------------------------------------------------

def _read_accessor(ctx, acc_addr: int):
    """Read+validate the 14-byte accessor at acc_addr. Returns
    ``(is_in_party, data_ptr, is_encrypted)`` or None.
    """
    try:
        buf = ctx.rpc.read(acc_addr, 14)
    except Exception:
        return None
    ok, info = is_likely_accessor(buf)
    if not ok:
        return None
    return info["is_in_party"], info["data_ptr"], info["is_encrypted"]


def _diag_accessor(ctx, acc_addr: int, info: dict) -> None:
    """Deep-dump one signature-matched candidate: raw bytes at its
    data_ptr plus both the PK6 and the decrypted-"Temp Data"
    interpretations. Lets us identify what these structs actually are
    when the normal parse rejects them.
    """
    dp = info["data_ptr"]
    try:
        raw = ctx.rpc.read(dp, 0x90)
    except Exception as e:
        log.info(f"    diag {acc_addr:#010x}: data read failed: {e}")
        return
    log.info(f"    diag {acc_addr:#010x} vt={info['vtable']:#010x} "
             f"party={int(info['is_in_party'])} data={dp:#010x} "
             f"enc={int(info['is_encrypted'])}")
    log.info(f"      raw[0x00:0x20]={raw[0x00:0x20].hex()}")
    log.info(f"      raw[0x20:0x40]={raw[0x20:0x40].hex()}")
    # PK6 interpretation (decrypt + checksum).
    try:
        full = ctx.rpc.read(dp, 260)
        pt = decrypt_pkm(full)
        sp = int.from_bytes(pt[8:10], "little")
        stored = int.from_bytes(pt[6:8], "little")
        calc = calc_checksum(pt)
        lvl = pt[0xEC]
        log.info(f"      [PK6] species={sp} csum stored={stored:#06x} "
                 f"calc={calc:#06x} match={stored == calc} lvl(0xEC)={lvl}")
    except Exception as e:
        log.info(f"      [PK6] decrypt failed: {e}")
    # Temp Data interpretation (decrypted flat struct).
    try:
        dex = int.from_bytes(raw[0x18:0x1A], "little")
        form = raw[0x1A]
        pid = int.from_bytes(raw[0x08:0x0C], "little")
        nature = int.from_bytes(raw[0x20:0x22], "little")
        ivs = [int.from_bytes(raw[o:o + 2], "little")
               for o in range(0x24, 0x30, 2)]
        ok = (0 < dex <= 721 and nature <= 24
              and all(v <= 31 for v in ivs))
        log.info(f"      [Temp] dex={dex} form={form} pid={pid:#010x} "
                 f"nature={nature} IVs={ivs} plausible={ok}")
    except Exception as e:
        log.info(f"      [Temp] parse failed: {e}")


def _pkm_via_accessor(ctx, acc_addr: int):
    """Follow an accessor to its live pkm and parse it.

    Returns ``(pkm, data_ptr, is_in_party)`` or None. Re-reads the
    accessor each call so a moved data_ptr is followed automatically.
    """
    acc = _read_accessor(ctx, acc_addr)
    if acc is None:
        return None
    is_in_party, data_ptr, is_enc = acc
    try:
        raw = ctx.rpc.read(data_ptr, 260)
    except Exception:
        return None
    if len(raw) < 232:
        return None
    if int.from_bytes(raw[:4], "little") in (0, 0xFFFFFFFF):
        return None
    try:
        pt = decrypt_pkm(raw) if is_enc else raw
    except Exception:
        return None
    pkm = _parse_valid(pt)
    if pkm is None:
        return None
    return pkm, data_ptr, is_in_party


# ---------------------------------------------------------------------------
# Accessor scan
# ---------------------------------------------------------------------------

def _scan_accessors(ctx, lo: int, hi: int) -> list[int]:
    """Walk [lo, hi) for accessor structs that dereference to a valid
    Pokémon. Returns the list of accessor addresses found.

    Reads in 0x10000 sub-chunks (1 KB RPC-capped internally), probe-
    and-skips obviously-unmapped 1 MB blocks, and only does the extra
    deref+decrypt read when the 14-byte signature matches (rare → cheap).
    """
    CHUNK   = 0x10000        # 64 KB per read (1 KB RPC-capped internally)
    OVERLAP = 0x20           # > 14 so a boundary-straddling struct isn't lost
    found: list[int] = []
    sig_matches = 0
    sample_logged = 0
    cur = lo
    t_last = time.monotonic()
    while cur < hi and not ctx.should_stop():
        if time.monotonic() - t_last > 3.0:
            pct = 100 * (cur - lo) / max(1, hi - lo)
            log.info(f"  accessor scan {cur:#010x} ({pct:.0f}%) — "
                     f"{sig_matches} sig-match, {len(found)} valid")
            t_last = time.monotonic()

        n = min(CHUNK, hi - cur)
        if n < 14:                       # nothing meaningful left
            break

        # Cheap 0x40-byte probe. If it's non-zero we read+scan the full
        # 64 KB; if zero we advance ONE 64 KB block (NOT a whole 1 MB —
        # accessors sit deeper in 1 MB blocks whose first chunk is
        # zero-padding, and the old 1 MB jump skipped ~99% of the heap).
        try:
            probe = ctx.rpc.read(cur, 0x40)
        except Exception:
            cur += n
            continue
        if (not probe or probe == b"\x00" * len(probe)
                or probe == b"\xFF" * len(probe)):
            cur += n                     # skip just this 64 KB
            continue
        try:
            block = ctx.rpc.read(cur, n)
        except Exception:
            cur += n
            continue

        for off in range(0, len(block) - 14 + 1, 4):
            ok, info = is_likely_accessor(block[off:off + 14])
            if not ok:
                continue
            sig_matches += 1
            acc_addr = cur + off
            if _pkm_via_accessor(ctx, acc_addr) is not None:
                found.append(acc_addr)
                log.info(f"  VALID accessor @ {acc_addr:#010x}")
            elif sample_logged < 30:
                # Failed normal parse — deep-dump it (small candidate
                # set now the signature is tight, so dump them all).
                _diag_accessor(ctx, acc_addr, info)
                sample_logged += 1

        # Guaranteed forward progress. Full chunks overlap by OVERLAP
        # so a 14-byte struct on a boundary isn't missed; the final
        # partial chunk has no successor so it's just consumed.
        cur += (n - OVERLAP) if n == CHUNK else n
    log.info(f"  range [{lo:#010x},{hi:#010x}] done: "
             f"{sig_matches} signature matches, {len(found)} validated.")
    return found


# ---------------------------------------------------------------------------
# Accessor address cache
# ---------------------------------------------------------------------------

def _load_cache() -> list[int]:
    try:
        if _ACCESSOR_CACHE.exists():
            data = json.loads(_ACCESSOR_CACHE.read_text())
            return [int(x) for x in data.get(_cache_key(), [])]
    except Exception:
        pass
    return []


def _save_cache(addrs: list[int]) -> None:
    try:
        data = {}
        if _ACCESSOR_CACHE.exists():
            data = json.loads(_ACCESSOR_CACHE.read_text())
        data[_cache_key()] = addrs
        _ACCESSOR_CACHE.write_text(json.dumps(data))
    except Exception as e:
        log.debug(f"  accessor cache write failed: {e}")


_cache_key_val = "default"


def _cache_key() -> str:
    return _cache_key_val


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(ctx) -> None:
    global _cache_key_val
    _cache_key_val = ctx.game.key
    log.info("Mode: observe (accessor-based live detection)")

    seen_keys: set[int] = set()       # enc_keys already reported to Seen
    known_acc: set[int] = set()       # accessor addrs we track
    lock = threading.Lock()

    # ── Validate cached accessors (fast path) ────────────────────────────
    cached = _load_cache()
    if cached:
        log.info(f"  validating {len(cached)} cached accessor(s)…")
        for a in cached:
            if _pkm_via_accessor(ctx, a) is not None:
                known_acc.add(a)
        log.info(f"  {len(known_acc)} cached accessor(s) still valid.")

    # ── Scan if cache empty / stale ──────────────────────────────────────
    if not known_acc:
        ranges = _accessor_scan_ranges(ctx.game.generation)
        log.info("  no valid cached accessors — scanning "
                 f"({len(ranges)} range(s)). First run is slow; the "
                 f"result is cached.")
        for lo, hi in ranges:
            if ctx.should_stop():
                return
            log.info(f"  scanning accessors in [{lo:#010x}, {hi:#010x})")
            hits = _scan_accessors(ctx, lo, hi)
            for a in hits:
                known_acc.add(a)
            if hits:
                log.info(f"  found {len(hits)} accessor(s) in this range; "
                         f"stopping range walk.")
                break
        if known_acc:
            _save_cache(sorted(known_acc))

    if not known_acc:
        log.error("No Pokémon accessors found. Make sure a save is "
                  "loaded and you're past the title screen, then retry.")
        return

    # Classify + initial report.
    _classify_and_report(ctx, known_acc, seen_keys, initial=True)

    # ── Watch loop ──────────────────────────────────────────────────────
    last_rescan = time.monotonic()
    loop_n = 0
    while not ctx.should_stop():
        loop_n += 1

        # Hot-poll every known accessor: deref → parse → detect changes.
        with lock:
            accs = list(known_acc)
        party_slots: list[dict] = []
        for a in accs:
            res = _pkm_via_accessor(ctx, a)
            if res is None:
                continue
            pkm, data_ptr, is_in_party = res
            if is_in_party:
                party_slots.append(_slot_dict(pkm, len(party_slots)))
            ek = pkm.encryption_key
            if ek not in seen_keys:
                seen_keys.add(ek)
                _report(ctx, pkm, data_ptr,
                         "party" if is_in_party else "wild")

        # Refresh the Party tab (replaces, never accumulates).
        if party_slots and loop_n % 5 == 0:
            ctx.dashboard.broadcast("party", slots=party_slots)

        # Periodic bounded rescan picks up NEW accessors (the wild foe's
        # accessor appears only when a battle starts).
        if time.monotonic() - last_rescan > _RESCAN_INTERVAL_S:
            last_rescan = time.monotonic()
            threading.Thread(
                target=_rescan_thread,
                args=(ctx, known_acc, seen_keys, lock),
                name="ObserveRescan", daemon=True).start()

        time.sleep(_HOT_POLL_INTERVAL_S)


def _accessor_scan_ranges(gen: int) -> list[tuple[int, int]]:
    """Heap ranges to scan for accessors, in priority order.

    Accessors are live runtime objects; for Gen 6 they sit in the
    linear heap (0x14000000+) where the game's working/battle
    allocations live — NOT the app heap, which only holds the stale
    save block. Linear heap first; app heap as a fallback.
    """
    if gen == 6:
        return [
            (0x14000000, 0x1C000000),   # linear heap (live runtime)
            (0x08000000, 0x10000000),   # app heap fallback
        ]
    # Gen 7: N3DS extended linear heap.
    return [(0x30000000, 0x40000000), (0x08000000, 0x10000000)]


def _rescan_thread(ctx, known_acc: set, seen_keys: set,
                   lock: threading.Lock) -> None:
    """Re-scan the primary heap range for accessors that appeared after
    startup (notably the wild-battle foe). Bounded; daemon thread.
    """
    ranges = _accessor_scan_ranges(ctx.game.generation)
    if not ranges:
        return
    lo, hi = ranges[0]
    hits = _scan_accessors(ctx, lo, hi)
    new = 0
    for a in hits:
        with lock:
            if a in known_acc:
                continue
            known_acc.add(a)
        new += 1
        res = _pkm_via_accessor(ctx, a)
        if res is None:
            continue
        pkm, data_ptr, is_in_party = res
        if pkm.encryption_key in seen_keys:
            continue
        seen_keys.add(pkm.encryption_key)
        _report(ctx, pkm, data_ptr, "party" if is_in_party else "wild")
    if new:
        log.info(f"  rescan: +{new} new accessor(s)")
        _save_cache(sorted(known_acc))


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _slot_dict(pkm, slot: int) -> dict:
    return {
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
    }


def _classify_and_report(ctx, known_acc: set, seen_keys: set,
                         initial: bool) -> None:
    party: list[dict] = []
    for a in sorted(known_acc):
        res = _pkm_via_accessor(ctx, a)
        if res is None:
            continue
        pkm, data_ptr, is_in_party = res
        tag = "party" if is_in_party else "wild"
        log.info(f"  accessor {a:#010x} → {data_ptr:#010x} "
                 f"#{pkm.species} {pkm.nickname!r} "
                 f"Lv{pkm.party['level'] if pkm.party else '?'} "
                 f"{'PARTY' if is_in_party else 'non-party'} "
                 f"shiny={pkm.shiny}")
        if is_in_party:
            party.append(_slot_dict(pkm, len(party)))
        seen_keys.add(pkm.encryption_key)
        if not is_in_party:
            _report(ctx, pkm, data_ptr, tag)
    if party:
        ctx.dashboard.broadcast("party", slots=party)
        log.info(f"  party tab populated with {len(party)} Pokémon.")


def _report(ctx, pkm, addr: int, source: str) -> None:
    lvl = pkm.party["level"] if pkm.party else "?"
    log.info(f"{source.upper()} @ {addr:#010x}: #{pkm.species} "
             f"{pkm.nickname or ''} Lv{lvl} "
             f"{'SHINY ' if pkm.shiny else ''}PID={pkm.pid:08X}")
    ctx.dashboard.broadcast(
        "encounter", source=source, address=f"{addr:#010x}",
        **encounter_payload(pkm))
    if ctx.target and ctx.target.matches(pkm):
        ctx.dashboard.broadcast(
            "target_hit", reason=ctx.target.describe(pkm),
            species=pkm.species, shiny=pkm.shiny,
            nature=pkm.nature, ivs=pkm.ivs)
        log.info(f"TARGET HIT: {ctx.target.describe(pkm)}")
