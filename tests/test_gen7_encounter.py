"""
Gen 7 random encounters, wiggling in place (pokebot/modes/gen7_encounter.py).

Found live on Ultra Sun: equal Circle Pad Left/Right pushes roll
encounters without leaving the spot, the loaded modules show Battle
during a battle and Bag while the command menu is up, and D-pad Down
then A runs.
"""
from __future__ import annotations

import struct
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot.modes import gen7_encounter as g7  # noqa: E402
from test_xy_starter_screen import Memory  # noqa: E402


# --------------------------------------------------------------------
# Finding the loaded modules
# --------------------------------------------------------------------

def modules_memory(names, crs=0x008B2000):
    mem = Memory()
    mem.put(crs + 0x80, b"CRO0")
    nxt = 0
    for i, name in reversed(list(enumerate(names))):
        m = 0x00A00000 + 0x10000 * i
        mem.u32(m + 0x84, m + 0x200)
        mem.u32(m + 0x88, nxt)
        mem.put(m + 0x200, name.encode() + b"\0")
        nxt = m
    mem.u32(crs + 0x88, nxt)
    return mem


def test_modules_found_by_their_cro0_page():
    mods = g7.Modules(modules_memory(["FieldRo", "FieldMenu"]))
    assert mods.names() == {"FieldRo", "FieldMenu"}
    assert mods.crs == 0x008B2000


def test_the_crs_is_found_again_after_a_reload():
    mem = modules_memory(["Battle"])
    mods = g7.Modules(mem)
    mods.names()
    mem.put(0x008B2000 + 0x80, b"\0\0\0\0")        # gone
    moved = modules_memory(["FieldRo"], crs=0x00900000)
    mem.mem.update(moved.mem)
    assert mods.names() == {"FieldRo"} and mods.crs == 0x00900000


def test_no_crs_means_no_modules():
    assert g7.Modules(Memory()).names() == set()


# --------------------------------------------------------------------
# Fakes for the loop
# --------------------------------------------------------------------

class FakeInput:
    def __init__(self):
        self.events = []

    def tap(self, button, hold_s=0.05):
        self.events.append(button)


class FakeDashboard:
    def broadcast(self, *a, **k):
        pass


class FakeGame:
    key = "USUM-USA-1.2"
    generation = 7

    class offsets:
        foe_base = 0x32540000
        foe_scan_len = 0x20000
        foe_extra = ((0x30028000, 0x18000),)


class Ctx:
    def __init__(self, generation=7):
        self.game = FakeGame()
        self.game.generation = generation
        self.input = FakeInput()
        self.dashboard = FakeDashboard()
        self.target = None
        self.config = {}
        self.rpc = None
        self._stop_evt = threading.Event()
        self.stop_reason = None

    def should_stop(self):
        return self._stop_evt.is_set()

    def request_stop(self, reason=""):
        self.stop_reason = reason
        self._stop_evt.set()


class Pk:
    def __init__(self, key, shiny=False, ot=""):
        self.encryption_key, self.shiny, self.ot_name = key, shiny, ot
        self.species = 731


class World:
    """Battles start after a few wiggles and end when RUN is taken."""

    def __init__(self, ctx, wilds, wiggles_per_battle=3):
        self.ctx, self.wilds = ctx, list(wilds)
        self.per = wiggles_per_battle
        self.state, self.records = "field", [Pk(1, ot="Roman")]
        self.wiggles = 0

    def names(self):
        ev = self.ctx.input.events
        if self.state == "field" and ev.count("CircleRight") >= self.per * (self.wiggles + 1):
            self.wiggles += 1
            self.state = "menu"
            self.records.append(self.wilds.pop(0))
        if self.state == "menu" and ev[-1:] == ["A"]:
            self.state = "field"
        return {"field": {"FieldRo"}, "menu": {"Battle", "Bag"}}[self.state]

    def scan(self, ctx, windows, keys):
        return [(0x3002F9A0 + i, p) for i, p in enumerate(self.records)]


