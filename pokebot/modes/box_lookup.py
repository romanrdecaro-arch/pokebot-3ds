"""
Find a caught Pokémon in the PC boxes.

A catch made with a full party goes straight to a box, where the party
scan cannot see it — so the target got saved as its PRE-CAPTURE record
and PKHeX rejected the file for having no owner. This locates it in
the boxes instead, so the legal copy can be exported either way.

Addressing is relative, not absolute. Azahar relocates the whole save
block, so the literal ``box1_slot1`` from the registry is not where the
boxes actually are — but their offset FROM the party is fixed by the
save layout, and the party is already located by content. In the
LiveHeX reference addresses box 1 slot 1 sits 0xC420 past party slot 0,
and 31 boxes of 30 slots span 0x34AD0 after that, so a window starting
before the first box and ending past the last one covers every slot
wherever the block landed.

The search is bounded on purpose. Azahar serves memory in 1 KB chunks
over UDP, and a sweep wide enough to "just find it anywhere" is what
crashed the emulator when this project was young — see
``crystal.scan_range`` for the same lesson learned the hard way.
"""
from __future__ import annotations

import logging

from .observe import _scan_owned

log = logging.getLogger(__name__)

# Offsets FROM the located party slot 0. Start before box 1 and end
# past box 31, both with margin, so a save layout that differs a little
# from the reference still falls inside.
BOX_SEARCH_START = 0x8000        # box 1 is ~0xC420 in
BOX_SEARCH_LEN = 0x48000         # last box ends ~0x40EF0 in

# Gen 6 PC storage, for the log line.
BOX_SLOT_SIZE = 232
SLOTS_PER_BOX = 30


def box_window(party_addr: int) -> tuple[int, int]:
    """The [lo, hi) worth scanning for box slots, given the party."""
    lo = max(0, party_addr + BOX_SEARCH_START)
    return lo, lo + BOX_SEARCH_LEN


def find_in_boxes(ctx, pid: int, species: int, player_ot: str,
                  party_addr: int):
    """The box record for ``pid``, or None.

    Returns a ParsedPokemon carrying ``source_address`` so the exporter
    can re-read the raw bytes, exactly as a party hit does.
    """
    if not party_addr:
        log.debug("  box lookup: no party address to anchor the search")
        return None

    lo, hi = box_window(party_addr)
    log.info(f"  box lookup: scanning {lo:#010x}..{hi:#010x} for "
             f"PID {pid:08X}")
    try:
        owned = _scan_owned(ctx, lo, hi, player_ot)
    except Exception as exc:
        log.warning(f"  box lookup failed: {exc}")
        return None

    for addr, p in owned:
        if p.pid == pid and p.species == species:
            slot = (addr - lo) // BOX_SLOT_SIZE
            log.info(f"  box lookup: found it at {addr:#010x} "
                     f"(~box {slot // SLOTS_PER_BOX + 1}, "
                     f"slot {slot % SLOTS_PER_BOX + 1})")
            # _scan_owned yields (address, parsed); the exporter wants
            # the address on the record itself.
            try:
                p.source_address = addr
            except Exception:
                return None
            return p

    log.info(f"  box lookup: PID {pid:08X} is not in the boxes either "
             f"({len(owned)} owned record(s) seen in that window)")
    return None
