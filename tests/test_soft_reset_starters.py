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
        #: Buttons currently held. A direction latched here at the end
        #: of a run is one latched in Azahar for real.
        self.held = set()
        self.hold_works = True

    def tap(self, button, hold_s=0.05):
        self.events.append(button)

    def hold(self, button):
        self.events.append(f"HOLD:{button}")
        if not self.hold_works:
            return False
        self.held.add(button)
        return True

    def release(self, button):
        self.events.append(f"RELEASE:{button}")
        self.held.discard(button)

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

def test_left_is_never_tapped_only_held(monkeypatch):
    """The stepwise cursor navigation is gone.

    A tap only moves the cursor if it lands while the selection is up,
    so it has to be aimed at a moment in a cutscene the bot cannot see.
    Holding needs no aim.
    """
    world = World(presses_to_receive=4, shiny=True)
    ctx = _run(world, monkeypatch, starter="chespin")
    taps = [e for e in ctx.input.events
            if "Dpad" in e and not e.startswith(("HOLD:", "RELEASE:"))]
    assert taps == [], f"still tapping directions: {taps}"


def test_only_a_is_tapped_during_an_attempt(monkeypatch):
    """Not B either — the old sequence mashed B to receive."""
    world = World(presses_to_receive=4, shiny=True)
    ctx = _run(world, monkeypatch, starter="chespin")
    tapped = {e for e in ctx.input.events
              if not e.startswith(("HOLD:", "RELEASE:")) and e != "RESET"}
    assert tapped <= {"A"}, tapped


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


def test_a_is_pressed_immediately_after_the_reset(monkeypatch):
    """No wait for the boot logos.

    The old 12-second pause did nothing on the theory that a press
    during the Nintendo/Game Freak splash is wasted — but a wasted
    press costs 30ms and the pause cost 12 seconds of every attempt.
    The reset and the next attempt are one continuous stream of A.
    """
    world = World(presses_to_receive=3, shiny=False, reset_works=False)
    ctx = _run(world, monkeypatch, starter="chespin")
    after = ctx.input.events[ctx.input.events.index("RESET") + 1:]
    assert "A" in after, f"nothing pressed after the reset: {after}"


def test_the_reset_does_not_sleep_through_the_boot_logos(monkeypatch):
    """post_reset_wait used to be 12s on every single attempt."""
    import time as _t
    world = World(presses_to_receive=2, shiny=False)
    ctx = FakeCtx(_cfg(starter="chespin", post_reset_wait=12.0))
    _wire(monkeypatch, world, ctx)
    real_reset = ctx.input.soft_reset

    def reset(hold_s=0.5):
        real_reset(hold_s)
        world.shiny = True

    ctx.input.soft_reset = reset
    t0 = _t.monotonic()
    sr._run_starters(ctx, ctx.config["soft_reset"])
    assert _t.monotonic() - t0 < 2.0, "still sleeping through the logos"


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


# --------------------------------------------------------------------
# Holding Left for the whole sequence
# --------------------------------------------------------------------

def test_left_is_held_before_the_first_a_press(monkeypatch):
    """Held for the WHOLE sequence means from before press one."""
    world = World(presses_to_receive=4, shiny=True)
    ctx = _run(world, monkeypatch, starter="chespin")
    ev = ctx.input.events
    assert "HOLD:DpadLeft" in ev, ev
    assert ev.index("HOLD:DpadLeft") < ev.index("A"), (
        f"pressed A before holding Left: {ev[:5]}")


def test_left_is_released_when_the_sequence_ends(monkeypatch):
    """A latched direction outlives the bot process.

    The player would take back a game that is still walking, and the
    next attempt would start with a key down nothing ever releases.
    """
    world = World(presses_to_receive=4, shiny=True)
    ctx = _run(world, monkeypatch, starter="chespin")
    assert ctx.input.held == set(), f"still held: {ctx.input.held}"


