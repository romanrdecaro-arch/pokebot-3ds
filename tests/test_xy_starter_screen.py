"""
pokebot/xy_starter_screen.py: is X/Y's starter selection up, and which
starter is focused.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot import xy_starter_screen as xs  # noqa: E402

CRS, FIELD, P3S = 0x012B6000, 0x006A5000, 0x00900000
GM, PM, NODE, PROC, VIEW, UPPER = (0x08C61A78, 0x08C66B44, 0x08C61C4C,
                                   0x08D00000, 0x08D10000, 0x08D20000)


class Memory:
    """Sparse 3DS memory: unmapped reads come back as zeros, as Azahar's do."""

    def __init__(self):
        self.mem = {}

    def put(self, addr, data):
        for i, b in enumerate(data):
            self.mem[addr + i] = b

    def u32(self, addr, value):
        self.put(addr, struct.pack("<I", value))

    def read(self, addr, size):
        return bytes(self.mem.get(addr + i, 0) for i in range(size))

    def read_u32(self, addr):
        return struct.unpack("<I", self.read(addr, 4))[0]


def module(mem, base, name, nxt):
    mem.u32(base + 0x84, base + 0x200)
    mem.u32(base + 0x88, nxt)
    mem.put(base + 0x200, name + b"\0")


def overworld():
    mem = Memory()
    mem.u32(xs.STATIC_CRS_PTR, CRS)
    mem.u32(CRS + 0x88, FIELD)
    module(mem, FIELD, b"DllField", 0)
    mem.u32(xs.GAME_MANAGER_PTR, GM)
    mem.u32(GM + 0x14, PM)
    mem.u32(PM + 0x10, NODE)
    mem.u32(NODE + 0x0C, 0x08C61BE8)      # FieldmapProc
    return mem


def selection(cursor=1):
    mem = overworld()
    module(mem, FIELD, b"DllField", P3S)
    module(mem, P3S, b"DllPoke3Select", 0)
    mem.u32(NODE + 0x0C, PROC)
    mem.u32(PROC, P3S + 0x68C4)
    mem.u32(PROC + 0x3C, VIEW)
    mem.u32(VIEW + 0x7C, UPPER)
    mem.u32(UPPER + 0xEC, cursor)
    return mem


def test_not_up_in_the_overworld():
    assert xs.focused_starter(overworld()) is None


def test_reads_the_focused_starter():
    for i in range(3):
        assert xs.focused_starter(selection(cursor=i)) == i


def test_loading_until_the_screens_proc_is_running():
    mem = selection()
    mem.u32(NODE + 0x0C, 0x08C61BE8)      # module loaded, field proc still current
    assert xs.focused_starter(mem) == xs.LOADING


def test_loading_until_the_view_exists():
    mem = selection()
    mem.u32(VIEW + 0x7C, 0)
    assert xs.focused_starter(mem) == xs.LOADING
    mem.u32(PROC + 0x3C, 0)
    assert xs.focused_starter(mem) == xs.LOADING


def test_an_out_of_range_cursor_is_not_trusted():
    assert xs.focused_starter(selection(cursor=7)) == xs.LOADING


def test_a_null_link_anywhere_is_not_a_crash():
    mem = selection()
    mem.u32(GM + 0x14, 0)
    assert xs.focused_starter(mem) == xs.LOADING
    mem.u32(xs.STATIC_CRS_PTR, 0)
    assert xs.focused_starter(mem) is None


def test_a_looping_module_list_ends():
    mem = overworld()
    module(mem, FIELD, b"DllField", FIELD)  # next points back at itself
    assert xs.focused_starter(mem) is None


def test_a_longer_name_is_not_the_selection_module():
    mem = selection()
    module(mem, P3S, b"DllPoke3SelectX", 0)
    assert xs.focused_starter(mem) is None
