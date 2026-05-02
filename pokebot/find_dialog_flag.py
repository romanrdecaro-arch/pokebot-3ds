"""
Dialog-flag finder.

Locates the memory byte that flips when a dialog box (or any
input-blocking UI state) is on screen in the running game. Once
known, the bot can poll this byte instead of guessing how many
A presses it takes to advance through dialog.

Methodology — snapshot diffing:
  1. User stands on the overworld with no UI showing.   → snap A
  2. User triggers a dialog (talks to an NPC).          → snap B
  3. User dismisses the dialog.                          → snap C
  4. User triggers a different dialog.                   → snap D

  We then find every byte whose value:
    - Was identical in A and C   ("dialog OFF" group)
    - Was identical in B and D   ("dialog ON"  group)
    - Differs between the two groups
  Those bytes are candidate dialog flags. Higher confidence is
  given to bytes whose ON value is a small enum (0..7).

Run:
    python -m pokebot.find_dialog_flag
    python -m pokebot.find_dialog_flag --save-config config.yaml

The default scan range is a 16 MB window starting at 0x33000000,
which covers the working FCRAM region for Gen 6/7 titles. Use
``--start`` and ``--end`` to widen it if no candidates are found.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from .citra_rpc import wait_for_emulator
from .find_offsets import write_offsets_to_config

log = logging.getLogger(__name__)


# Default 16 MB window — quick to scan (~30s per snapshot) and
# covers the bulk of Gen 6/7 working memory. Widen with --start/--end
# if no candidates surface.
DEFAULT_START = 0x33000000
DEFAULT_END   = 0x34000000


def take_snapshot(rpc, start: int, end: int, chunk: int = 1024) -> bytes:
    """Read [start, end) from emulator memory. Failed regions become 0xFF."""
    out = bytearray()
    cur = start
    last_progress = time.monotonic()
    total = end - start
    while cur < end:
        size = min(chunk, end - cur)
        try:
            data = rpc.read(cur, size)
            if len(data) < size:
                data = data + b"\xFF" * (size - len(data))
            out.extend(data)
        except Exception:
            out.extend(b"\xFF" * size)
        cur += size
        if time.monotonic() - last_progress > 1.5:
            pct = 100 * (cur - start) / total
            print(f"    {pct:5.1f}%   ({cur - start:>10,} / {total:>10,} B)",
                  end="\r", flush=True)
            last_progress = time.monotonic()
    print(" " * 60, end="\r", flush=True)
    return bytes(out)


def diff_snapshots(snaps_off: list[bytes],
                   snaps_on:  list[bytes]) -> list[tuple]:
    """Find candidate dialog-flag offsets.

    Returns ``(offset, off_value, on_value, score)`` tuples sorted by
    descending score.
    """
    n = len(snaps_off[0])
    candidates: list[tuple] = []
    # Cache snapshots as memoryviews for fast iteration.
    off_views = [memoryview(s) for s in snaps_off]
    on_views  = [memoryview(s) for s in snaps_on]
    for i in range(n):
        off_v = off_views[0][i]
        if any(v[i] != off_v for v in off_views):
            continue
        on_v = on_views[0][i]
        if any(v[i] != on_v for v in on_views):
            continue
        if off_v == on_v:
            continue
        # Score the candidate.
        score = 0
        if off_v == 0 and 1 <= on_v <= 7:
            score += 20    # Most common pattern: bool/enum, 0 = inactive
        elif on_v == 0 and 1 <= off_v <= 7:
            score += 18    # Inverse: 0 = active (rarer)
        elif off_v in (0, 0xFF) and on_v in (0, 0xFF):
            score += 8
        else:
            score += 1
        # Single-bit flips are slightly preferred.
        if (off_v ^ on_v).bit_count() == 1:
            score += 2
        candidates.append((i, int(off_v), int(on_v), score))
    candidates.sort(key=lambda c: (-c[3], c[0]))
    return candidates


def _annotate(off_v: int, on_v: int) -> str:
    if off_v == 0 and 1 <= on_v <= 7:
        return "likely bool / state enum"
    if on_v == 0 and 1 <= off_v <= 7:
        return "inverse bool"
    if (off_v ^ on_v).bit_count() == 1:
        return "single-bit flip"
    return ""


def _prompt(text: str) -> None:
    """Block until the user presses Enter (so they can position the game)."""
    try:
        input(text)
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    ap = argparse.ArgumentParser(
        description="Find the in-dialog flag in the running game's memory")
    ap.add_argument("--start", type=lambda s: int(s, 0), default=DEFAULT_START,
                    help="memory range start (default 0x33000000)")
    ap.add_argument("--end", type=lambda s: int(s, 0), default=DEFAULT_END,
                    help="memory range end (default 0x34000000)")
    ap.add_argument("--max-results", type=int, default=15,
                    help="how many candidates to print (default 15)")
    ap.add_argument("--save-config", default=None,
                    help="path to config.yaml; saves the top candidate as "
                         "offsets.dialog_flag if provided")
    args = ap.parse_args(argv)

    if args.end <= args.start:
        print("--end must be greater than --start", file=sys.stderr)
        return 2
    span_mb = (args.end - args.start) / (1024 * 1024)

    print("-" * 64)
    print(" Dialog-flag finder")
    print("-" * 64)
    print()
    print("This tool finds the memory byte that flips when a text box")
    print("appears in your game. We'll take 4 snapshots while you")
    print("alternate between dialog-OFF and dialog-ON states.")
    print()
    print(f"Range: {args.start:#010x} → {args.end:#010x}  ({span_mb:.1f} MB)")
    print()
    _prompt("[Press Enter to connect to Azahar]")
    print()

    log.info("Connecting to Azahar...")
    rpc = wait_for_emulator()
    pid, tid, name = rpc.attach_to_pokemon_game()
    log.info(f"Attached to PID {pid} / TID {tid:#018x} / {name!r}")
    print()

    plan = [
        ("OFF",
         "Stand on the overworld with NO text box on screen.\n"
         "        Walk a step or two so the player isn't talking to anyone."),
        ("ON",
         "Talk to an NPC, read a sign, or open the start menu (X)\n"
         "        — anything that puts a text box / menu on screen.\n"
         "        Keep it visible while the snapshot runs."),
        ("OFF",
         "Dismiss the text box (B or A until you're back on the overworld)."),
        ("ON",
         "Trigger a different dialog (or open the menu again).\n"
         "        Keep it visible while the snapshot runs."),
    ]

    snaps_off: list[bytes] = []
    snaps_on:  list[bytes] = []
    for i, (state, instr) in enumerate(plan, 1):
        print(f"[Snapshot {i}/4]  Dialog {state}")
        print(f"        {instr}")
        _prompt("  Press Enter when ready and the game is in the right state...")
        print("  Reading memory...")
        t0 = time.monotonic()
        snap = take_snapshot(rpc, args.start, args.end)
        print(f"  Done in {time.monotonic() - t0:.1f}s "
              f"({len(snap):,} B captured)")
        print()
        if state == "OFF":
            snaps_off.append(snap)
        else:
            snaps_on.append(snap)

    print("Analyzing diffs...")
    candidates = diff_snapshots(snaps_off, snaps_on)
    print(f"Found {len(candidates)} candidate offset(s).")
    print()
    if not candidates:
        print("No candidates. Try:")
        print("  - Re-running the snapshots more carefully (be sure dialog")
        print("    is fully visible during ON, and fully gone during OFF).")
        print(f"  - Widening the scan range, e.g. "
              f"--start 0x30000000 --end 0x40000000")
        return 1

    print(f"Top {min(args.max_results, len(candidates))} candidates:")
    print()
    print(f"  Rank  Address       Off    On     Notes")
    print(f"  ----  ----------    -----  -----  --------------------------")
    for rank, (offset, off_v, on_v, score) in enumerate(
            candidates[:args.max_results], 1):
        addr = args.start + offset
        print(f"  {rank:>4}  {addr:#010x}    "
              f"0x{off_v:02X}   0x{on_v:02X}   {_annotate(off_v, on_v)}")

    top_addr = args.start + candidates[0][0]
    print()
    print(f"Top pick: {top_addr:#010x}")

    if args.save_config:
        cfg_path = Path(args.save_config)
        if not cfg_path.exists():
            log.error(f"Config not found: {cfg_path}")
            return 1
        try:
            written = write_offsets_to_config(
                cfg_path, {"dialog_flag": top_addr})
            if "dialog_flag" in written:
                print(f"Saved offsets.dialog_flag = {top_addr:#010x} "
                      f"to {cfg_path}")
            else:
                print(f"Wrote (or appended) dialog_flag in {cfg_path}.")
        except Exception as e:
            log.error(f"Failed to save: {e}")
            return 1
    else:
        print()
        print("To save the top pick to your config automatically, re-run with:")
        print(f"  python -m pokebot.find_dialog_flag "
              f"--save-config config.yaml")

    return 0


if __name__ == "__main__":
    sys.exit(main())
