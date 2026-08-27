"""
Tests for the X/Y starter soft-reset loop.

The mode used to drive a fixed, hand-timed sequence: DpadLeft to start
the cutscene, 25 A presses on one-second gaps, two cursor presses to
pick a starter, then up to 30 B presses to receive it. Every step
assumed the cutscene was where the timings said it would be, and a
cursor misfire meant receiving the wrong starter and burning the
attempt.

It is now the same shape as the Celebi hunt: press A fast, watch the
party, stop the moment something lands. The tests below pin the parts
that are easy to regress — that no directional press is ever sent, that
the pressing stops on detection rather than running into the nickname
prompt, and that a reset which silently did nothing halts the hunt
instead of spamming A into whatever is still on screen.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot.modes import soft_reset as sr        # noqa: E402


class FakeInput:
    def __init__(self):
        self.events = []

    def tap(self, button, hold_s=0.05):
        self.events.append(button)

    def soft_reset(self, hold_s=0.5):
        self.events.append("RESET")

    def gb_soft_reset(self, hold_s=0.6):
        self.events.append("GB-RESET")


class FakeDashboard:
    def __init__(self):
        self.sent = []

    def broadcast(self, kind, **payload):
        self.sent.append((kind, payload))

    def kinds(self):
        return [k for k, _ in self.sent]


class FakeOffsets:
    party_base = 0x08C00000
    party_stride = 484


class FakeGame:
    key = "X-USA"
    offsets = FakeOffsets()


class FakeTarget:
    """A shiny filter.

    Not optional detail: with no target rules the mode treats ANY
    correct-species starter as a hit and stops on attempt one, which is
    right for "I just want a Chespin" and useless for testing a loop
    that is supposed to reset.
    """
    rules = [{"shiny": True}]

    def matches(self, pkm):
        return bool(pkm.shiny)

    def describe(self, pkm):
        return "shiny starter"


class FakeCtx:
    def __init__(self, cfg=None):
        self.game = FakeGame()
        self.input = FakeInput()
        self.dashboard = FakeDashboard()
        self._stop_evt = threading.Event()
        self.stop_reason = None
        self.target = FakeTarget()
        self.config = {"soft_reset": cfg or {}}

    def should_stop(self):
        return self._stop_evt.is_set()

    def request_stop(self, reason=""):
        self.stop_reason = reason
        self._stop_evt.set()


class FakePkm:
    """The fields _run_starters reads off a received starter."""

    def __init__(self, species=650, shiny=False):
        self.species = species
        self.shiny = shiny
        self.nickname = ""
        self.nature = "Adamant"
        self.nature_id = 3
        self.gender = "M"
        self.ivs = {"HP": 5, "Atk": 6, "Def": 7,
                    "Spe": 8, "SpA": 9, "SpD": 10}
        self.pid = 0x12345678
        self.tsv = 100
        self.psv = 200
        self.ability_id = 65
        self.ability_num = 1
        self.moves = []
        self.party = {"level": 5}
        self.encryption_key = 0xDEADBEEF
        self.source_address = 0x08C01000


class World:
    """The game state the mode is really talking to.

    ``party`` fills after ``presses_to_receive`` A presses and empties
    on a reset — unless ``reset_works`` is False, which models a combo
    that never landed.
    """

    def __init__(self, presses_to_receive=5, species=650, shiny=False,
                 reset_works=True, start_with_mon=False):
        self.presses = 0
        self.presses_to_receive = presses_to_receive
        self.species = species
        self.shiny = shiny
        self.reset_works = reset_works
        self.party = [FakePkm(species, shiny)] if start_with_mon else []
        self.resets = 0

    def press(self, button):
        if button == "A":
            self.presses += 1
            if self.presses >= self.presses_to_receive and not self.party:
                self.party = [FakePkm(self.species, self.shiny)]

    def reset(self):
        self.resets += 1
        self.presses = 0
        if self.reset_works:
            self.party = []


def _wire(monkeypatch, world, ctx):
    """Point the mode's collaborators at the fake world."""
    real_tap = ctx.input.tap
    real_reset = ctx.input.soft_reset

    def tap(button, hold_s=0.05):
        real_tap(button, hold_s)
        world.press(button)

    def reset(hold_s=0.5):
        real_reset(hold_s)
        world.reset()

    ctx.input.tap = tap
    ctx.input.soft_reset = reset

    monkeypatch.setattr(sr, "quick_get_party", lambda c, ot: list(world.party))
    monkeypatch.setattr(sr, "get_party",
                        lambda c, b, s, ot, contiguous=True: list(world.party))
    monkeypatch.setattr(sr, "broadcast_party", lambda c, p: None)
    monkeypatch.setattr(sr, "save_target_pk6",
                        lambda c, a, p, tag: None)
    monkeypatch.setattr(sr, "focus_azahar", lambda: True)
    monkeypatch.setattr(sr, "starters_for",
                        lambda key: {"chespin": 650, "fennekin": 653,
                                     "froakie": 656})
    monkeypatch.setattr(sr, "starter_species",
                        lambda key, name: {"chespin": 650, "fennekin": 653,
                                           "froakie": 656}.get(name.lower()))


def _cfg(**over):
    base = {"press_hold": 0.0, "press_interval": 0.0, "detect_every": 0.0,
            "receive_timeout": 2.0, "reset_timeout": 1.0,
            "post_reset_wait": 0.0, "detect_tries": 2, "detect_gap": 0.0,
            "trainer_name": "ROMAN"}
    base.update(over)
    return base


