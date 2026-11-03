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

    def __init__(self, ctx, wilds, wiggles_per_battle=3, breaks=0,
                 leftover=None, trigger="CircleRight"):
        self.ctx, self.wilds, self.trigger = ctx, list(wilds), trigger
        self.per, self.breaks = wiggles_per_battle, breaks
        self.state, self.records = "field", [Pk(1, ot="Roman")]
        if leftover is not None:                 # started inside its battle
            self.state = "menu"
            self.records.append(leftover)
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
        if self.state == "field" and ev.count(self.trigger) >= self.per * (self.wiggles + 1):
            self.wiggles += 1
            if not self.wilds:
                self.ctx.request_stop("out of wilds")
            else:
                self.state = "menu"
                wild = self.wilds.pop(0)
                if wild is not None:        # None: a battle with no new record
                    self.records.append(wild)
        return {"field": {"FieldRo"}, "menu": {"Battle", "Bag"},
                "throw": {"Battle"}}[self.state]

    def scan(self, ctx, windows, keys):
        return [(0x3002F9A0 + i, p) for i, p in enumerate(self.records)]


def run_world(monkeypatch, wilds, generation=7, config=None, breaks=0,
              leftover=None, balls=None):
    ctx = Ctx(generation)
    ctx.config = config or {}
    vertical = (ctx.config.get("random_encounters") or {}).get("movement") == "vertical"
    world = World(ctx, wilds, breaks=breaks, leftover=leftover,
                  trigger="CircleDown" if vertical else "CircleRight")
    monkeypatch.setattr(g7, "_balls_left", lambda ctx: balls)  # None: unread
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


def test_twenty_five_broken_balls_stop_with_the_battle_up(monkeypatch):
    ctx, _ = run_world(monkeypatch, [Pk(11, shiny=True)], breaks=99)
    ev = ctx.input.events
    assert ev.count("Y") == 25
    assert ctx.stop_reason == "catch failed"
    assert "DpadDown" not in ev               # never ran from it
    assert ctx.dashboard.sent == []


def test_an_empty_bag_throws_nothing(monkeypatch):
    """No balls: Y would do nothing and A, A would attack the shiny."""
    ctx, _ = run_world(monkeypatch, [Pk(11, shiny=True)], balls=0)
    assert ctx.stop_reason == "catch failed"
    assert not {"Y", "A"} & set(ctx.input.events)


def test_balls_are_counted_from_the_bag(monkeypatch):
    bag = {4: 60, 2: 5, 17: 3, 576: 1}       # Poke, Ultra, a Potion, Dream
    monkeypatch.setattr(g7, "read_bag", lambda rpc, base: bag)
    assert g7._balls_left(Ctx()) == 66
    monkeypatch.setattr(g7, "read_bag", lambda rpc, base: None)
    assert g7._balls_left(Ctx()) is None


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


def _quick_find(monkeypatch):
    """The record that never turns up is waited for 8 real seconds."""
    real = g7.find_wild
    monkeypatch.setattr(g7, "find_wild",
                        lambda ctx, w, seen, timeout: real(ctx, w, seen, 0.05))


def test_a_battle_with_no_new_record_is_run_from_when_nothing_is_a_hit(monkeypatch):
    _quick_find(monkeypatch)
    ctx, reported = run_world(monkeypatch, [Pk(10), None, Pk(12)])
    assert reported == [10, 12]
    assert ctx.stop_reason == "out of wilds"


def test_a_battle_with_no_new_record_is_left_if_any_record_is_a_shiny(monkeypatch):
    _quick_find(monkeypatch)
    """The false "battle over" right after a shiny: what looks like a
    new battle may be the shiny's own, so nothing is run from."""
    ctx, reported = run_world(monkeypatch, [Pk(11, shiny=True), None],
                              config=STOP)
    assert ctx.stop_reason == "shiny"            # stopped on the shiny itself
    ctx, reported = run_world(monkeypatch, [Pk(11, shiny=True), None])
    assert ctx.stop_reason == "started in a battle"
    assert ctx.input.events[-1] != "A"           # no RUN after it


