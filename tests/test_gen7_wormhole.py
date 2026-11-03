"""The Ultra Warp Ride hunt, end to end against a simulated game.

The simulation follows what 58 videos of the ride show: A three times at
the altar's hole (the prompt, the menu's top entry), a ride that ends
when a hole takes it, a world whose Pokemon hides at the far end of a
fixed corridor -- right, then up, like the red world -- under a camera
that turns the Circle Pad 90 degrees, and the hole back right behind the
arrival spot. Nothing here has met the real game yet.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pokebot.modes import gen7_encounter as g7  # noqa: E402
from pokebot.modes import gen7_wormhole as wh  # noqa: E402

HOME, SPAWN = (1000.0, 990.0), (1000.0, 1200.0)
ARRIVAL, SPOT = (0.0, 15.0), (285.0, -270.0)
SPEED = 400.0                           # units per second of push
#: The world's camera: each push goes 90 degrees off its Alola way.
WORLD_DIRS = {"CircleUp": (1, 0), "CircleRight": (0, 1),
              "CircleDown": (-1, 0), "CircleLeft": (0, -1)}
NORTH = "CircleLeft"                    # in the world, toward the hole


def in_world(p):
    x, z = p
    return ((-30 <= x <= 30 and 0 <= z <= 30)          # the arrival nook
            or (30 <= x <= 300 and 0 <= z <= 30)       # right ...
            or (270 <= x <= 300 and -300 <= z <= 0))   # ... then up


def in_altar(p):
    return 800 <= p[0] <= 1200 and 990 <= p[1] <= 1300


class Pk(SimpleNamespace):
    def __init__(self, key, species=469, shiny=False):
        super().__init__(encryption_key=key, species=species, shiny=shiny,
                         ot_name="", nature="Timid", ivs={})


class Sim:
    """The game, driven only by the bot's presses."""

    def __init__(self, ctx, wilds, trips=2, pull_early=False,
                 transition=0):
        self.ctx, self.wilds, self.trips = ctx, list(wilds), trips
        self.pull_early = pull_early
        #: Polls the "!" and the battle intro take before Battle loads.
        self.transition, self.waited = transition, 0
        self.battle_at = None           # len(events) when Battle loaded
        self.state, self.pos_ = "altar", HOME
        self.facing, self.events, self.bad = "CircleUp", [], []
        self.prompt = self.text = self.polls = 0
        self.held: set = set()
        self.records, self.done, self.rides = [], False, 0

    # -- what the bot reads ------------------------------------------
    def names(self):
        if self.state == "ride":
            self.polls += 1
            let_go = not self.held and self.polls > 2
            if let_go or (self.pull_early and self.polls > 3):
                self.state, self.pos_, self.text = "world", ARRIVAL, 2
                self.done = False
            return {wh.RIDE}
        if self.state == "intro":
            self.waited += 1
            if self.waited > self.transition:
                self._battle()
        return {"battle": {"Battle", "Bag"}}.get(self.state, {"FieldRo"})

    def _battle(self):
        self.state = "battle"
        self.records.append(self.wilds.pop(0))
        self.battle_at = len(self.events)

    def pos(self, rpc=None, route=None):
        # The player object exists from boot: through a ride or a battle
        # it still reads, with its last coordinates. Only the module list
        # tells those apart.
        return self.pos_

    def scan(self, ctx, windows, keys):
        return [(0x3002F9A0 + i, p) for i, p in enumerate(self.records)]

    # -- what the bot presses ----------------------------------------
    def hold(self, b):
        self.events.append(f"hold {b}")
        self.held.add(b)
        return True

    def release(self, b):
        self.events.append(f"release {b}")
        self.held.discard(b)

    def tap(self, b, hold_s=0.05):
        self.events.append(b)
        if b.startswith("Circle"):
            self._push(b, hold_s)
        elif b == "A":
            self._a()
        elif b == "B" and self.state == "world":
            if self.prompt:
                self.prompt = 0                # No
            elif self.text:
                self.text -= 1                 # B moves text on too
        if self.state == "battle":
            if self.events[-2:] == ["DpadDown", "A"]:
                self.state, self.text = "world", 1     # ran; an item to pick up
            elif self.events[-3:] == ["Y", "A", "A"]:
                self.state, self.text = "world", 1
                self.ctx.caught_species = self.records[-1].species

    def _push(self, b, hold_s):
        if self.prompt:
            self.bad.append(f"{b} with a prompt open ({self.state})")
            return
        self.facing = b
        if self.state == "altar":
            dx, dz = wh._DIRS[b]
            ok = in_altar
        elif self.state == "world" and not self.text:
            dx, dz = WORLD_DIRS[b]
            ok = in_world
        else:
            return
        step = SPEED * hold_s
        nxt = (self.pos_[0] + dx * step, self.pos_[1] + dz * step)
        if ok(nxt):
            self.pos_ = nxt
        if (self.state == "world" and not self.done
                and abs(self.pos_[0] - SPOT[0]) <= 30
                and abs(self.pos_[1] - SPOT[1]) <= 30):
            self.done, self.waited = True, 0
            if self.transition:
                self.state = "intro"    # the player frozen under the "!"
            else:
                self._battle()

    def _at_altar_hole(self):
        return (self.facing == "CircleUp" and abs(self.pos_[0] - 1000) <= 40
                and self.pos_[1] <= 990 + 60)

    def _at_world_hole(self):
        return (self.facing == NORTH and abs(self.pos_[0]) <= 30
                and self.pos_[1] <= 0 + 60)

    def _a(self):
        if self.state == "altar" and self._at_altar_hole():
            self.prompt += 1
            if self.prompt == 3:                       # prompt, menu, top entry
                if self.rides == self.trips:
                    self.ctx.request_stop("enough trips")
                    return
                self.state, self.prompt, self.polls = "ride", 0, 0
                self.rides += 1
        elif self.state == "world":
            if self.text:
                self.text -= 1
            elif self._at_world_hole():
                self.prompt += 1
                if self.prompt == 2:                   # the question, Yes
                    self.state, self.pos_, self.prompt = "altar", SPAWN, 0
                    self.facing = "CircleUp"