def test_left_is_released_even_when_the_attempt_times_out(monkeypatch):
    world = World(presses_to_receive=10 ** 9)      # never arrives
    ctx = _run(world, monkeypatch, starter="chespin", receive_timeout=0.3)
    assert ctx.input.held == set(), f"still held after a timeout: {ctx.input.held}"


def test_left_is_released_before_the_reset(monkeypatch):
    """L+R+Start with a direction still latched is a different combo."""
    world = World(presses_to_receive=3, shiny=False)
    ctx = FakeCtx(_cfg(starter="chespin"))
    _wire(monkeypatch, world, ctx)
    real_reset = ctx.input.soft_reset

    def reset(hold_s=0.5):
        assert ctx.input.held == set(), (
            f"reset with {ctx.input.held} still held")
        real_reset(hold_s)
        world.shiny = True

    ctx.input.soft_reset = reset
    sr._run_starters(ctx, ctx.config["soft_reset"])
    assert world.resets == 1


def test_left_is_re_held_for_every_attempt(monkeypatch):
    """Each attempt is its own sequence, so each gets its own hold."""
    world = World(presses_to_receive=3, shiny=False)
    ctx = FakeCtx(_cfg(starter="chespin"))
    _wire(monkeypatch, world, ctx)
    real_reset = ctx.input.soft_reset

    def reset(hold_s=0.5):
        real_reset(hold_s)
        if world.resets >= 2:
            world.shiny = True

    ctx.input.soft_reset = reset
    sr._run_starters(ctx, ctx.config["soft_reset"])
    assert ctx.input.events.count("HOLD:DpadLeft") == 3
    assert ctx.input.events.count("RELEASE:DpadLeft") == 3


def test_a_hold_that_fails_warns_but_still_runs(monkeypatch):
    """Losing the hold should not abandon an otherwise working attempt."""
    world = World(presses_to_receive=4, shiny=True)
    ctx = FakeCtx(_cfg(starter="chespin"))
    _wire(monkeypatch, world, ctx)
    ctx.input.hold_works = False
    sr._run_starters(ctx, ctx.config["soft_reset"])
    assert ctx.stop_reason == "target hit"
    assert "A" in ctx.input.events


def test_the_hold_button_is_configurable(monkeypatch):
    """Azahar binds the Circle Pad to different keys than the D-pad."""
    world = World(presses_to_receive=4, shiny=True)
    ctx = _run(world, monkeypatch, starter="chespin",
               hold_button="CircleLeft")
    assert "HOLD:CircleLeft" in ctx.input.events
    assert "HOLD:DpadLeft" not in ctx.input.events


def test_the_hold_can_be_turned_off(monkeypatch):
    world = World(presses_to_receive=4, shiny=True)
    ctx = _run(world, monkeypatch, starter="chespin", hold_button="")
    assert not any(e.startswith("HOLD:") for e in ctx.input.events)


def test_a_bogus_hold_button_is_rejected_before_pressing_anything(monkeypatch):
    """Better than a ValueError from deep inside the driver mid-hunt."""
    world = World(presses_to_receive=4, shiny=True)
    ctx = _run(world, monkeypatch, starter="chespin", hold_button="Leftish")
    assert ctx.stop_reason and "hold_button" in ctx.stop_reason
    assert ctx.input.events == []


# --------------------------------------------------------------------
# The driver's hold/release, which the sequence above depends on
# --------------------------------------------------------------------

