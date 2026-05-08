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
from pathlib import Path

from .citra_rpc import CitraRPC, wait_for_emulator
from .games import HEAP_RANGE_3DS, EXT_HEAP_RANGE_N3DS
from .parser import decrypt_pkm, parse_pkm

log = logging.getLogger(__name__)


def derive_offsets_from_clusters(clusters: list[dict]) -> dict:
    """Pick the most likely party / foe addresses out of clustered hits.

    Returns a dict with any of ``party_base``, ``party_stride``, ``foe_base``
    that we could identify with high confidence.
    """
    out: dict = {}
    foe_candidates = []
    for c in clusters:
        n = len(c["members"])
        if 5 <= n <= 7 and c["stride"] and 480 <= c["stride"] <= 500:
            # Prefer the densest plausible-stride cluster as the party.
            if "party_base" not in out or n > out.get("_party_n", 0):
                out["party_base"]   = c["start"]
                out["party_stride"] = c["stride"]
                out["_party_n"]     = n
        elif n == 1 and c["stride"] is None:
            foe_candidates.append(c["members"][0][0])
    out.pop("_party_n", None)
    # If exactly one loner exists it's almost certainly the active foe slot.
    # When the scan happens on the overworld there's usually no foe at all,
    # so we stay quiet rather than guessing wrong.
    if len(foe_candidates) == 1:
        out["foe_base"] = foe_candidates[0]
    return out


def write_offsets_to_config(cfg_path: Path, offsets: dict) -> list[str]:
    """Write the discovered offsets in-place into config.yaml.

    Preserves comments, ordering, and indentation by doing a line-aware
    rewrite rather than going through PyYAML. Only keys already present
    under the ``offsets:`` block (or any top-level offset entry) are
    updated; new keys are appended at the end of the block.
    Returns the list of keys actually changed.
    """
    if not cfg_path.exists():
        return []
    text = cfg_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    pending = dict(offsets)
    written: list[str] = []
    in_offsets_block = False
    block_indent: str | None = None
    out: list[str] = []
    last_block_line_idx: int | None = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        # Detect the "offsets:" block header
        if stripped == "offsets:":
            in_offsets_block = True
            block_indent = None
            out.append(line)
            continue
        # Once in the block, infer the child indent from the first non-empty
        # non-comment line; bail out of the block on a less-indented line.
        if in_offsets_block:
            if stripped == "" or stripped.startswith("#"):
                out.append(line)
                continue
            indent = line[: len(line) - len(line.lstrip())]
            if block_indent is None:
                block_indent = indent
            if not indent.startswith(block_indent) or len(indent) < len(block_indent):
                # Block ended.
                in_offsets_block = False
                # Append any pending unknown keys before this line.
                for k, v in list(pending.items()):
                    out.append(f"{block_indent}{k}: {v:#010x}\n")
                    written.append(k)
                    pending.pop(k)
                out.append(line)
                continue
            # Replace value if this line's key is one we want to update.
            key = stripped.split(":", 1)[0].strip()
            if key in pending:
                v = pending.pop(key)
                out.append(f"{block_indent}{key}: {v:#010x}\n")
                written.append(key)
            else:
                out.append(line)
            last_block_line_idx = len(out) - 1
            continue
        out.append(line)

    # If the file ended while still inside the block, flush remaining keys.
    if in_offsets_block and pending:
        if block_indent is None:
            block_indent = "  "
        # Make sure the last block line ended with a newline.
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        for k, v in pending.items():
            out.append(f"{block_indent}{k}: {v:#010x}\n")
            written.append(k)

    cfg_path.write_text("".join(out), encoding="utf-8")
    return written


