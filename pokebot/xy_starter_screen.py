"""
X/Y's starter selection screen, read from RAM.

The screen is its own CRO module, DllPoke3Select, loaded only while the
three starters are on offer (script command CallPoke3Select). Found by
reverse engineering kujira code.bin and the module, 2026-10-18:

* 0x005D0E88 holds the static CRS; loaded CROs hang off it as a list
  (header +0x84 name pointer, +0x88 next).
* 0x005E9E90 holds the GameManager: +0x14 GameProcManager, +0x10 the
  running process node, +0x0C its proc.
* While the screen runs, that proc is an app::poke3select::Proc (vtable
  at module base + 0x68C4). Proc+0x3C is its View; View+0x7C is the
  upper-screen view, whose +0xEC is the focused starter (the one named
  on screen): 0 Chespin, 1 Fennekin, 2 Froakie. Checked live on Y: it
  follows every D-pad tap, and the cursor stops at both ends.
"""
from __future__ import annotations

import struct
from typing import Optional

STATIC_CRS_PTR = 0x005D0E88
GAME_MANAGER_PTR = 0x005E9E90
MODULE = b"DllPoke3Select"
_PROC_VTABLE = 0x68C4
#: More modules than X/Y ever has loaded at once; bounds a corrupt list.
_MAX_MODULES = 64

#: The screen's module is loaded but its cursor cannot be read yet.
LOADING = -1
STARTER_INDEX = {"chespin": 0, "fennekin": 1, "froakie": 2}


def _follow(rpc, addr: int, *offsets: int) -> int:
    """Read the pointer at ``addr``, then at each pointer + offset; 0 if
    any link is null."""
    ptr = rpc.read_u32(addr)
    for off in offsets:
        if not ptr:
            return 0
        ptr = rpc.read_u32(ptr + off)
    return ptr


def _module_base(rpc) -> Optional[int]:
    m = _follow(rpc, STATIC_CRS_PTR, 0x88)
    for _ in range(_MAX_MODULES):
        if not m:
            return None
        name_ptr, nxt = struct.unpack("<II", rpc.read(m + 0x84, 8))
        if rpc.read(name_ptr, len(MODULE) + 1) == MODULE + b"\0":
            return m
        m = nxt
    return None


def focused_starter(rpc) -> Optional[int]:
    """None when the selection screen is not up, LOADING while it is
    coming up, otherwise the focused starter's index."""
    base = _module_base(rpc)
    if base is None:
        return None
    proc = _follow(rpc, GAME_MANAGER_PTR, 0x14, 0x10, 0x0C)
    if not proc or rpc.read_u32(proc) != base + _PROC_VTABLE:
        return LOADING
    upper = _follow(rpc, proc + 0x3C, 0x7C)
    index = rpc.read_u32(upper + 0xEC) if upper else LOADING
    return index if 0 <= index <= 2 else LOADING