def _driver(monkeypatch, posted):
    """An InputDriver whose PostMessage calls land in ``posted``."""
    from pokebot import input_driver as idrv

    drv = idrv.InputDriver.__new__(idrv.InputDriver)
    drv.binds = idrv.KeyBinds()
    drv.dry_run = False
    drv._kb = None
    drv._azahar_hwnd = 4242
    drv._postmsg_warned = False
    drv._held_keys = set()
    drv._held_vks = set()

    monkeypatch.setattr(idrv.sys, "platform", "win32")
    monkeypatch.setattr(
        "pokebot.platform_utils.find_azahar_hwnd", lambda: 4242)
    monkeypatch.setattr(
        "pokebot.platform_utils.char_to_vk", lambda c: ord(c[:1].upper()))
    monkeypatch.setattr(
        "pokebot.platform_utils.post_key_down",
        lambda h, vk: posted.append(("down", h, vk)) or True)
    monkeypatch.setattr(
        "pokebot.platform_utils.post_key_up",
        lambda h, vk: posted.append(("up", h, vk)) or True)
    return drv


def test_driver_hold_posts_a_keydown_with_no_keyup(monkeypatch):
    """That asymmetry IS the hold — Qt reads key state from the events."""
    posted = []
    drv = _driver(monkeypatch, posted)
    assert drv.hold("DpadLeft") is True
    assert [k for k, _, _ in posted] == ["down"]
    assert drv._held_vks == {ord("F")}          # DpadLeft binds to 'f'


def test_driver_release_posts_the_matching_keyup(monkeypatch):
    posted = []
    drv = _driver(monkeypatch, posted)
    drv.hold("DpadLeft")
    drv.release("DpadLeft")
    assert [k for k, _, _ in posted] == ["down", "up"]
    assert drv._held_vks == set()


def test_driver_release_is_safe_to_call_twice(monkeypatch):
    """The sequence releases in a finally, which can double up."""
    posted = []
    drv = _driver(monkeypatch, posted)
    drv.hold("DpadLeft")
    drv.release("DpadLeft")
    drv.release("DpadLeft")
    assert [k for k, _, _ in posted] == ["down", "up"]


def test_driver_close_releases_a_still_held_direction(monkeypatch):
    """A latched key outlives the process and leaves Azahar walking."""
    posted = []
    drv = _driver(monkeypatch, posted)
    drv.hold("DpadLeft")
    drv.close()
    assert ("up", 4242, ord("F")) in posted
    assert drv._held_vks == set()


def test_driver_hold_rejects_an_unknown_button(monkeypatch):
    import pytest
    posted = []
    drv = _driver(monkeypatch, posted)
    with pytest.raises(ValueError):
        drv.hold("Leftish")
    assert posted == []


# --------------------------------------------------------------------
# Recently Seen: the table must show what actually arrived
# --------------------------------------------------------------------

def test_the_wrong_starter_still_reaches_the_table(monkeypatch):
    """Reported: Recently Seen sat empty through a whole run.

    Every attempt was finding a real Fennekin and rejecting it as the
    wrong starter, and the rejection path broadcast only read_failure —
    which populates nothing. An empty table reads as "the UI is broken"
    rather than "the cursor is not moving", which is exactly the wrong
    thing to be looking at.
    """
    world = World(presses_to_receive=3, species=656)   # Froakie
    ctx = FakeCtx(_cfg(starter="chespin"))             # wanted Chespin
    _wire(monkeypatch, world, ctx)
    real_reset = ctx.input.soft_reset

    def reset(hold_s=0.5):
        real_reset(hold_s)
        world.species = 650
        world.shiny = True

    ctx.input.soft_reset = reset
    sr._run_starters(ctx, ctx.config["soft_reset"])

    candidates = [p for k, p in ctx.dashboard.sent if k == "candidate"]
    assert len(candidates) == 2, (
        f"{len(candidates)} candidate rows for 2 received starters")
    assert candidates[0]["species"] == 656, "the rejected one is missing"
    assert candidates[1]["species"] == 650