def run_world(monkeypatch, wilds, generation=7):
    ctx = Ctx(generation)
    world = World(ctx, wilds)
    monkeypatch.setattr(g7.Modules, "names", lambda self: world.names())
    monkeypatch.setattr(g7, "scan_windows", world.scan)
    monkeypatch.setattr(g7, "read_xz", lambda rpc, route: None)   # no anchor
    monkeypatch.setattr(g7, "ensure_targets_dir", lambda: None)
    monkeypatch.setattr(g7, "_focus_if_needed", lambda ctx: None)
    reported = []
    monkeypatch.setattr(g7, "_report_encounter",
                        lambda c, p, a, n, via: reported.append(p.encryption_key))
    g7.run(ctx)
    return ctx, reported


def test_a_shiny_stops_with_no_input_after_it(monkeypatch):
    ctx, reported = run_world(monkeypatch, [Pk(10), Pk(11, shiny=True)])
    assert reported == [10, 11]
    assert ctx.stop_reason == "shiny"
    after = ctx.input.events[ctx.input.events.index("A") + 1:]
    # after fleeing the first, only wiggles until the shiny -- then nothing
    assert set(after) <= {"CircleLeft", "CircleRight"}
    assert after[-1] == "CircleRight"


def test_a_non_shiny_is_run_from_with_down_then_a(monkeypatch):
    ctx, _ = run_world(monkeypatch, [Pk(10), Pk(11, shiny=True)])
    ev = ctx.input.events
    i = ev.index("A")
    assert ev[i - 2:i + 1] == ["B", "DpadDown", "A"]


def test_the_wiggle_is_equal_left_and_right(monkeypatch):
    ctx, _ = run_world(monkeypatch, [Pk(11, shiny=True)])
    moves = [e for e in ctx.input.events if e.startswith("Circle")]
    assert moves.count("CircleLeft") == moves.count("CircleRight")
    assert all(a != b for a, b in zip(moves, moves[1:]))


def test_the_players_own_records_are_never_judged(monkeypatch):
    ctx, reported = run_world(monkeypatch, [Pk(11, shiny=True)])
    assert 1 not in reported                  # OT 'Roman', present at start


def test_not_a_gen7_game_stops_at_once(monkeypatch):
    ctx, _ = run_world(monkeypatch, [], generation=6)
    assert ctx.stop_reason == "not a Gen 7 game"
    assert ctx.input.events == []


def test_starting_inside_a_battle_is_refused(monkeypatch):
    ctx = Ctx()
    monkeypatch.setattr(g7.Modules, "names", lambda self: {"Battle", "Bag"})
    monkeypatch.setattr(g7, "ensure_targets_dir", lambda: None)
    monkeypatch.setattr(g7, "_focus_if_needed", lambda ctx: None)
    g7.run(ctx)
    assert ctx.stop_reason == "started in a battle"
    assert ctx.input.events == []


def test_flee_gives_up_at_the_timeout(monkeypatch):
    ctx = Ctx()
    monkeypatch.setattr(g7.Modules, "names", lambda self: {"Battle"})
    assert g7.flee(ctx, g7.Modules(None), timeout=0.05) is False
    assert set(ctx.input.events) == {"B"}


@pytest.mark.parametrize("hold,expect", [(0.08, 0.08), (5, 0.5), (0, 0.02), ("x", 0.08)])
def test_wiggle_hold_is_clamped(hold, expect):
    assert g7.WigglePlan.from_config({"gen7_wiggle_hold": hold}).hold == expect


# --------------------------------------------------------------------
# Holding the spot (Ultra Sun / Ultra Moon)
# --------------------------------------------------------------------

