"""
The Gen 7 bag in RAM (Ultra Sun / Ultra Moon).

PKMN-NTR's USUM ItemsOffset. PKHeX's MyItem block is 0xE28 bytes: 906
one-word entries, the item ID in bits 0-9 and the count in bits 10-19
(InventoryItem7). Not yet read live on Azahar, so anything that does not
look like a bag is reported as unknown (None), never as empty.
"""
from __future__ import annotations

import struct

BAG = {"USUM-USA-1.2": 0x33011934}
_LEN = 0xE28
SHINY_CHARM = 632
#: Every kind of Poke Ball (PKHeX item IDs): Master..Cherish, the
#: Apricorn balls, Park, Dream, Beast.
BALLS = frozenset([*range(1, 17), *range(492, 501), 576, 851])


def read_bag(rpc, base: int) -> dict | None:
    """Item ID -> count, or None if the block does not read as a bag."""
    raw = bytes(rpc.read(base, _LEN))
    items: dict = {}
    for w in struct.unpack(f"<{_LEN // 4}I", raw):
        if not w:
            continue
        item, count = w & 0x3FF, (w >> 10) & 0x3FF
        if not (0 < item <= 959 and count <= 999):
            return None
        items[item] = items.get(item, 0) + count
    return items if len(items) >= 5 else None
