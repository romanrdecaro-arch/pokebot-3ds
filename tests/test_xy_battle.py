"""
pokebot/xy_battle.py and the flee that watches it.

The layout was read from DllBattle and checked live on X: the menu
object turned up 0.34 s into each battle, +0xA0 went 1 as the menu
opened, +0x93 settled at 1 when it was fully drawn, +0x8C went 3 when
Run was chosen, and DllBattle unloaded when the battle ended.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot import xy_battle as xb  # noqa: E402
from pokebot.modes import encounter  # noqa: E402
from test_xy_starter_screen import (FIELD, GM, NODE, PM, Memory,  # noqa: E402
                                    module, overworld)

BATTLE = 0x006A5000                      # DllBattle takes DllField's slot
PROC, VIEWROOT, VIEW, MENU = 0x08C61BE8, 0x08D00000, 0x08D10000, 0x08515904


def battle(up=1, drawn=1, chosen=0, menu=True):
    mem = overworld()
    module(mem, FIELD, b"DllBackGround", BATTLE)
    module(mem, BATTLE, b"DllBattle", 0)
    mem.u32(NODE + 0x0C, PROC)
    mem.u32(PROC, BATTLE + 0xDADAC)
    mem.u32(PROC + 0x20, VIEWROOT)
    mem.u32(VIEWROOT + 0x1F8, VIEW)
    if menu:
        mem.u32(VIEW + 0x190, MENU)
        mem.u32(MENU + 0x5C, BATTLE + 0xDAF7C)
        mem.put(MENU + 0x8C, bytes([chosen]))
        mem.put(MENU + 0x93, bytes([drawn]))
        mem.put(MENU + 0xA0, bytes([up]))
    return mem


def test_the_overworld_is_not_a_battle():
    assert xb.battle_state(overworld()) == xb.NONE


def test_menu_when_drawn_and_waiting():
    assert xb.battle_state(battle()) == xb.MENU


def test_busy_before_the_menu_exists():
    assert xb.battle_state(battle(menu=False)) == xb.BUSY


def test_busy_while_the_menu_wipes_in():
    assert xb.battle_state(battle(drawn=4)) == xb.BUSY


def test_busy_once_a_command_is_chosen():
    assert xb.battle_state(battle(chosen=3)) == xb.BUSY


def test_busy_when_the_menu_is_closed():
    assert xb.battle_state(battle(up=0)) == xb.BUSY


def test_a_wrong_vtable_is_not_trusted():
    mem = battle()
    mem.u32(MENU + 0x5C, 0x1234)
    assert xb.battle_state(mem) == xb.BUSY


def test_dllbattle_loaded_without_its_proc_running_is_not_a_battle():
    mem = battle()
    mem.u32(NODE + 0x0C, 0x08C00000)          # some other proc
    assert xb.battle_state(mem) == xb.NONE


# --------------------------------------------------------------------
# The flee that watches the battle
# --------------------------------------------------------------------

class FakeInput:
    def __init__(self):
        self.events = []

    def tap(self, button, hold_s=0.05):
        self.events.append(button)

    def tap_touch(self, x, y, hold_s=0.08):
        self.events.append("TOUCH")
        return True


class FakeEvt:
    def is_set(self):
        return False

    def wait(self, s):
        pass


class Ctx:
    rpc = None

    def __init__(self):
        self.input = FakeInput()
        self._stop_evt = FakeEvt()

    def should_stop(self):
        return False


def run_flee(monkeypatch, states, timeout=5.0):
    seq = iter(states)
    monkeypatch.setattr(encounter, "battle_state", lambda rpc: next(seq))
    ctx = Ctx()
    ok = encounter._flee_watching_ram(ctx, "default", None, None, timeout)
    return ok, ctx.input.events


def test_flee_clears_text_then_touches_run_once(monkeypatch):
    ok, ev = run_flee(monkeypatch, ["busy", "busy", "menu", "busy", "busy", "none"])
    assert ok
    assert ev == ["B", "B", "TOUCH", "B", "B"]


def test_flee_waits_for_the_battle_to_start(monkeypatch):
    """Live on X: the wild is in RAM before the battle runs, and the
    first version took that 'no battle' as 'battle over' and walked off
    into the battle."""
    ok, ev = run_flee(monkeypatch, ["none", "none", "busy", "menu", "busy", "none"])
    assert ok
    assert ev == ["B", "TOUCH", "B"]


def test_no_battle_at_all_is_nothing_to_flee(monkeypatch):
    import itertools
    monkeypatch.setattr(encounter, "_BATTLE_START_S", 0.05)
    seq = itertools.repeat("none")
    monkeypatch.setattr(encounter, "battle_state", lambda rpc: next(seq))
    ctx = Ctx()
    assert encounter._flee_watching_ram(ctx, "default", None, None, 5.0)
    assert ctx.input.events == []


def test_a_failed_escape_touches_run_again(monkeypatch):
    ok, ev = run_flee(monkeypatch, ["menu", "busy", "menu", "busy", "none"])
    assert ok and ev.count("TOUCH") == 2


def test_a_battle_that_never_ends_gives_up(monkeypatch):
    import itertools
    seq = itertools.repeat("busy")
    monkeypatch.setattr(encounter, "battle_state", lambda rpc: next(seq))
    ok = encounter._flee_watching_ram(Ctx(), "default", None, None, timeout=0.05)
    assert ok is False


def test_only_xy_watch_the_battle():
    class G:
        key = "X-USA"

    class C:
        game = G()
    assert encounter._reads_battle(C())
    G.key = "OR-USA"
    assert not encounter._reads_battle(C())
    assert not encounter._reads_battle(object())