def _run(world, monkeypatch, **over):
    ctx = FakeCtx(_cfg(**over))
    _wire(monkeypatch, world, ctx)
    sr._run_starters(ctx, ctx.config["soft_reset"])
    return ctx


# --------------------------------------------------------------------
# The explicit ask: no directional presses
# --------------------------------------------------------------------

def test_no_directional_presses_are_ever_sent(monkeypatch):
    """The cursor navigation is gone. A only."""
    world = World(presses_to_receive=4, shiny=True)
    ctx = _run(world, monkeypatch, starter="chespin")
    directions = [e for e in ctx.input.events if "Dpad" in e or "Circle" in e]
    assert directions == [], f"still pressing {directions}"


def test_only_a_is_pressed_during_an_attempt(monkeypatch):
    """Not B either — the old sequence mashed B to receive."""
    world = World(presses_to_receive=4, shiny=True)
    ctx = _run(world, monkeypatch, starter="chespin")
    assert set(ctx.input.events) <= {"A", "RESET"}, set(ctx.input.events)


def test_the_cursor_sequence_helper_is_gone() -> None:
    """The fixed hand-timed sequence should not survive as dead code."""
    assert not hasattr(sr, "_xy_starter_sequence")
    assert not hasattr(sr, "_SEQUENCES")


# --------------------------------------------------------------------
# Press → detect → stop
# --------------------------------------------------------------------

def test_pressing_stops_as_soon_as_the_starter_lands(monkeypatch):
    """Overshoot is what runs the spam into the nickname prompt."""
    world = World(presses_to_receive=5, shiny=True)
    ctx = _run(world, monkeypatch, starter="chespin")
    presses = ctx.input.events.count("A")
    assert 5 <= presses <= 8, (
        f"{presses} A presses for a starter that arrives on the 5th")


def test_a_target_stops_the_hunt(monkeypatch):
    world = World(presses_to_receive=3, shiny=True)
    ctx = _run(world, monkeypatch, starter="chespin")
    assert ctx.stop_reason == "target hit"
    assert "target_hit" in ctx.dashboard.kinds()
    assert "RESET" not in ctx.input.events, "reset past a target"


def test_a_miss_resets_and_tries_again(monkeypatch):
    """Not a target -> reset -> press again, and the party must empty."""
    world = World(presses_to_receive=3, species=650, shiny=False)

    ctx = FakeCtx(_cfg(starter="chespin"))
    _wire(monkeypatch, world, ctx)

    # Third attempt is the shiny; stop there so the loop terminates.
    real_reset = ctx.input.soft_reset

    def reset(hold_s=0.5):
        real_reset(hold_s)
        if world.resets >= 2:
            world.shiny = True

    ctx.input.soft_reset = reset
    sr._run_starters(ctx, ctx.config["soft_reset"])

    assert world.resets == 2, f"{world.resets} resets"
    assert ctx.stop_reason == "target hit"


# --------------------------------------------------------------------
# Refusing to run blind
# --------------------------------------------------------------------

def test_a_party_that_is_not_empty_at_the_start_stops_the_hunt(monkeypatch):
    """Otherwise every attempt scores the mon already sitting there."""
    world = World(start_with_mon=True)
    ctx = _run(world, monkeypatch, starter="chespin")
    assert ctx.stop_reason == "party not empty at start"
    assert ctx.input.events == [], "pressed buttons with a non-empty party"


def test_a_reset_that_never_registers_stops_the_hunt(monkeypatch):
    """If L+R+Start does nothing the nickname keyboard is still up.

    Spamming A into that while waiting for a starter already in the
    party would type nonsense forever, so the party emptying is the
    proof the reset landed.
    """
    world = World(presses_to_receive=3, shiny=False, reset_works=False)
    ctx = _run(world, monkeypatch, starter="chespin")
    assert ctx.stop_reason == "soft reset did not register"
    assert "read_failure" in ctx.dashboard.kinds()
    assert world.resets == 1, "kept resetting after the first did nothing"


def test_nothing_is_pressed_while_waiting_for_the_reset_to_take(monkeypatch):
    world = World(presses_to_receive=3, shiny=False, reset_works=False)
    ctx = _run(world, monkeypatch, starter="chespin")
    after = ctx.input.events[ctx.input.events.index("RESET") + 1:]
    assert after == [], f"pressed {after} with the old screen still up"


def test_nothing_received_within_the_timeout_stops_with_a_reason(monkeypatch):
    """Input is delivered indirectly and may not arrive at all."""
    world = World(presses_to_receive=10 ** 9)      # never arrives
    ctx = _run(world, monkeypatch, starter="chespin", receive_timeout=0.3)
    assert ctx.stop_reason and "no starter received" in ctx.stop_reason
    assert "A" in ctx.input.events, "did not even try pressing A"
    assert "read_failure" in ctx.dashboard.kinds()


def test_the_wrong_starter_is_reported_and_reset(monkeypatch):
    """With no cursor navigation the species check is the only guard."""
    world = World(presses_to_receive=3, species=656)   # Froakie
    ctx = FakeCtx(_cfg(starter="chespin"))             # wanted Chespin
    _wire(monkeypatch, world, ctx)

    real_reset = ctx.input.soft_reset

    def reset(hold_s=0.5):
        real_reset(hold_s)
        if world.resets >= 1:
            world.species = 650
            world.shiny = True

    ctx.input.soft_reset = reset
    sr._run_starters(ctx, ctx.config["soft_reset"])

    reasons = [p.get("reason", "") for k, p in ctx.dashboard.sent
               if k == "read_failure"]
    assert any("wrong starter" in r for r in reasons), reasons
