"""
Memory probe — reads specific RAM addresses out of Azahar and reports
what's there. Use this when the bot's heap scan finds nothing to
diagnose where the live save block actually lives.

Reads:
  - PKHeX-Plugins LiveHeX trainer_block / box1_slot1 addresses
  - Several candidate party_base addresses derived from those
  - A walk of the app-heap region in 256 KB steps to find non-zero
    runs (this is the "where is anything mapped at all?" sweep)

Run with Azahar open and your X/Y save loaded:

    python scripts/probe_memory.py

The output is verbose on purpose — copy it into the chat so we can
see exactly what the emulator's exposing.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running from any cwd
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pokebot.citra_rpc import wait_for_emulator
from pokebot.games import GAMES, find_game_by_title_id, party_base_candidates
from pokebot.livehex_compat import (
    livehex_version_for, get_trainer_block_offset,
    get_trainer_block_size, get_b1s1_offset, LiveHeXVersion,
)
from pokebot.find_offsets import is_likely_pk7


def hex_line(b: bytes, n: int = 32) -> str:
    return b[:n].hex(" ") if b else "(empty)"


def looks_utf16_ascii(b: bytes, max_chars: int = 16) -> str:
    """Best-effort decode of a UTF-16LE buffer if every char is ASCII printable."""
    chars: list[str] = []
    for i in range(0, len(b) - 1, 2):
        lo, hi = b[i], b[i + 1]
        if hi != 0:
            break
        if lo == 0:
            break
        if 0x20 <= lo <= 0x7E:
            chars.append(chr(lo))
        else:
            break
        if len(chars) >= max_chars:
            break
    return "".join(chars)


def main():
    print("Connecting to Azahar...")
    try:
        rpc = wait_for_emulator(timeout=10)
    except Exception as e:
        print(f"  RPC connect failed: {e}")
        return
    print("  RPC OK")

    # Attach + identify game
    try:
        pid, tid, name = rpc.attach_to_pokemon_game()
        print(f"  Attached PID={pid} TID={tid:#018x} ({name})")
    except Exception as e:
        print(f"  Attach failed: {e}")
        return

    g = find_game_by_title_id(tid)
    if not g:
        print(f"  Game TID {tid:#018x} not in registry; no LiveHeX mapping.")
        return
    print(f"  Game key: {g.key} ({g.title})")

    lv = livehex_version_for(g.key)
    print(f"  LiveHeX version mapping: {lv}")
    if lv == LiveHeXVersion.UNKNOWN:
        print("  No LiveHeX addresses to probe; aborting.")
        return

    # 1. Trainer block
    tb_addr = get_trainer_block_offset(lv)
    tb_size = get_trainer_block_size(lv)
    print()
    print(f"== Trainer block @ {tb_addr:#010x} (size {tb_size:#x}) ==")
    try:
        tb = rpc.read(tb_addr, min(tb_size, 0x80))
        print(f"  first 32 bytes: {hex_line(tb)}")
        if tb == b"\x00" * len(tb):
            print("  ALL ZEROS — address unmapped or empty in this session.")
        elif tb == b"\xFF" * len(tb):
            print("  ALL 0xFF — address unmapped (uninitialised RAM).")
        else:
            # Trainer card stores OT name as UTF-16LE at +0x48 typically.
            for ot_off in (0x48, 0x18, 0x68):
                name_str = looks_utf16_ascii(tb[ot_off : ot_off + 24])
                if len(name_str) >= 3:
                    print(f"  ASCII at +{ot_off:#x}: {name_str!r}")
                    break
    except Exception as e:
        print(f"  Read failed: {e}")

    # 2. Box 1 Slot 1
    b1_addr = get_b1s1_offset(lv)
    print()
    print(f"== Box 1 Slot 1 @ {b1_addr:#010x} (260 bytes) ==")
    try:
        raw = rpc.read(b1_addr, 260)
        print(f"  first 32 bytes: {hex_line(raw)}")
        if int.from_bytes(raw[:4], "little") == 0:
            print("  enc_key=0 (slot empty)")
        else:
            ok, info = is_likely_pk7(raw)
            print(f"  is_likely_pk7: {ok}, info: {info}")
    except Exception as e:
        print(f"  Read failed: {e}")

    # 3. Candidate party_base addresses
    print()
    print("== Candidate party_base addresses ==")
    candidates = party_base_candidates(g.key) or []
    for c in candidates:
        try:
            raw = rpc.read(c, 260)
            ek = int.from_bytes(raw[:4], "little")
            ok, info = is_likely_pk7(raw)
            print(f"  {c:#010x}: enc_key={ek:#010x} valid_PK6={ok} "
                  f"first8={raw[:8].hex()}")
            if ok and info:
                print(f"     -> species={info.get('species')} "
                      f"nature_id={info.get('nature')}")
        except Exception as e:
            print(f"  {c:#010x}: read failed: {e}")

    # 4. Brute walk of the app heap to find any non-zero region.
    print()
    print("== App-heap mapping sweep (0x08000000-0x09000000 in 1 MB probes) ==")
    for addr in range(0x08000000, 0x09000000, 0x100000):
        try:
            probe = rpc.read(addr, 0x40)
        except Exception as e:
            print(f"  {addr:#010x}: read failed: {e}")
            continue
        nonzero = sum(1 for b in probe if b != 0 and b != 0xFF)
        marker = "  data!" if nonzero > 8 else ""
        print(f"  {addr:#010x}: nonzero={nonzero:2d}/64  "
              f"first16={probe[:16].hex()}{marker}")

    # 5. A second, finer sweep around the LiveHeX trainer block specifically.
    #    If the data is here but probe-and-skip kept missing it because
    #    of zero padding, this finer sweep will catch it.
    print()
    print(f"== Fine sweep around {tb_addr:#010x} (±256 KB in 4 KB probes) ==")
    sweep_lo = (tb_addr & ~0xFFFF) - 0x40000
    sweep_hi = (tb_addr & ~0xFFFF) + 0x40000
    interesting: list[tuple[int, int]] = []
    for addr in range(sweep_lo, sweep_hi, 0x1000):
        try:
            probe = rpc.read(addr, 0x40)
        except Exception:
            continue
        nonzero = sum(1 for b in probe if b != 0 and b != 0xFF)
        if nonzero > 8:
            interesting.append((addr, nonzero))
    if not interesting:
        print(f"  no non-empty regions in [{sweep_lo:#x}, {sweep_hi:#x})")
    else:
        print(f"  {len(interesting)} non-empty 4 KB blocks; first 16:")
        for addr, nz in interesting[:16]:
            try:
                head = rpc.read(addr, 16)
                print(f"    {addr:#010x}: nonzero={nz:2d}/64 "
                      f"first16={head.hex()}")
            except Exception:
                pass


if __name__ == "__main__":
    main()
