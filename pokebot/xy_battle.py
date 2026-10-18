"""
X/Y battle state, read from RAM.

Found in the DllBattle code and checked live on X, 2026-10-18:

* A battle runs as ``btl::BattleMainProc`` from the DllBattle module (the
  overworld's DllField is unloaded meanwhile). Its vtable sits at module
  base + 0xDADAC.
* proc +0x20 -> +0x1F8 is the ``BattleViewSystem``; its +0x190 is the
  command menu, ``btl::BtlvUiActSelect``, whose vtable is at +0x5C of
  the object, not +0.
* In the menu object: +0xA0 is 1 while the action menu is up, +0x93 is 1
  once its entry animation is done (4 while the tile wipe plays), and
  +0x8C is the command chosen (0 until one is; Run is 3).
"""
from __future__ import annotations

from .xy_starter_screen import _follow, module_base, running_proc

NONE, BUSY, MENU = "none", "busy", "menu"
_MODULE = b"DllBattle"
_PROC_VT = 0xDADAC
_MENU_VT = 0xDAF7C


def battle_state(rpc) -> str:
    """NONE outside a battle; MENU when the command menu is drawn and
    waiting for a choice; BUSY for everything in between (intro, text,
    animations, the escape itself)."""
    base = module_base(rpc, _MODULE)
    proc = running_proc(rpc) if base is not None else 0
    if not proc or rpc.read_u32(proc) != base + _PROC_VT:
        return NONE
    menu = _follow(rpc, proc + 0x20, 0x1F8, 0x190)
    if not menu or rpc.read_u32(menu + 0x5C) != base + _MENU_VT:
        return BUSY
    fields = rpc.read(menu + 0x8C, 0x15)          # +0x8C .. +0xA0
    chosen, drawn, up = fields[0], fields[0x93 - 0x8C], fields[0xA0 - 0x8C]
    return MENU if up == 1 and drawn == 1 and chosen == 0 else BUSY