class Event:
    """threading.Event without the waiting: every wait in the bot is
    instant here, and time is the counted clock below."""

    def __init__(self):
        self.flag = False

    def set(self):
        self.flag = True

    def is_set(self):
        return self.flag

    def wait(self, timeout=None):
        return self.flag


class Ctx:
    def __init__(self, config=None):
        self.config = config or {}
        self.game = SimpleNamespace(generation=7, key="USUM-USA-1.2",
                                    offsets=None)
        self.target = None
        self.rpc = None
        self._stop_evt = Event()
        self.stop_reason = None
        self.sent = []
        self.caught_species = None
        self.dashboard = SimpleNamespace(
            broadcast=lambda kind, **f: self.sent.append((kind, f)))

    def should_stop(self):
        return self._stop_evt.is_set()

    def request_stop(self, reason=""):
        self.stop_reason = self.stop_reason or reason
        self._stop_evt.set()


FAST = wh.WormholePlan(
    fly=0.0, poll=0.0, altar_timeout=2, ride_timeout=2, world_timeout=2,
    walk_timeout=5, return_timeout=0.2, prompt_gap=0.0,
    battle=g7.WigglePlan(find_timeout=0.5, flee_timeout=2, settle_taps=1,
                         dismiss_gap=0.0, throw_gap=0.0))