def test_an_up_tick_goes_in_every_n_pairs(monkeypatch):
    cfg = {"random_encounters": {"gen7_tick": "up", "gen7_tick_every": 2}}
    ctx, _ = run_world(monkeypatch, [Pk(10)], config=cfg)
    moves = [e for e in ctx.input.events if e.startswith("Circle")]
    assert moves[:7] == ["CircleLeft", "CircleRight", "CircleUp",
                         "CircleLeft", "CircleRight", "CircleLeft",
                         "CircleRight"]


def test_the_tick_off_sends_only_the_wiggle(monkeypatch):
    ctx, _ = run_world(monkeypatch, [Pk(10)],
                       config={"random_encounters": {"gen7_tick": "off"}})
    assert {e for e in ctx.input.events if e.startswith("Circle")} == {
        "CircleLeft", "CircleRight"}


def test_vertical_movement_wiggles_up_and_down(monkeypatch):
    ctx, reported = run_world(monkeypatch, [Pk(10)],
                              config={"random_encounters": {"movement": "vertical"}})
    moves = [e for e in ctx.input.events if e.startswith("Circle")]
    assert reported == [10]
    assert set(moves) == {"CircleUp", "CircleDown"}
    assert moves.count("CircleUp") == moves.count("CircleDown")


def test_movement_defaults_to_left_and_right():
    assert g7.WigglePlan.from_config({}).pair == ("CircleLeft", "CircleRight")
    assert g7.WigglePlan.from_config({"movement": "horizontal"}).axis == 0
    assert g7.WigglePlan.from_config({"movement": "Vertical"}).axis == 1


def test_the_players_own_records_are_never_judged(monkeypatch):
    ctx, reported = run_world(monkeypatch, [Pk(11, shiny=True)])
    assert 1 not in reported                  # OT 'Roman', present at start


def test_not_a_gen7_game_stops_at_once(monkeypatch):
    ctx, _ = run_world(monkeypatch, [], generation=6)
    assert ctx.stop_reason == "not a Gen 7 game"
    assert ctx.input.events == []


@pytest.mark.parametrize("records", [[], [Pk(5), Pk(6, shiny=True)]],
                         ids=["no wild readable", "a shiny record"])
def test_a_battle_on_screen_that_may_be_a_shiny_is_left_alone(monkeypatch, records):
    ctx = Ctx()
    monkeypatch.setattr(g7.Modules, "names", lambda self: {"Battle", "Bag"})
    monkeypatch.setattr(g7, "scan_windows", lambda c, w, k: list(enumerate(records)))
    monkeypatch.setattr(g7, "ensure_targets_dir", lambda: None)
    monkeypatch.setattr(g7, "_focus_if_needed", lambda ctx: None)
    g7.run(ctx)
    assert ctx.stop_reason == "started in a battle"
    assert ctx.input.events == []


def test_a_leftover_battle_without_a_hit_is_run_from_then_the_hunt_goes_on(monkeypatch):
    ctx, reported = run_world(monkeypatch, [Pk(11, shiny=True)], leftover=Pk(9))
    ev = ctx.input.events
    assert ev[:3] == ["B", "DpadDown", "A"]   # ran from the leftover first
    assert 9 not in reported                  # never judged as a new wild
    assert reported == [11] and "Y" in ev


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
    """GameManager singleton -> +0x24 -> +0xCC move-model manager ->
    slot 0 at +0x0C -> the player, +0x3B0 x, y, z."""
    mem = Memory()
    gm, resident, mgr, player = 0x08A00000, 0x08B00000, 0x08C00000, 0x33F81E20
    mem.u32(0x006A3984, gm)
    mem.u32(gm + 0x24, resident)
    mem.u32(resident + 0xCC, mgr)
    mem.u32(mgr + 0x0C, player)
    mem.u32(player, 0x0064A4F0)
    mem.put(player + 0x3B0, struct.pack("<3f", 731.5, 3.9, 1668.5))
    route = g7._PLAYER_ROUTE["USUM-USA-1.2"]
    assert g7.read_xz(mem, route) == (731.5, 1668.5)
    mem.u32(mgr + 0x0C, 0)                    # no player model
    assert g7.read_xz(mem, route) is None
    mem.u32(mgr + 0x0C, player)
    mem.u32(player, 0x1234)                   # not the player object
    assert g7.read_xz(mem, route) is None


