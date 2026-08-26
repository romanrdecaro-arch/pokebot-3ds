"""
Find where the opponent's DVs live in Crystal's battle RAM.

The party structure is documented and verified; the in-battle opponent
structure is not, and its DV offset is the last thing standing between
this bot and a Celebi soft-reset hunt. Guessing from a RAM map is how
you end up reading a byte that merely looks plausible.

So this derives it from ground truth instead:

  1. Wait for a wild battle, then snapshot the whole of WRAM.
  2. Wait for you to CATCH that Pokemon.
  3. Read its real DVs out of the party block, which is verified.
  4. Search the snapshot for that exact DV word and report every
     offset it appears at.

Whichever offset shows up in the battle region IS the opponent's DV
address — proven against a known value rather than assumed.

    python scripts/find_enemy_dvs.py

Run it, walk into a wild battle, and catch the Pokemon. Fleeing or
knocking it out gives nothing to correlate against, so it will just
wait for the next one.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pokebot import gen2                                        # noqa: E402
from pokebot.citra_rpc import CitraRPC, wait_for_emulator      # noqa: E402
from pokebot.crystal import (CrystalSession, locate_wram,       # noqa: E402
                             BATTLE_WILD, GB_BATTLE_MODE)
from pokebot.games import HEAP_RANGE_3DS, EXT_HEAP_RANGE_N3DS  # noqa: E402

log = logging.getLogger("find_enemy_dvs")

WRAM_SPAN = 0x8000          # generous: covers banked WRAM too


def dv_word(dvs: dict) -> int:
    return ((dvs["Atk"] << 12) | (dvs["Def"] << 8)
            | (dvs["Spe"] << 4) | dvs["Spc"])


def party_key(p) -> tuple:
    return (p.species, p.ot_id, p.exp, dv_word(p.dvs))


def find_all(buf: bytes, word: int) -> list[int]:
    """Every GB address at which this DV word appears."""
    pat = word.to_bytes(2, "big")
    out, start = [], 0
    while True:
        i = buf.find(pat, start)
        if i < 0:
            return out
        out.append(gen2.GB_WRAM_LO + i)
        start = i + 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--poll", type=float, default=0.5)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    kwargs = {"host": args.host}
    if args.port:
        kwargs["port"] = args.port
    wait_for_emulator(**kwargs)
    rpc = CitraRPC(**kwargs)

    ranges = [HEAP_RANGE_3DS, EXT_HEAP_RANGE_N3DS]
    base = locate_wram(rpc, ranges)
    if base is None:
        print("Could not find Crystal's WRAM.")
        return 1
    session = CrystalSession(rpc, ranges, base=base)

    def snapshot() -> bytes:
        return rpc.read(base, WRAM_SPAN)

    print(f"WRAM base {base:#010x}")
    print("Waiting for a wild battle — go and find one. Ctrl+C to stop.\n")

    before = {party_key(p) for p in session.party()}
    captured: bytes | None = None
    wild_note = ""

    try:
        while True:
            mode = rpc.read(base + GB_BATTLE_MODE - gen2.GB_WRAM_LO, 1)[0]

            if mode == BATTLE_WILD and captured is None:
                # Snapshot WHILE the opponent is on screen: once the
                # battle ends its structure is stale or reused.
                captured = snapshot()
                before = {party_key(p) for p in session.party()}
                wild_note = time.strftime("%H:%M:%S")
                print(f"[{wild_note}] wild battle — WRAM snapshot taken.")
                print("          now CATCH it (fleeing gives nothing to "
                      "correlate against).")

            if captured is not None:
                party = session.party()
                new = [p for p in party if party_key(p) not in before]
                if new:
                    caught = new[-1]
                    word = dv_word(caught.dvs)
                    print()
                    print(f"Caught #{caught.species} Lv{caught.level}")
                    print(f"  true DVs from the party block: {caught.dvs}")
                    print(f"  DV word: {word:04X}")
                    print()
                    hits = find_all(captured, word)
                    if not hits:
                        print("That DV word does not appear anywhere in the")
                        print("battle-time snapshot. Either the opponent's")
                        print("DVs are stored somewhere outside this window,")
                        print("or they are re-rolled on capture.")
                        return 2
                    print("The word appears in the battle-time snapshot at:")
                    for a in hits:
                        region = ("party block" if a >= gen2.GB_PARTY_MON1
                                  else "BATTLE REGION")
                        print(f"    {a:04X}   <- {region}")
                    battle_hits = [a for a in hits
                                   if a < gen2.GB_PARTY_MON1]
                    print()
                    if battle_hits:
                        print("Opponent DV address confirmed:")
                        for a in battle_hits:
                            print(f"    GB_ENEMY_DVS = 0x{a:04X}")
                        print()
                        print("Verified against a known value, not guessed.")
                    else:
                        print("Only party copies found — the opponent's DVs")
                        print("were not in the snapshot's battle region.")
                    return 0

            time.sleep(args.poll)
    except KeyboardInterrupt:
        print("\nstopped")
        return 130
    finally:
        rpc.close()


if __name__ == "__main__":
    raise SystemExit(main())