def test_every_attempt_produces_exactly_one_table_row(monkeypatch):
    """Rows should track attempts, whatever the species gate decides."""
    world = World(presses_to_receive=2, species=653)   # always Fennekin
    ctx = FakeCtx(_cfg(starter="chespin"))
    _wire(monkeypatch, world, ctx)
    real_reset = ctx.input.soft_reset

    def reset(hold_s=0.5):
        real_reset(hold_s)
        if world.resets >= 3:
            ctx.request_stop("enough")

    ctx.input.soft_reset = reset
    sr._run_starters(ctx, ctx.config["soft_reset"])

    attempts = sum(1 for k, _ in ctx.dashboard.sent
                   if k == "soft_reset_attempt")
    candidates = sum(1 for k, _ in ctx.dashboard.sent if k == "candidate")
    assert candidates == attempts, f"{candidates} rows for {attempts} attempts"


# --------------------------------------------------------------------
# Never reset away a target over a species mismatch
# --------------------------------------------------------------------

def test_a_shiny_of_the_wrong_species_is_kept_not_reset(monkeypatch):
    """The one outcome the whole hunt exists to find.

    The species gate used to run before the target check, so a hunt
    configured for Chespin would soft-reset a shiny Fennekin — throwing
    away a 1-in-4096 roll over a mismatch PKHeX fixes in ten seconds.
    Species is a preference; a shiny is the point.
    """
    world = World(presses_to_receive=3, species=653, shiny=True)  # Fennekin
    ctx = FakeCtx(_cfg(starter="chespin"))                        # wanted 650
    _wire(monkeypatch, world, ctx)

    # Bound the loop. Resetting past this shiny is the bug under test,
    # and the world keeps producing shiny Fennekin, so without a bound
    # the regression hangs the suite instead of failing it.
    real_reset = ctx.input.soft_reset

    def reset(hold_s=0.5):
        real_reset(hold_s)
        ctx.request_stop("reset past the shiny")

    ctx.input.soft_reset = reset
    sr._run_starters(ctx, ctx.config["soft_reset"])

    assert ctx.stop_reason == "target hit", (
        f"stopped with {ctx.stop_reason!r} — a shiny was reset away")
    assert world.resets == 0, f"{world.resets} resets past a shiny"
    hits = [p for k, p in ctx.dashboard.sent if k == "target_hit"]
    assert hits and hits[0]["species"] == 653


def test_the_kept_wrong_species_shiny_is_still_flagged(monkeypatch, caplog):
    """Keeping it silently would look like the cursor had worked."""
    import logging
    world = World(presses_to_receive=3, species=653, shiny=True)
    ctx = FakeCtx(_cfg(starter="chespin"))
    _wire(monkeypatch, world, ctx)
    # Bounded for the same reason as the test above: the world keeps
    # producing shiny Fennekin, so a regression that resets past it
    # would spin forever rather than report.
    real_reset = ctx.input.soft_reset

    def reset(hold_s=0.5):
        real_reset(hold_s)
        ctx.request_stop("reset past the shiny")

    ctx.input.soft_reset = reset
    with caplog.at_level(logging.INFO, logger="pokebot.modes.soft_reset"):
        sr._run_starters(ctx, ctx.config["soft_reset"])
    text = caplog.text
    assert "Fennekin" in text, text
    assert "PKHeX" in text, "did not say how to fix the species"


def test_a_non_shiny_wrong_species_still_resets(monkeypatch):
    """The gate still does its job when there is nothing to lose."""
    world = World(presses_to_receive=3, species=653, shiny=False)
    ctx = FakeCtx(_cfg(starter="chespin"))
    _wire(monkeypatch, world, ctx)
    real_reset = ctx.input.soft_reset

    def reset(hold_s=0.5):
        real_reset(hold_s)
        world.species, world.shiny = 650, True

    ctx.input.soft_reset = reset
    sr._run_starters(ctx, ctx.config["soft_reset"])
    assert world.resets == 1, "did not reset a plain wrong-species starter"
    assert ctx.stop_reason == "target hit"