def test_read_xz_follows_the_route():
    mem = Memory()
    root, parent, player = 0x0062143C, 0x30007078, 0x33F81E20
    mem.u32(root, parent)
    mem.u32(parent + 0x88, player)
    mem.u32(player, 0x0064A4F0)
    mem.put(player + 0x3B0, struct.pack("<3f", 731.5, 3.9, 1668.5))
    route = g7._PLAYER_ROUTE["USUM-USA-1.2"]
    assert g7.read_xz(mem, route) == (731.5, 1668.5)
    mem.u32(parent + 0x88, 0)                 # null during a battle
    assert g7.read_xz(mem, route) is None
    mem.u32(parent + 0x88, player)
    mem.u32(player, 0x1234)                   # not the player object
    assert g7.read_xz(mem, route) is None


class DriftWorld:
    """The measured behaviour: a push moves `speed` units per second
    along its axis, and every Left/Right reversal adds `drift` south."""

    def __init__(self, speed=1800.0, drift=2.0):
        self.x = self.z = 0.0
        self.speed, self.drift, self.facing = speed, drift, None
        self.events = []

    def tap(self, button, hold_s=0.05):
        self.events.append((button, hold_s))
        step = self.speed * hold_s
        if button in ("CircleLeft", "CircleRight"):
            if self.facing and self.facing != button and \
                    {self.facing, button} == {"CircleLeft", "CircleRight"}:
                self.z += self.drift          # the swing through facing down
            self.x += step if button == "CircleRight" else -step
        elif button == "CircleUp":
            self.z -= step
        elif button == "CircleDown":
            self.z += step
        self.facing = button


def run_anchor(world, cycles, anchored=True, hold=0.08):
    ctx = Ctx()
    ctx.input = world
    anchor = g7.Anchor(None, None, (0.0, 0.0))
    anchor.sample = lambda: anchor.samples.append((world.x, world.z))
    g7_read = g7.read_xz
    g7.read_xz = lambda rpc, route: (world.x, world.z)
    try:
        for _ in range(cycles):
            for button in ("CircleLeft", "CircleRight"):
                world.tap(button, hold)
                anchor.sample()
            if anchored:
                anchor.correct(ctx, hold)
            else:
                anchor.samples.clear()
    finally:
        g7.read_xz = g7_read
    return anchor


def test_unanchored_the_measured_drift_builds_up():
    world = DriftWorld()
    run_anchor(world, 200, anchored=False)
    assert world.z > 300                      # ~2 units a cycle, south


def test_the_anchor_holds_the_spot_over_a_long_run():
    world = DriftWorld()
    run_anchor(world, 200)
    assert abs(world.z) <= g7._ANCHOR_TOL + 1
    assert abs(world.x) <= g7._ANCHOR_TOL + 1   # where each pair comes to rest


class LopsidedWorld(DriftWorld):
    """Live, the two pushes did not travel equally far: the reversal eats
    part of each push, and not the same part both ways."""

    def tap(self, button, hold_s=0.05):
        if button == "CircleLeft" and self.facing == "CircleRight":
            hold_s *= 1.6                     # Left goes further than Right
        super().tap(button, hold_s)


def test_unequal_pushes_still_come_back_to_home():
    world = LopsidedWorld()
    run_anchor(world, 200)
    assert abs(world.x) <= g7._ANCHOR_TOL + 1
    assert abs(world.z) <= g7._ANCHOR_TOL + 1


def test_corrections_push_toward_home():
    world = DriftWorld()
    world.z = 100.0                           # well south of home
    anchor = run_anchor(world, 1)
    ups = [h for b, h in world.events if b == "CircleUp"]
    assert ups and not any(b == "CircleDown" for b, _ in world.events)
    assert 0.02 <= ups[0] <= 0.25


def test_the_anchor_learns_a_slower_emulator():
    fast, slow = DriftWorld(speed=1800.0), DriftWorld(speed=180.0)   # ~10x vs 1x
    a, b = run_anchor(fast, 5), run_anchor(slow, 5)
    assert a.speed[0] == pytest.approx(10 * b.speed[0], rel=0.05)
    run_anchor(slow, 200)
    assert abs(slow.z) <= g7._ANCHOR_TOL + 1
