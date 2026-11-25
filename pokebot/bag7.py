"""
The Gen 7 bag in RAM (Sun / Moon, Ultra Sun / Ultra Moon).

PKMN-NTR's ItemsOffset per game. PKHeX's MyItem block is 0xDE0 bytes in
SM (888 entries) and 0xE28 in USUM (906; the extra Z-Crystal and Rotom
pockets): one-word entries, the item ID in bits 0-9 and the count in bits 10-19
(InventoryItem7). Not yet read live on Azahar, so anything that does not
look like a bag is reported as unknown (None), never as empty.
"""
from __future__ import annotations

import struct

#: game key -> (address, length).
BAG = {"SM-USA-1.2": (0x330D5934, 0xDE0),
       "USUM-USA-1.2": (0x33011934, 0xE28)}
SHINY_CHARM = 632
#: Every kind of Poke Ball (PKHeX item IDs): Master..Cherish, the
#: Apricorn balls, Park, Dream, Beast.
BALLS = frozenset([*range(1, 17), *range(492, 501), 576, 851])


def read_bag(rpc, base: int, length: int = 0xE28) -> dict | None:
    """Item ID -> count, or None if the block does not read as a bag."""
    raw = bytes(rpc.read(base, length))
    items: dict = {}
    for w in struct.unpack(f"<{length // 4}I", raw):
        if not w:
            continue
        item, count = w & 0x3FF, (w >> 10) & 0x3FF
        if not (0 < item <= 959 and count <= 999):
            return None
        items[item] = items.get(item, 0) + count
    return items if len(items) >= 5 else None