def test_a_bad_route_reads_nothing_outside_the_heaps():
    """The first route read ASCII as a pointer; a wrong link now stops
    before reading anywhere a pointer cannot point."""
    reads = []

    class Spy(Memory):
        def read_u32(self, addr):
            reads.append(addr)
            return super().read_u32(addr)
    mem = Spy()
    mem.u32(0x006A3984, 0x34333231 & 0x00FFFFFF)   # a code-ish address
    assert g7.read_xz(mem, g7._PLAYER_ROUTE["USUM-USA-1.2"]) is None
    assert reads == [0x006A3984]


def test_one_read_without_battle_does_not_end_it():
    """Live 2026-11-02: three times a read during the intro lacked
    Battle and the bot "ran in 0.0s", then fled the same battle again."""
    answers = iter([{"FieldRo"}, {"Battle"}, {"Battle", "Bag"}])
    mods = type("M", (), {"names": lambda self: next(answers)})()
    assert not g7.battle_over(Ctx(), mods, gap=0.0)
    answers = iter([{"FieldRo"}] * 3)
    assert g7.battle_over(Ctx(), mods, gap=0.0)


def test_the_drift_fix_options():
    p = g7.WigglePlan.from_config
    assert p({}).tick == "auto"
    assert p({"gen7_tick": "off"}).tick == "off"
    assert p({"gen7_tick": "Up", "gen7_tick_every": "5"}).tick == "CircleUp"
    assert p({"gen7_tick_every": 5}).tick_every == 5
    assert p({"gen7_tick": "sideways", "gen7_tick_every": "x"}).tick == "auto"
    assert p({"gen7_tick_every": 0}).tick_every == 1


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


def run_anchor(world, cycles, anchored=True, hold=0.08,
               pair=("CircleLeft", "CircleRight")):
    ctx = Ctx()
    ctx.input = world
    anchor = g7.Anchor(None, None, (0.0, 0.0),
                       axis=1 if pair[0] == "CircleUp" else 0)
    anchor.sample = lambda: anchor.samples.append((world.x, world.z))
    g7_read = g7.read_xz
    g7.read_xz = lambda rpc, route: (world.x, world.z)
    try:
        for _ in range(cycles):
            for button in pair:
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


class SidewaysDriftWorld(DriftWorld):
    """An Up / Down wiggle, assumed to creep sideways the way Left /
    Right creeps south (not measured live)."""

    def tap(self, button, hold_s=0.05):
        if {self.facing, button} == {"CircleUp", "CircleDown"}:
            self.x += self.drift
        super().tap(button, hold_s)


def test_the_anchor_holds_a_vertical_wiggle_too():
    world = SidewaysDriftWorld()
    anchor = run_anchor(world, 200, pair=("CircleUp", "CircleDown"))
    assert abs(world.x) <= g7._ANCHOR_TOL + 1
    assert abs(world.z) <= g7._ANCHOR_TOL + 1
    assert anchor.speed[1] == pytest.approx(1800.0, rel=0.05)


def test_the_anchor_learns_a_slower_emulator():
    fast, slow = DriftWorld(speed=1800.0), DriftWorld(speed=180.0)   # ~10x vs 1x
    a, b = run_anchor(fast, 5), run_anchor(slow, 5)
    assert a.speed[0] == pytest.approx(10 * b.speed[0], rel=0.05)
    run_anchor(slow, 200)
    assert abs(slow.z) <= g7._ANCHOR_TOL + 1


def test_the_launchers_drift_choice_reaches_the_hunt(tmp_path, monkeypatch):
    import run as run_mod
    import pokebot.bot as bot_mod

    class Stop(Exception):
        pass

    got = {}

    def fake_bot(config, **kw):
        got.update(config)
        raise Stop

    monkeypatch.setattr(bot_mod, "Bot", fake_bot)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("mode: gen7_encounter\n", encoding="utf-8")
    with pytest.raises(Stop):
        run_mod.main(["--gen7-tick", "down", "--config", str(cfg)])
    assert got["random_encounters"]["gen7_tick"] == "down"
    assert g7.WigglePlan.from_config(got["random_encounters"]).tick == "CircleDown"
    src = (Path(__file__).resolve().parents[1] / "launcher.py").read_text(encoding="utf-8")
    assert '"--gen7-tick"' in src and "_drift_choices" in src