def hunt(monkeypatch, wilds, plan=FAST, hide_records=False, **kw):
    ctx = Ctx()
    sim = Sim(ctx, wilds, **kw)
    ctx.input = sim
    if hide_records:
        sim.scan = lambda ctx, windows, keys: []
    monkeypatch.setattr(wh.WormholePlan, "from_config",
                        classmethod(lambda cls, cfg: plan))
    monkeypatch.setattr(g7.Modules, "names", lambda self: sim.names())
    monkeypatch.setattr(wh, "read_xz", sim.pos)
    monkeypatch.setattr(wh, "scan_windows", sim.scan)
    monkeypatch.setattr(g7, "scan_windows", sim.scan)
    monkeypatch.setattr(wh, "foe_windows", lambda o: [])
    monkeypatch.setattr(wh, "ensure_targets_dir", lambda: None)
    monkeypatch.setattr(wh, "_focus_if_needed", lambda c: None)
    monkeypatch.setattr(wh, "_balls_left", lambda c: None)
    monkeypatch.setattr(g7, "_balls_left", lambda c: None)
    reported = []
    monkeypatch.setattr(wh, "_report_encounter",
                        lambda c, p, a, n, via: reported.append(p.encryption_key))
    clock = SimpleNamespace(monotonic=_clock())
    monkeypatch.setattr(wh, "time", clock)
    monkeypatch.setattr(g7, "time", clock)
    wh.run(ctx)
    return ctx, sim, reported


def _clock():
    """Each read of the clock is 10 ms later: timeouts are counted in
    steps, so the tests take no wall time and are not timing-sensitive."""
    t = [0.0]

    def tick():
        t[0] += 0.01
        return t[0]
    return tick


def test_two_trips_run_from_the_first_and_find_the_second_straight(monkeypatch):
    ctx, sim, reported = hunt(monkeypatch, [Pk(1), Pk(2)], trips=2)
    assert reported == [1, 2]
    assert ctx.stop_reason == "enough trips"
    assert sim.bad == []
    assert sim.rides == 2 and sim.state == "altar"


def test_the_ride_holds_the_corner_and_lets_go(monkeypatch):
    ctx, sim, _ = hunt(monkeypatch, [Pk(1)], trips=1)
    holds = [e for e in sim.events if e.startswith(("hold", "release"))]
    assert holds == ["hold CircleDown", "hold CircleLeft",
                     "release CircleDown", "release CircleLeft"]


def test_the_altar_gets_only_a_once_the_hole_is_faced(monkeypatch):
    """Up or Down on the altar's menu would pick Quit or Megalopolis."""
    ctx, sim, _ = hunt(monkeypatch, [Pk(1)], trips=1)
    assert not [b for b in sim.bad if "altar" in b]


def test_a_shiny_world_pokemon_is_caught_and_the_hunt_goes_on(monkeypatch):
    ctx, sim, reported = hunt(monkeypatch, [Pk(1), Pk(2, shiny=True)], trips=2)
    assert reported == [1, 2]
    assert ctx.caught_species == 469
    assert [k for k, _ in ctx.sent].count("target_caught") == 1
    assert ctx.stop_reason == "enough trips"


def test_a_shiny_legendary_is_left_on_screen(monkeypatch):
    ctx, sim, _ = hunt(monkeypatch, [Pk(1, species=150, shiny=True)], trips=1)
    assert ctx.stop_reason == "wormhole: shiny on screen"
    assert sim.state == "battle"
    assert len(sim.events) == sim.battle_at     # nothing pressed after it


def test_a_battle_still_loading_when_the_search_gives_up_is_not_pressed_into(
        monkeypatch, soon):
    """The search times out under the "!", the battle loads a moment
    later: it must be read, never walked back from with A, A."""
    plan = wh.WormholePlan(**{**FAST.__dict__, "walk_timeout": 0.5})
    ctx, sim, _ = hunt(monkeypatch, [Pk(1, species=150, shiny=True)],
                       plan=plan, trips=1, transition=60)
    assert ctx.stop_reason == "wormhole: shiny on screen"
    assert sim.battle_at is not None
    assert len(sim.events) == sim.battle_at
    assert soon == [True]                       # the search had given up


