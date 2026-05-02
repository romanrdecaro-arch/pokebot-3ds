"""
Offset finder.

Brute-force scans the running game's heap for valid PK7 records.

How it works:
  1. We sweep the FCRAM-mapped heap range in 4-byte-aligned steps.
  2. At each candidate address, we read 260 bytes.
  3. We try to decrypt and verify the checksum. A random buffer has a
     1-in-65536 chance of passing -- so a single match is already strong
     evidence; multiple hits at consistent stride is conclusive.
  4. Hits are clustered: if we find 6 consecutive valid records spaced
     by the same stride (typically 484 bytes), that's the player's
     party. Other hits are likely the box (30/box × 32 boxes contiguous)
     or the foe slot.

Run as:
    python -m pokebot.find_offsets

Optional args:
    --start 0x30000000  --end 0x40000000  --step 4

Tip: have at least one Pokémon in your party and run this on the
overworld for cleanest results. Walking around a bit first helps
because it forces the game to rewrite the party block to a fresh
location if there's any pooling.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from .citra_rpc import CitraRPC, wait_for_emulator
from .games import HEAP_RANGE_3DS, EXT_HEAP_RANGE_N3DS
from .parser import decrypt_pkm, parse_pkm

log = logging.getLogger(__name__)


def is_likely_pk7(raw: bytes) -> tuple[bool, dict | None]:
    """Quick filter then strict checksum verify."""
    if len(raw) < 232:
        return False, None
    # Cheap rejects:
    enc_key = int.from_bytes(raw[:4], "little")
    if enc_key == 0 or enc_key == 0xFFFFFFFF:
        return False, None
    sanity = int.from_bytes(raw[4:6], "little")
    if sanity != 0:               # almost always 0 in legitimate records
        return False, None
    species = None
    try:
        pt = decrypt_pkm(raw[:260] if len(raw) >= 260 else raw[:232] + b"\x00"*28)
        # checksum check
        from .parser import calc_checksum
        stored = int.from_bytes(pt[6:8], "little")
        if calc_checksum(pt) != stored:
            return False, None
        # extra plausibility: species in 1..1000 (Gen 7 caps at ~807)
        species = int.from_bytes(pt[8:10], "little")
        if species == 0 or species > 1000:
            return False, None
    except Exception:
        return False, None
    return True, {"enc_key": enc_key, "species": species}


def scan(rpc: CitraRPC,
         start: int = EXT_HEAP_RANGE_N3DS[0],
         end:   int = EXT_HEAP_RANGE_N3DS[1],
         step:  int = 4,
         chunk: int = 0x10000):
    """Yield (address, info) for every candidate PK7 found."""
    cur = start
    last_progress = time.monotonic()
    while cur < end:
        try:
            block = rpc.read(cur, chunk)
        except Exception as e:
            log.debug(f"read failed at {cur:#x}: {e}")
            cur += chunk
            continue
        # The read may return fewer bytes if the address range straddles
        # an unmapped region; chunk down if too short.
        if len(block) < 260:
            cur += chunk
            continue
        for off in range(0, len(block) - 260 + 1, step):
            ok, info = is_likely_pk7(block[off:off+260])
            if ok:
                yield (cur + off, info)
        cur += chunk - 256  # slight overlap so we don't miss boundary cases
        if time.monotonic() - last_progress > 2.0:
            pct = 100 * (cur - start) / (end - start)
            log.info(f"scan progress: {cur:#010x} ({pct:.1f}%)")
            last_progress = time.monotonic()


def cluster_hits(hits: list[tuple[int, dict]]) -> list[dict]:
    """Group consecutive hits with the same address stride."""
    if not hits:
        return []
    hits = sorted(hits, key=lambda h: h[0])
    clusters: list[dict] = []
    current = {"start": hits[0][0], "stride": None,
               "members": [hits[0]]}
    for prev, this in zip(hits, hits[1:]):
        delta = this[0] - prev[0]
        # tolerate 232..512 byte strides (party 484, box 232, varies)
        if 200 <= delta <= 600:
            if current["stride"] is None:
                current["stride"] = delta
                current["members"].append(this)
            elif abs(delta - current["stride"]) <= 4:
                current["members"].append(this)
            else:
                clusters.append(current)
                current = {"start": this[0], "stride": None, "members": [this]}
        else:
            clusters.append(current)
            current = {"start": this[0], "stride": None, "members": [this]}
    clusters.append(current)
    return clusters


def main(argv=None):
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    ap = argparse.ArgumentParser(description="Find PK7 records in Azahar memory")
    ap.add_argument("--start", type=lambda s: int(s, 0),
                    default=EXT_HEAP_RANGE_N3DS[0])
    ap.add_argument("--end",   type=lambda s: int(s, 0),
                    default=EXT_HEAP_RANGE_N3DS[1])
    ap.add_argument("--step",  type=int, default=4)
    ap.add_argument("--full-heap", action="store_true",
                    help="scan whole 3DS heap (slow)")
    args = ap.parse_args(argv)

    if args.full_heap:
        args.start, args.end = HEAP_RANGE_3DS

    rpc = wait_for_emulator()
    rpc.attach_to_pokemon_game()
    log.info(f"Scanning {args.start:#010x} - {args.end:#010x} step {args.step}")

    hits = list(scan(rpc, args.start, args.end, step=args.step))
    if not hits:
        log.info("No candidate PK7 records found. Try a different region "
                 "or step size, or make sure your party has at least one "
                 "Pokémon and you're on the overworld.")
        return

    log.info(f"{len(hits)} candidate records found.")
    clusters = cluster_hits(hits)
    for c in clusters:
        if len(c["members"]) >= 2:
            log.info(f"=== cluster: start={c['start']:#010x} "
                     f"stride={c['stride']} members={len(c['members'])} ===")
            for addr, info in c["members"][:8]:
                log.info(f"  {addr:#010x}  species={info['species']:>4}  "
                         f"key={info['enc_key']:#010x}")
            if len(c["members"]) > 8:
                log.info(f"  ... +{len(c['members']) - 8} more")
        else:
            addr, info = c["members"][0]
            log.info(f"  loner: {addr:#010x}  species={info['species']:>4}  "
                     f"key={info['enc_key']:#010x}")

    log.info("\nLikely candidates:")
    for c in clusters:
        n = len(c["members"])
        if 5 <= n <= 7 and c["stride"] and 480 <= c["stride"] <= 500:
            log.info(f"  PARTY: party_base = {c['start']:#010x}  "
                     f"party_stride = {c['stride']}  ({n} slots)")
        elif n == 1 and c["stride"] is None:
            log.info(f"  Maybe foe slot: foe_base = {c['members'][0][0]:#010x}")
        elif n >= 25:
            log.info(f"  PC BOX area: starts at {c['start']:#010x} "
                     f"({n} slots, stride {c['stride']})")


if __name__ == "__main__":
    sys.exit(main())
