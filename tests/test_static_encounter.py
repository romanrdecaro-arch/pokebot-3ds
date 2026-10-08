"""
The ORAS static-encounter soft reset.

Two promises, both from the request:

* the button presses are X/Y's static hunt, unchanged;
* on a shiny, STOP ALL ACTIONS -- no catch sequence, nothing after.

The second is the one worth being exact about, so the fake game starts
the battle on ITS clock rather than as a side effect of the bot's read
(an earlier fake did that and hid a whole class of bug), and every
input is timestamped against the moment the wild appeared.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot.modes import soft_reset as sr  # noqa: E402
from pokebot.modes import static_encounter as se  # noqa: E402

FOE = 0x08800000
SLOT = FOE + 0x3ECC


class Mon:
    _n = 0

    def __init__(self, species=382, shiny=False, key=None):
        Mon._n += 1
        self.species = species
        self.shiny = shiny
        self.encryption_key = key if key is not None else 0x7000 + Mon._n
        self.pid = 0x1000 + Mon._n
        self.nickname = ""
        self.nature = "Modest"
        self.ivs = {k: 31 for k in ("HP", "Atk", "Def", "SpA", "SpD",
                                    "Spe")}


class Game:
    """A save in front of a legendary.

    The ``presses``-th A since the last reset starts the battle, and
    the wild appears ``delay`` seconds later -- the encounter cutscene.
    """

    def __init__(self, encounters, presses=2, delay=0.04):
        self.queue = list(encounters)
        self.presses_needed = presses
        self.delay = delay
        self.presses = 0
        self.spawn_at = None
        self.wild = None
        self.spawned_t = None
        self.inputs: list[tuple[float, str]] = []
        self.resets = 0

    def _tick(self):
        if (self.wild is None and self.spawn_at is not None
                and time.monotonic() >= self.spawn_at and self.queue):
            self.wild = self.queue.pop(0)
            self.spawned_t = time.monotonic()

    # controller
    def tap(self, button, hold_s=0.05):
        self._tick()
        self.inputs.append((time.monotonic(), button))
        if button == "A" and self.wild is None and self.spawn_at is None:
            self.presses += 1
            if self.presses >= self.presses_needed:
                self.spawn_at = time.monotonic() + self.delay
        return "postmessage"

    def tap_touch(self, *a, **k):
        self.inputs.append((time.monotonic(), "TOUCH"))
        return True

    def soft_reset(self, hold_s=0.5):
        self.inputs.append((time.monotonic(), "RESET"))
        self.resets += 1
        self.wild = None
        self.spawn_at = None
        self.spawned_t = None
        self.presses = 0

    # memory
    def foe(self):
        self._tick()
        return [(SLOT, self.wild)] if self.wild else []

    def inputs_after_spawn(self):
        if self.spawned_t is None:
            return []
        return [b for t, b in self.inputs if t > self.spawned_t]


class Ctx:
    def __init__(self, game, target=None, seconds=10.0):
        self.input = game
        self.target = target
        self.rpc = object()
        self.config = {"soft_reset": dict(FAST)}
        self._stop_evt = threading.Event()   # REAL waits: timing matters
        self.events: list[tuple] = []
        self.ran_away = False
        self._deadline = time.monotonic() + seconds

        class Off:
            foe_base = FOE
            foe_scan_len = 0x20000
            party_base = 0x08C814AC
            party_stride = 260

        class G:
            offsets = Off()
            key = "OR-USA"

        self.game = G()

        class Dash:
            @staticmethod
            def broadcast(kind, **kw):
                self.events.append((kind, kw))

        self.dashboard = Dash()

    def should_stop(self):
        if self._stop_evt.is_set():
            return True
        if time.monotonic() > self._deadline:
            self.ran_away = True
            return True
        return False

    def request_stop(self, reason=""):
        self._stop_evt.set()

    def kinds(self):
        return [k for k, _ in self.events]


FAST = {"trainer_name": "Roman", "static_a_gap": 0.03,
        "static_a_max": 60, "post_reset_wait": 0.0,
        "post_reset_taps": 2, "post_reset_gap": 0.0}


@pytest.fixture
def wired(monkeypatch):
    saved = []

    def install(game):
        monkeypatch.setattr(se, "ensure_targets_dir", lambda: None)
        monkeypatch.setattr(se, "focus_azahar", lambda: None)
        monkeypatch.setattr(sr, "focus_azahar", lambda: None)
        monkeypatch.setattr(se, "get_party", lambda *a, **k: [])
        monkeypatch.setattr(se, "broadcast_party", lambda ctx, p: set())
        monkeypatch.setattr(se, "scan_nonparty",
                            lambda ctx, b, n, keys: game.foe())
        monkeypatch.setattr(se, "_report_encounter", lambda *a, **k: None)
        monkeypatch.setattr(se, "save_target_pk6",
                            lambda ctx, a, p, lbl: saved.append(p.species))

        import pokebot.modes.catch as catch_mod

        def no_catch(*a, **k):
            raise AssertionError("the catch sequence ran -- it must not")

        monkeypatch.setattr(catch_mod, "catch_wild", no_catch)
        return game

    install.saved = saved          # type: ignore[attr-defined]
    return install


def run(ctx):
    se.run(ctx)
    assert not ctx.ran_away, "the hunt never stopped on its own"


# ----------------------------------------------------------------------
# Stop all actions on a shiny
# ----------------------------------------------------------------------
def test_a_shiny_stops_the_hunt(wired):
    game = wired(Game([Mon(shiny=True)]))
    ctx = Ctx(game)
    run(ctx)

    assert "target_hit" in ctx.kinds()
    assert game.resets == 0, "reset away a shiny"


def test_no_input_of_any_kind_after_the_shiny_appears(wired):
    """"Stop all actions": no A, no reset, no touch, nothing."""
    game = wired(Game([Mon(shiny=True)], presses=3, delay=0.05))
    ctx = Ctx(game)
    run(ctx)

    after = game.inputs_after_spawn()
    assert after == [], f"sent {after} after the shiny appeared"


def test_there_is_no_catch_sequence(wired):
    """catch_wild raises in this fixture; reaching it fails the run."""
    game = wired(Game([Mon(shiny=True)]))
    run(Ctx(game))
    assert not [b for _, b in game.inputs if b == "TOUCH"], (
        "touched the screen -- that is the bag/ball sequence")


def test_the_shiny_is_saved_to_targets(wired):
    """A read, not an action: it does not touch the controller."""
    game = wired(Game([Mon(species=381, shiny=True)]))
    run(Ctx(game))
    assert wired.saved == [381]


# ----------------------------------------------------------------------
# Not shiny: reset
# ----------------------------------------------------------------------
def test_a_plain_encounter_is_reset(wired):
    game = wired(Game([Mon(), Mon(), Mon(shiny=True)]))
    ctx = Ctx(game)
    run(ctx)

    assert game.resets == 2
    assert "target_hit" in ctx.kinds()


def test_no_a_press_lands_on_a_battle_across_many_attempts(wired):
    """Each wild must be noticed before the next press, not after."""
    game = wired(Game([Mon() for _ in range(5)] + [Mon(shiny=True)],
                      presses=2, delay=0.045))
    ctx = Ctx(game, seconds=20.0)
    landed = {"n": 0}
    real_tap = game.tap

    def tap(button, hold_s=0.05):
        game._tick()
        if game.wild is not None and button == "A":
            landed["n"] += 1
        return real_tap(button, hold_s)

    game.tap = tap                                 # type: ignore
    run(ctx)

    assert game.resets == 5
    assert landed["n"] == 0, f"{landed['n']} A press(es) hit a battle"


def test_no_encounter_resets_and_tries_again(wired):
    game = wired(Game([Mon(shiny=True)], presses=10 ** 9))
    ctx = Ctx(game)
    ctx.config["soft_reset"]["static_a_max"] = 3
    reset = game.soft_reset

    def soft_reset(hold_s=0.5):
        reset(hold_s)
        game.presses_needed = 1          # works after the reset

    game.soft_reset = soft_reset                   # type: ignore
    run(ctx)

    assert game.resets == 1
    assert "target_hit" in ctx.kinds()


def test_a_leftover_wild_is_not_this_attempts_encounter(wired):
    """The foe window keeps the last wild. A stale SHINY there must not
    stop a hunt that has not pressed anything yet."""
    stale = Mon(shiny=True, key=0xDEAD)
    game = Game([Mon(), Mon(shiny=True)], presses=2)
    game.wild = stale
    wired(game)
    ctx = Ctx(game)

    # The stale record persists until the first reset clears it.
    run(ctx)

    hits = [kw for k, kw in ctx.events if k == "target_hit"]
    assert hits and game.resets >= 1, "stopped on the leftover shiny"


# ----------------------------------------------------------------------
# The X/Y presses, unchanged
# ----------------------------------------------------------------------
def test_only_a_is_pressed_before_the_encounter(wired):
    game = wired(Game([Mon(shiny=True)], presses=4))
    run(Ctx(game))
    assert {b for _, b in game.inputs} == {"A"}


def test_the_press_gap_is_the_xy_one_by_default():
    """0.4 s and 60 presses, the Snorlax hunt's values."""
    src = (REPO / "pokebot" / "modes" / "static_encounter.py").read_text(
        encoding="utf-8")
    assert 'cfg.get("snorlax_a_gap", 0.4)' in src
    assert 'cfg.get("snorlax_a_max", 60)' in src