def is_likely_pk7(raw: bytes) -> tuple[bool, dict | None]:
    """Quick filter then strict checksum verify, plus field-level sanity.

    The basic checksum + species check has a non-trivial false-positive rate
    (~1/(65536*65536*65)) which adds up across a 64M-position scan. The extra
    field-level checks below — nature, ability_num, level, IV bytes — bring
    the FP rate effectively to zero.
    """
    if len(raw) < 232:
        return False, None
    # Cheap rejects on plaintext bytes:
    enc_key = int.from_bytes(raw[:4], "little")
    if enc_key == 0 or enc_key == 0xFFFFFFFF:
        return False, None
    sanity = int.from_bytes(raw[4:6], "little")
    if sanity != 0:               # almost always 0 in legitimate records
        return False, None
    species = None
    try:
        is_party = (len(raw) >= 260)
        pt = decrypt_pkm(raw[:260] if is_party else raw[:232] + b"\x00" * 28)
        from .parser import calc_checksum
        stored = int.from_bytes(pt[6:8], "little")
        if calc_checksum(pt) != stored:
            return False, None
        species = int.from_bytes(pt[8:10], "little")
        if species == 0 or species > 1000:
            return False, None

        # ---- field-level sanity (filters checksum-coincidence FPs) ----
        # Nature lives at offset 0x1C and must be 0..24.
        nature = pt[0x1C]
        if nature > 24:
            return False, None
        # Ability number at 0x15 must be one of {0, 1, 2, 4}.
        ability_num = pt[0x15]
        if ability_num not in (0, 1, 2, 4):
            return False, None
        # IV block at 0x74 (u32) packs six 5-bit IVs (0..31). All values
        # 0..31 fit in 5 bits, so we just check the high two bits of the
        # u32 don't claim a 6th 5-bit slot beyond the first 30 bits.
        # (No-op for valid records but rejects garbage.)
        iv32 = int.from_bytes(pt[0x74:0x78], "little")
        if (iv32 >> 30) > 3:
            return False, None
        # For party records, current level at 0xEC must be 1..100.
        if is_party:
            level = pt[0xEC]
            if level == 0 or level > 100:
                return False, None
    except Exception:
        return False, None
    return True, {"enc_key": enc_key, "species": species,
                  "nature": nature, "ability_num": ability_num}


def scan(rpc: CitraRPC,
         start: int = EXT_HEAP_RANGE_N3DS[0],
         end:   int = EXT_HEAP_RANGE_N3DS[1],
         step:  int = 4,
         chunk: int = 0x4000,         # 16 KB — smaller per-read pressure
         probe_size: int = 256,
         skip_unit:  int = 0x100000,
         throttle_s: float = 0.005):
    """Yield (address, info) for every candidate PK7 found.

    Probe-and-skip + throttle: before chunked scanning a 1 MB block,
    read a tiny probe at the start. If the probe is all 0x00 or all
    0xFF (unmapped 3DS memory), skip the whole 1 MB block. Otherwise
    chunk-scan it in 16 KB increments with a small sleep between each
    chunk so Azahar's logging subsystem can flush.

    The chunk size and throttle are deliberately conservative — we'd
    rather take ~2x as long as crash the emulator. Earlier versions
    used 64 KB chunks with no throttle and would flood Azahar's log
    with thousands of unmapped-read Errors per second, eventually
    crashing it.
    """
    cur = start
    last_progress = time.monotonic()
    while cur < end:
        # Step 1: probe this 1 MB region. If it looks unmapped, skip it.
        try:
            probe = rpc.read(cur, probe_size)
        except Exception:
            cur += skip_unit
            continue
        if not probe or probe == b"\x00" * len(probe) \
                     or probe == b"\xFF" * len(probe):
            cur += skip_unit
            continue

        # Step 2: scan this 1 MB region in `chunk` increments.
        region_end = min(cur + skip_unit, end)
        while cur < region_end:
            try:
                block = rpc.read(cur, chunk)
            except Exception:
                cur += chunk
                continue
            if len(block) < 260:
                cur += chunk
                continue
            for off in range(0, len(block) - 260 + 1, step):
                ok, info = is_likely_pk7(block[off:off+260])
                if ok:
                    yield (cur + off, info)
            cur += chunk - 256  # slight overlap on boundaries
            if throttle_s:
                time.sleep(throttle_s)

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
    ap.add_argument("--save-config", default=None,
                    help="path to config.yaml; discovered offsets are "
                         "written there automatically when set")
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

    discovered = derive_offsets_from_clusters(clusters)
    if not discovered:
        log.info("\nCouldn't auto-identify offsets. Try walking around in "
                 "the overworld to refresh the party block, then re-scan.")
        return

    if args.save_config:
        cfg_path = Path(args.save_config)
        try:
            written = write_offsets_to_config(cfg_path, discovered)
            if written:
                log.info(f"\nWROTE_OFFSETS: {cfg_path} updated keys=[{','.join(written)}]")
                for k in written:
                    log.info(f"  {k} = {discovered[k]:#010x}")
            else:
                log.info(f"\nNothing changed in {cfg_path}.")
        except Exception as e:
            log.error(f"Failed to write offsets to {cfg_path}: {e}")
    else:
        log.info("\nDiscovered offsets (paste under `offsets:` in config.yaml,"
                 " or rerun with --save-config to do it automatically):")
        for k, v in discovered.items():
            log.info(f"  {k}: {v:#010x}")


if __name__ == "__main__":
    sys.exit(main())