@pytest.fixture
def soon(monkeypatch):
    calls = []
    real = wh._battle_soon

    def spy(*a, **k):
        calls.append(real(*a, **k))
        return calls[-1]
    monkeypatch.setattr(wh, "_battle_soon", spy)
    return calls


def test_a_battle_whose_wild_cannot_be_read_is_left_alone(monkeypatch):
    ctx, sim, reported = hunt(monkeypatch, [Pk(1)], trips=1, hide_records=True)
    assert ctx.stop_reason == "wormhole: wild not read"
    assert len(sim.events) == sim.battle_at


def test_on_target_stop_leaves_a_shiny_world_pokemon_too(monkeypatch):
    plan = wh.WormholePlan(**{**FAST.__dict__, "catch": False})
    ctx, sim, _ = hunt(monkeypatch, [Pk(1, shiny=True)], plan=plan, trips=1)
    assert ctx.stop_reason == "wormhole: shiny on screen"
    assert ctx.caught_species is None


def test_a_hole_that_pulls_early_still_works(monkeypatch):
    ctx, sim, reported = hunt(monkeypatch, [Pk(1)], trips=1, pull_early=True,
                              plan=wh.WormholePlan(**{**FAST.__dict__,
                                                      "fly": 999.0}))
    assert reported == [1]
    assert sim.held == set()                    # nothing left held down


def test_the_second_visit_remembers_the_path_and_the_hole(monkeypatch):
    ctx, sim, _ = hunt(monkeypatch, [Pk(1), Pk(2)], trips=2)
    first = sim.events.index("hold CircleDown", 1)
    trip2 = sim.events[sim.events.index("hold CircleDown", first + 1):]
    trip1 = sim.events[first:sim.events.index("hold CircleDown", first + 1)]
    pushes = lambda ev: sum(1 for e in ev if e.startswith("Circle"))
    assert pushes(trip2) < pushes(trip1)


def test_not_ultra_sun_ultra_moon_stops_at_once(monkeypatch):
    ctx = Ctx()
    ctx.game = SimpleNamespace(generation=7, key="SM-USA-1.2", offsets=None)
    ctx.input = SimpleNamespace(tap=lambda *a, **k: pytest.fail("pressed"))
    wh.run(ctx)
    assert ctx.stop_reason == "wormhole: not Ultra Sun / Ultra Moon"


def test_starting_inside_a_ride_or_battle_presses_nothing(monkeypatch):
    for state in ("ride", "battle"):
        ctx = Ctx()
        sim = Sim(ctx, [])      # its position reads: only the modules tell
        sim.state = state
        ctx.input = sim
        monkeypatch.setattr(g7.Modules, "names", lambda self: sim.names())
        monkeypatch.setattr(wh, "read_xz", sim.pos)
        monkeypatch.setattr(wh, "ensure_targets_dir", lambda: None)
        monkeypatch.setattr(wh, "_focus_if_needed", lambda c: None)
        wh.run(ctx)
        assert ctx.stop_reason == "wormhole: not at the altar"
        assert sim.events == []


def test_config_reads_fly_corner_and_on_target():
    p = wh.WormholePlan.from_config({"fly_seconds": "90", "corner": "UP_RIGHT",
                                     "on_target": "stop"})
    assert (p.fly, p.corner, p.catch) == (90.0, ("CircleUp", "CircleRight"),
                                          False)
    d = wh.WormholePlan.from_config({"fly_seconds": "soon", "corner": "nope"})
    assert (d.fly, d.corner, d.catch) == (150.0, wh.CORNERS["down_left"], True)


def test_the_twenty_world_species():
    assert len(wh.WORLD_SPECIES) == 20
    assert {195, 469, 558, 695} <= wh.WORLD_SPECIES
    assert not {150, 793, 799} & wh.WORLD_SPECIES   # Mewtwo, Nihilego, Guzzlord
