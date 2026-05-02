"""
Dialog-flag finder — interactive REPL.

Locates the memory byte that flips when a dialog box / menu /
cutscene is on screen in the running game. Works by snapshot
diffing: take memory captures with the dialog ON vs. OFF, find
bytes that consistently match within each group but differ across
groups.

Robustness features
-------------------
- Adds a refine-by-extra-snapshot loop: if the first pair leaves too
  many candidates, take more pairs until the candidate set narrows
  to a confident pick.
- Live verification: poll the top candidates in real time so the
  user can toggle dialog state in-game and visually confirm which
  byte tracks the dialog state.
- Auto-widen on zero matches: if the scan range produces no
  candidates, the user can opt to retry with a larger range without
  exiting the tool.
- Menu-driven REPL after each diff pass: refine / verify / widen /
  save / quit.

Run:
    python -m pokebot.find_dialog_flag
    python -m pokebot.find_dialog_flag --save-config config.yaml
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .citra_rpc import wait_for_emulator, CitraRPC
from .find_offsets import write_offsets_to_config

log = logging.getLogger(__name__)


# Default 16 MB window — quick to scan (~30s per snapshot) and
# covers the bulk of Gen 6/7 working memory. Widen via the menu
# if no candidates surface.
DEFAULT_START = 0x33000000
DEFAULT_END   = 0x34000000

# Wider fallback when the default window yields nothing.
WIDE_START = 0x30000000
WIDE_END   = 0x40000000


# ---------------------------------------------------------------------------
# Memory snapshot
# ---------------------------------------------------------------------------

def take_snapshot(rpc: CitraRPC, start: int, end: int,
                  chunk: int = 1024) -> bytes:
    """Read [start, end) from emulator memory. Failed regions become 0xFF.

    Prints periodic progress updates on the same line.
    """
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


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

def diff_snapshots(snaps_off: list[bytes],
                   snaps_on:  list[bytes]) -> list[tuple]:
    """Find candidate dialog-flag offsets.

    Returns ``(offset, off_value, on_value, score)`` tuples sorted by
    descending score. ``offset`` is relative to the snapshot start.
    """
    if not snaps_off or not snaps_on:
        return []
    n = min(len(s) for s in snaps_off + snaps_on)
    off_views = [memoryview(s) for s in snaps_off]
    on_views  = [memoryview(s) for s in snaps_on]
    candidates: list[tuple] = []
    for i in range(n):
        off_v = off_views[0][i]
        if any(v[i] != off_v for v in off_views):
            continue
        on_v = on_views[0][i]
        if any(v[i] != on_v for v in on_views):
            continue
        if off_v == on_v:
            continue
        score = 0
        if off_v == 0 and 1 <= on_v <= 7:
            score += 20    # Strongest pattern: bool/enum, 0 = inactive
        elif on_v == 0 and 1 <= off_v <= 7:
            score += 18
        elif off_v in (0, 0xFF) and on_v in (0, 0xFF):
            score += 8
        else:
            score += 1
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


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def _prompt(text: str) -> str:
    try:
        return input(text)
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)


def _print_candidates(cands: list[tuple], scan_start: int,
                      n: int = 15) -> None:
    print(f"  Rank  Address       Off    On     Notes")
    print(f"  ----  ----------    -----  -----  --------------------------")
    for rank, (off, ov, nv, _score) in enumerate(cands[:n], 1):
        addr = scan_start + off
        print(f"  {rank:>4}  {addr:#010x}    "
              f"0x{ov:02X}   0x{nv:02X}   {_annotate(ov, nv)}")


# ---------------------------------------------------------------------------
# Snapshot orchestration
# ---------------------------------------------------------------------------

@dataclass
class SnapState:
    rpc: CitraRPC
    start: int
    end:   int
    snaps_off: list[bytes] = field(default_factory=list)
    snaps_on:  list[bytes] = field(default_factory=list)

    @property
    def span_mb(self) -> float:
        return (self.end - self.start) / (1024 * 1024)

    def reset(self) -> None:
        self.snaps_off.clear()
        self.snaps_on.clear()

    def collect_pair(self, label: str = "") -> None:
        """Take one OFF + one ON snapshot, with user prompts."""
        suffix = f" ({label})" if label else ""
        plan = [
            ("OFF",
             "Stand on the overworld with NO text box on screen.\n"
             "        Walk a step or two so the player isn't talking to anyone."),
            ("ON",
             "Talk to an NPC, read a sign, or open the start menu (X) —\n"
             "        anything that puts a text box / menu on screen."),
        ]
        for state, instr in plan:
            print(f"  Snapshot — Dialog {state}{suffix}")
            print(f"        {instr}")
            _prompt("    Press Enter when the game is in the right state...")
            print("    Reading memory...")
            t0 = time.monotonic()
            snap = take_snapshot(self.rpc, self.start, self.end)
            print(f"    Done in {time.monotonic() - t0:.1f}s "
                  f"({len(snap):,} B captured)")
            print()
            (self.snaps_off if state == "OFF" else self.snaps_on).append(snap)

    def widen(self, new_start: int, new_end: int) -> None:
        self.start = new_start
        self.end = new_end
        self.reset()


def verify_live(rpc: CitraRPC, candidates: list[tuple],
                scan_start: int, top_n: int = 10) -> None:
    """Real-time polling of the top candidates so the user can confirm
    visually which byte tracks the dialog state.
    """
    if not candidates:
        return
    cands = candidates[:top_n]
    print()
    print("Live verification — poll top candidates in real time.")
    print("Toggle dialog state in your game; watch which 'current'")
    print("column matches OFF when no dialog and ON when a dialog box")
    print("is visible.")
    print("Press Ctrl+C to stop.")
    print()
    print(f"  Rank  Address       Off    On     Current  Match")
    print(f"  ----  ----------    -----  -----  -------  ----------")
    rows = len(cands)
    # Reserve `rows` lines below the header that we'll keep updating.
    for _ in range(rows):
        print()
    try:
        while True:
            # Move cursor up `rows` lines, redraw each row in place.
            print(f"\033[{rows}A", end="")
            for rank, (off, ov, nv, _score) in enumerate(cands, 1):
                addr = scan_start + off
                try:
                    cur = rpc.read(addr, 1)[0]
                    cur_str = f"0x{cur:02X}"
                except Exception:
                    cur, cur_str = -1, "    ?"
                if cur == nv:
                    match = "ON  ✓"
                elif cur == ov:
                    match = "OFF ✓"
                else:
                    match = f"      "
                # Clear the line then redraw.
                print(f"\033[2K  {rank:>4}  {addr:#010x}    "
                      f"0x{ov:02X}   0x{nv:02X}   {cur_str}    {match}")
            time.sleep(0.25)
    except KeyboardInterrupt:
        print()
        print("Verification stopped.")


# ---------------------------------------------------------------------------
# Menu loop
# ---------------------------------------------------------------------------

def _menu_choice() -> str:
    print()
    print("What next?")
    print("  [r] Refine — take another OFF/ON pair to narrow the list")
    print("  [v] Verify — live-poll the top candidates while you toggle")
    print("              dialog in-game (Ctrl+C to stop)")
    print("  [w] Widen  — expand the scan range and start over")
    print("  [s] Save   — save the top candidate to config.yaml")
    print("  [q] Quit")
    return _prompt("> ").strip().lower()


def _save_top(cfg_path: Path, addr: int) -> None:
    written = write_offsets_to_config(cfg_path, {"dialog_flag": addr})
    if "dialog_flag" in written:
        print(f"Saved offsets.dialog_flag = {addr:#010x} → {cfg_path}")
    else:
        print(f"Wrote (or appended) dialog_flag in {cfg_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    ap = argparse.ArgumentParser(
        description="Find the in-dialog flag in the running game's memory")
    ap.add_argument("--start", type=lambda s: int(s, 0), default=DEFAULT_START,
                    help="memory range start (default 0x33000000)")
    ap.add_argument("--end", type=lambda s: int(s, 0), default=DEFAULT_END,
                    help="memory range end (default 0x34000000)")
    ap.add_argument("--max-results", type=int, default=15)
    ap.add_argument("--save-config", default=None,
                    help="path to config.yaml; saves the top candidate as "
                         "offsets.dialog_flag when chosen from the menu")
    args = ap.parse_args(argv)

    print("-" * 64)
    print(" Dialog-flag finder")
    print("-" * 64)
    print()
    print("Take memory snapshots while toggling dialog ON/OFF in your")
    print("game. We compare them to find the byte that flips with the")
    print("dialog state. After each round you can refine, verify live,")
    print("widen the scan, or save the top pick.")
    print()
    _prompt("[Press Enter to connect to Azahar]")
    print()

    log.info("Connecting to Azahar...")
    rpc = wait_for_emulator()
    pid, tid, name = rpc.attach_to_pokemon_game()
    log.info(f"Attached to PID {pid} / TID {tid:#018x} / {name!r}")
    print()

    state = SnapState(rpc=rpc, start=args.start, end=args.end)
    print(f"Range: {state.start:#010x} → {state.end:#010x}  "
          f"({state.span_mb:.1f} MB)")
    print()

    # Initial round — collect TWO OFF/ON pairs (4 snapshots) to start.
    print("[Round 1 — initial 2 pairs]")
    state.collect_pair("pair 1")
    state.collect_pair("pair 2")

    while True:
        print(f"Analyzing {len(state.snaps_off)} OFF + "
              f"{len(state.snaps_on)} ON snapshots...")
        cands = diff_snapshots(state.snaps_off, state.snaps_on)
        print(f"Found {len(cands)} candidate offset(s).")
        print()

        if not cands:
            print("No candidates with the current data.")
            if state.start == DEFAULT_START and state.end == DEFAULT_END:
                print("The default 16 MB scan window may not cover the right")
                print(f"region for your game. Try widening to "
                      f"{WIDE_START:#x}–{WIDE_END:#x} (~256 MB, ~4 min/snapshot).")
            ans = _prompt("Widen scan and retry? [y/N] ").strip().lower()
            if ans == "y":
                state.widen(WIDE_START, WIDE_END)
                print(f"Widened to {state.start:#x}–{state.end:#x}.")
                state.collect_pair("pair 1")
                state.collect_pair("pair 2")
                continue
            print("Try toggling dialog more cleanly and re-running.")
            return 1

        _print_candidates(cands, state.start, args.max_results)
        if len(cands) > args.max_results:
            print(f"  ... + {len(cands) - args.max_results} more "
                  "(too many — refine to narrow the list)")

        choice = _menu_choice()

        if choice in ("q", "quit", "exit"):
            print("Quitting without saving.")
            return 0
        elif choice in ("r", "refine"):
            print()
            print(f"[Round — refining; current: {len(cands)} candidates]")
            state.collect_pair(f"pair {len(state.snaps_off) + 1}")
        elif choice in ("v", "verify"):
            verify_live(rpc, cands, state.start)
        elif choice in ("w", "widen"):
            new_end_default = min(state.end * 2, WIDE_END)
            print(f"Current range: {state.start:#x} – {state.end:#x}")
            new_start = _prompt(f"  New start [{state.start:#x}]: ").strip()
            new_end   = _prompt(f"  New end   [{new_end_default:#x}]: ").strip()
            try:
                ns = int(new_start, 0) if new_start else state.start
                ne = int(new_end,   0) if new_end   else new_end_default
            except ValueError:
                print("Couldn't parse those — keeping current range.")
                continue
            if ne <= ns:
                print("Range invalid; keeping current.")
                continue
            state.widen(ns, ne)
            print(f"Widened to {state.start:#x} – {state.end:#x} "
                  f"({state.span_mb:.1f} MB).")
            state.collect_pair("pair 1")
            state.collect_pair("pair 2")
        elif choice in ("s", "save"):
            top_addr = state.start + cands[0][0]
            cfg = args.save_config or _prompt(
                "  Path to config.yaml [config.yaml]: ").strip() or "config.yaml"
            cfg_path = Path(cfg)
            if not cfg_path.exists():
                print(f"Config not found: {cfg_path}")
                continue
            try:
                _save_top(cfg_path, top_addr)
            except Exception as e:
                print(f"Save failed: {e}")
                continue
            return 0
        else:
            print(f"Unknown option: {choice!r}")


if __name__ == "__main__":
    sys.exit(main())
