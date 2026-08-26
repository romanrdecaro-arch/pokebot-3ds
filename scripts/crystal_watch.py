"""
Watch a running Pokemon Crystal session and evaluate shininess live.

Passive: it reads memory and prints, and never sends input. Run it
while you play toward the Celebi event — it checks every Pokemon in
your party, so it exercises the whole stack (locate WRAM, parse the
Gen 2 records, evaluate DVs) long before Celebi is reachable, and will
tell you if anything you catch on the way happens to be shiny.

    python scripts/crystal_watch.py
    python scripts/crystal_watch.py --once        # one reading, then exit
    python scripts/crystal_watch.py --rescan      # ignore the cached base

Gen 2 shininess is DVs alone: Defense, Speed and Special all 10, and
Attack in {2,3,6,7,10,11,14,15} — 1 in 8192.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pokebot.citra_rpc import CitraRPC, wait_for_emulator      # noqa: E402
from pokebot.crystal import (CrystalSession, locate_wram,       # noqa: E402
                             BATTLE_NONE)
from pokebot.games import (GB_VC_SCAN_RANGES,  # noqa: E402
                           EXT_HEAP_RANGE_N3DS)

log = logging.getLogger("crystal_watch")

_DV_ORDER = ("HP", "Atk", "Def", "Spe", "Spc")


def dv_line(dvs: dict) -> str:
    return "/".join(f"{dvs.get(k, 0):2d}" for k in _DV_ORDER)


def describe(p) -> str:
    star = "  *** SHINY ***" if p.shiny else ""
    return (f"#{p.species:<3} Lv{p.level:<3} "
            f"DVs {dv_line(p.dvs)}  (HP/Atk/Def/Spe/Spc){star}")


def report(party: list, enemy) -> bool:
    """Print one reading. Returns True if anything shiny was seen."""
    shiny_seen = False
    if not party:
        print("  party: unreadable (is a save loaded with a Pokemon in it?)")
    for i, p in enumerate(party, 1):
        print(f"  slot {i}: {describe(p)}")
        shiny_seen |= p.shiny
    if enemy is not None:
        print(f"  enemy: #{enemy.species:<3} Lv{enemy.level:<3} "
              f"DVs {dv_line(enemy.dvs)}"
              f"{'  *** SHINY? ***' if enemy.shiny else ''}")
        print("         ^ PROVISIONAL: the in-battle struct's DV offsets")
        print("           are unverified. Catch it and compare the party")
        print("           reading above to confirm them.")
    return shiny_seen


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--interval", type=float, default=3.0,
                    help="seconds between readings (default 3)")
    ap.add_argument("--once", action="store_true",
                    help="take a single reading and exit")
    ap.add_argument("--rescan", action="store_true",
                    help="ignore the cached WRAM base and scan again")
    ap.add_argument("--extended", action="store_true",
                    help="also scan the New 3DS extended heap")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    kwargs = {"host": args.host}
    if args.port:
        kwargs["port"] = args.port
    wait_for_emulator(**kwargs)
    rpc = CitraRPC(**kwargs)

    ranges = list(GB_VC_SCAN_RANGES)
    if args.extended:
        ranges.append(EXT_HEAP_RANGE_N3DS)

    base = locate_wram(rpc, ranges, use_cache=not args.rescan)
    if base is None:
        print()
        print("Could not find Crystal's WRAM.")
        print("Check that the Crystal Virtual Console title is running and")
        print("that your save has at least one Pokemon in the party.")
        print("If it still fails, the VC emulator may not keep WRAM")
        print("contiguous — tell me and I'll try a different signature.")
        rpc.close()
        return 1

    session = CrystalSession(rpc, ranges, base=base)
    print(f"\nWRAM base {base:#010x} — watching (Ctrl+C to stop)\n")

    seen_shiny = False
    try:
        while True:
            stamp = time.strftime("%H:%M:%S")
            mode = session.battle_mode()
            state = {0: "overworld", 1: "wild battle",
                     2: "trainer battle"}.get(mode, f"mode {mode}")
            print(f"[{stamp}] {state}")
            enemy = session.enemy() if mode != BATTLE_NONE else None
            if report(session.party(), enemy) and not seen_shiny:
                seen_shiny = True
                print("\n  !!! A SHINY IS IN YOUR PARTY !!!\n")
            print()
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("stopped")
    finally:
        rpc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
