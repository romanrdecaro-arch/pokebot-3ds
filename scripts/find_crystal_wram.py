"""
Locate Pokemon Crystal's Game Boy WRAM inside Azahar's memory.

Crystal on 3DS is a Virtual Console title: a Game Boy emulator running
inside the 3DS process. Crystal's own addresses (party block at GB
0xDCD7) are therefore NOT 3DS addresses — the emulated WRAM sits at
some host address Nintendo's VC emulator chose, and that placement is
not publicly documented anywhere I could find.

So we find it the same way find_offsets locates the Gen 6 party: scan
the heap for a structure only the real thing produces. The signature
here is the party block, whose species list and per-record species
byte are two independent copies of the same data — garbage does not
agree on both, at a valid level, with sane HP.

    python scripts/find_crystal_wram.py
    python scripts/find_crystal_wram.py --start 0x30000000 --end 0x40000000

Prints the host address of the party block and the implied WRAM base,
so the mapping can be checked for stability across launches before
anything is wired into the bot.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pokebot.citra_rpc import CitraRPC, wait_for_emulator      # noqa: E402
from pokebot.games import HEAP_RANGE_3DS, EXT_HEAP_RANGE_N3DS  # noqa: E402
from pokebot.crystal import scan_range                          # noqa: E402

log = logging.getLogger("find_crystal_wram")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--start", default=None,
                    help="hex start address (default: 3DS heap)")
    ap.add_argument("--end", default=None, help="hex end address")
    ap.add_argument("--extended", action="store_true",
                    help="also scan the New 3DS extended heap")
    ap.add_argument("--first", type=int, default=0,
                    help="stop after N hits (0 = scan everything)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    kwargs = {"host": args.host}
    if args.port:
        kwargs["port"] = args.port
    wait_for_emulator(**kwargs)
    rpc = CitraRPC(**kwargs)

    try:
        rpc.attach_to_pokemon_game()
        log.info("Attached to a Gen 6/7 title — that is NOT Crystal.")
        log.info("Load the Crystal Virtual Console title instead.")
    except Exception:
        # Expected: Crystal VC is not in the Gen 6/7 title list, so the
        # bot's usual attach fails. Fall back to the running process.
        try:
            procs = rpc.list_processes()
            log.info(f"Processes visible: {len(procs)}")
            for pid, (tid, name) in list(procs.items())[:12]:
                log.info(f"   pid={pid:<6} tid={tid:#018x}  {name}")
        except Exception as exc:
            log.error(f"Could not list processes: {exc}")
            return 2

    ranges = []
    if args.start and args.end:
        ranges.append((int(args.start, 0), int(args.end, 0)))
    else:
        ranges.append(HEAP_RANGE_3DS)
        if args.extended:
            ranges.append(EXT_HEAP_RANGE_N3DS)

    all_hits: list[dict] = []
    for start, end in ranges:
        log.info(f"Scanning {start:#010x}-{end:#010x} …")
        all_hits += scan_range(rpc, start, end, stop_after=args.first)
        if args.first and len(all_hits) >= args.first:
            break

    rpc.close()

    print()
    if not all_hits:
        print("No Crystal party block found.")
        print("Have a party with at least one Pokemon, be on the overworld,")
        print("and make sure the Crystal VC title is actually running.")
        return 1

    print(f"Found {len(all_hits)} candidate(s):")
    for h in all_hits:
        print(f"  party block @ {h['party_addr']:#010x}   "
              f"implied WRAM base {h['wram_base']:#010x}")
    print()
    print("Run this again after restarting Azahar. If the WRAM base is")
    print("the same, the mapping is stable and can be wired in; if it")
    print("moves, the bot will have to re-scan per session the way")
    print("foe_base already does.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
