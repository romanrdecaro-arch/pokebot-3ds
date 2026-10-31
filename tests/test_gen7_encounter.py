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
    def __init__(self):
        self.sent = []

    def broadcast(self, event, **k):
        self.sent.append((event, k))


class FakeGame:
    key = "USUM-USA-1.2"
    generation = 7

    class offsets:
        foe_base = 0x32540000
        foe_scan_len = 0x20000
        foe_extra = ((0x30028000, 0x18000),)


class InstantEvent(threading.Event):
    """The press gaps are real seconds; the fakes need none of them."""

    def wait(self, timeout=None):
        return self.is_set()


class Ctx:
    def __init__(self, generation=7):
        self.game = FakeGame()
        self.game.generation = generation
        self.input = FakeInput()
        self.dashboard = FakeDashboard()
        self.target = None
        self.config = {}
        self.rpc = None
        self._stop_evt = InstantEvent()
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
    """Battles start after a few wiggles. From the command menu Down, A
    runs; Y, A, A throws a ball, and 15 B later the battle is over -- or
    the menu is back, if the ball broke (``breaks`` times). The hunt is
    stopped when a battle would start with no wilds left."""

    def __init__(self, ctx, wilds, wiggles_per_battle=3, breaks=0):
        self.ctx, self.wilds = ctx, list(wilds)
        self.per, self.breaks = wiggles_per_battle, breaks
        self.state, self.records = "field", [Pk(1, ot="Roman")]
        self.wiggles = self.read = self.bs = 0

    def names(self):
        ev = self.ctx.input.events
        for i in range(self.read, len(ev)):
            if self.state == "menu" and ev[i - 1:i + 1] == ["DpadDown", "A"]:
                self.state = "field"
            elif self.state == "menu" and ev[i - 2:i + 1] == ["Y", "A", "A"]:
                self.state, self.bs = "throw", 0
            elif self.state == "throw" and ev[i] == "B":
                self.bs += 1
                if self.bs == 15:
                    self.state = "menu" if self.breaks else "field"
                    self.breaks -= bool(self.breaks)
        self.read = len(ev)
        if self.state == "field" and ev.count("CircleRight") >= self.per * (self.wiggles + 1):
            self.wiggles += 1
            if not self.wilds:
                self.ctx.request_stop("out of wilds")
            else:
                self.state = "menu"
                self.records.append(self.wilds.pop(0))
        return {"field": {"FieldRo"}, "menu": {"Battle", "Bag"},
                "throw": {"Battle"}}[self.state]

    def scan(self, ctx, windows, keys):
        return [(0x3002F9A0 + i, p) for i, p in enumerate(self.records)]


def run_world(monkeypatch, wilds, generation=7, config=None, breaks=0):
    ctx = Ctx(generation)
    ctx.config = config or {}
    world = World(ctx, wilds, breaks=breaks)
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


STOP = {"random_encounters": {"on_target": "stop"}}


def test_on_target_stop_leaves_the_shiny_with_no_input_after_it(monkeypatch):
    ctx, reported = run_world(monkeypatch, [Pk(10), Pk(11, shiny=True)],
                              config=STOP)
    assert reported == [10, 11]
    assert ctx.stop_reason == "shiny"
    after = ctx.input.events[ctx.input.events.index("A") + 1:]
    # after fleeing the first, only wiggles until the shiny -- then nothing
    assert set(after) <= {"CircleLeft", "CircleRight"}
    assert after[-1] == "CircleRight"


def test_a_shiny_is_caught_then_the_wiggle_goes_on(monkeypatch):
    ctx, reported = run_world(monkeypatch, [Pk(10), Pk(11, shiny=True), Pk(12)])
    ev = ctx.input.events
    assert reported == [10, 11, 12]
    assert ctx.stop_reason == "out of wilds"
    i = ev.index("Y")
    assert ev[i - 8:i] == ["B"] * 8           # text cleared before Y
    assert ev[i:i + 18] == ["Y", "A", "A"] + ["B"] * 15
    assert ev[i + 18] == "CircleLeft"         # straight back to wiggling
    assert ev.count("Y") == 1
    assert ctx.dashboard.sent == [("target_caught", dict(
        species=731, shiny=True, count=2, caught=1))]


def test_a_ball_that_breaks_is_thrown_again(monkeypatch):
    ctx, _ = run_world(monkeypatch, [Pk(11, shiny=True)], breaks=1)
    assert ctx.input.events.count("Y") == 2
    assert [e for e, _ in ctx.dashboard.sent] == ["target_caught"]


def test_ten_broken_balls_stop_with_the_battle_up(monkeypatch):
    ctx, _ = run_world(monkeypatch, [Pk(11, shiny=True)], breaks=99)
    ev = ctx.input.events
    assert ev.count("Y") == 10
    assert ctx.stop_reason == "catch failed"
    assert "DpadDown" not in ev               # never ran from it
    assert ctx.dashboard.sent == []


def test_no_ball_is_thrown_once_the_battle_ends_during_the_settle(monkeypatch):
    """A slow Gotcha! can still have Bag loaded; Y must not reach the field."""
    ctx = Ctx()
    monkeypatch.setattr(g7.Modules, "names", lambda self: (
        {"Battle", "Bag"} if ctx.input.events.count("B") < 3 else {"FieldRo"}))
    assert g7.catch(ctx, g7.Modules(None), g7.WigglePlan()) is True
    assert "Y" not in ctx.input.events and "A" not in ctx.input.events


def test_on_target_reads_catch_by_default():
    assert g7.WigglePlan.from_config({}).catch is True
    assert g7.WigglePlan.from_config({"on_target": "STOP"}).catch is False


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