def test_the_right_species_but_not_a_target_still_resets(monkeypatch):
    world = World(presses_to_receive=3, species=650, shiny=False)
    ctx = FakeCtx(_cfg(starter="chespin"))
    _wire(monkeypatch, world, ctx)
    real_reset = ctx.input.soft_reset

    def reset(hold_s=0.5):
        real_reset(hold_s)
        world.shiny = True

    ctx.input.soft_reset = reset
    sr._run_starters(ctx, ctx.config["soft_reset"])
    assert world.resets == 1
    assert ctx.stop_reason == "target hit"


# --------------------------------------------------------------------
# Rate-limiting the title relaunches
# --------------------------------------------------------------------

def test_reset_cooldown_spaces_out_the_relaunches(monkeypatch):
    """Every soft reset makes Azahar rebuild the whole title.

    L+R+Start is not a screen transition: the game asks the 3DS to
    relaunch it, so Azahar re-parses the ExHeader, re-initialises the
    Vulkan renderer and reallocates a 512 MB upload buffer. Measured 16
    of those in 250 seconds, and Azahar died partway through one.
    """
    import time as _t
    world = World(presses_to_receive=2, shiny=False)
    ctx = FakeCtx(_cfg(starter="chespin", reset_cooldown=0.4))
    _wire(monkeypatch, world, ctx)
    stamps = []
    real_reset = ctx.input.soft_reset

    def reset(hold_s=0.5):
        stamps.append(_t.monotonic())
        real_reset(hold_s)
        if world.resets >= 2:
            world.shiny = True

    ctx.input.soft_reset = reset
    sr._run_starters(ctx, ctx.config["soft_reset"])

    assert len(stamps) >= 2, stamps
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert min(gaps) >= 0.35, f"resets only {min(gaps):.2f}s apart"


def test_no_cooldown_by_default(monkeypatch):
    """The default must not silently slow the hunt back down."""
    import time as _t
    world = World(presses_to_receive=2, shiny=False)
    ctx = FakeCtx(_cfg(starter="chespin"))
    _wire(monkeypatch, world, ctx)
    real_reset = ctx.input.soft_reset

    def reset(hold_s=0.5):
        real_reset(hold_s)
        if world.resets >= 2:
            world.shiny = True

    ctx.input.soft_reset = reset
    t0 = _t.monotonic()
    sr._run_starters(ctx, ctx.config["soft_reset"])
    assert _t.monotonic() - t0 < 1.5, "a default cooldown crept in"


def test_a_hold_button_that_is_an_azahar_hotkey_is_flagged(monkeypatch, caplog):
    """CircleLeft IS the Left arrow, which Azahar uses for speed limit.

    Holding it walks the emulation speed to nothing rather than moving
    a cursor, and nothing on screen looks wrong while it happens.
    """
    import logging
    monkeypatch.setattr(sr, "_warn_if_hotkey", sr._warn_if_hotkey)
    monkeypatch.setattr("pokebot.azahar_config.load_active_profile_binds",
                        lambda: {"CircleLeft": "Left"})
    monkeypatch.setattr("pokebot.azahar_config.load_hotkeys",
                        lambda: {"left": "Decrease Speed Limit"})
    with caplog.at_level(logging.WARNING, logger="pokebot.modes.soft_reset"):
        sr._warn_if_hotkey("CircleLeft")
    assert "Decrease Speed Limit" in caplog.text, caplog.text


def test_a_hold_button_with_no_hotkey_is_silent(monkeypatch, caplog):
    import logging
    monkeypatch.setattr("pokebot.azahar_config.load_active_profile_binds",
                        lambda: {"DpadLeft": "f"})
    monkeypatch.setattr("pokebot.azahar_config.load_hotkeys",
                        lambda: {"left": "Decrease Speed Limit"})
    with caplog.at_level(logging.WARNING, logger="pokebot.modes.soft_reset"):
        sr._warn_if_hotkey("DpadLeft")
    assert "Decrease Speed Limit" not in caplog.text