def test_a_tuned_snorlax_gap_carries_over(wired):
    """Someone who tuned the X/Y hunt gets the same timing here."""
    game = wired(Game([Mon(shiny=True)], presses=3))
    ctx = Ctx(game)
    cfg = ctx.config["soft_reset"]
    del cfg["static_a_gap"]
    cfg["snorlax_a_gap"] = 0.05
    run(ctx)

    a_times = [t for t, b in game.inputs if b == "A"]
    gaps = [b - a for a, b in zip(a_times, a_times[1:])]
    assert gaps and min(gaps) >= 0.04, f"gaps {gaps}"


def test_it_uses_the_xy_reset(wired):
    """Same L+R+Start and post-reset A taps as X/Y."""
    src = (REPO / "pokebot" / "modes" / "static_encounter.py").read_text(
        encoding="utf-8")
    assert "from .soft_reset import _do_reset" in src


# ----------------------------------------------------------------------
# Registration
# ----------------------------------------------------------------------
def test_the_mode_is_registered():
    from pokebot.modes import MODES

    assert "static_encounter" in MODES


def test_the_launcher_passes_the_trainer_name():
    """The party's own keys are excluded from wild detection by OT."""
    src = (REPO / "launcher.py").read_text(encoding="utf-8")
    assert '("soft_reset", "gifts", "static_encounter")' in src
