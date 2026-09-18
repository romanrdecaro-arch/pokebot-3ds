"""
Drives the REAL hunt loop in rock smash mode.

The wiring tests next door assert that the right lines exist in
encounter.py, which is cheap and catches a branch that was never
connected -- but it cannot tell whether the branch does the right
thing once the loop is actually turning. These do: they run
``encounter.run`` against a fake emulator and watch the buttons.

The one behaviour that has to hold is the same one the unit tests
guard, only end to end this time: **the A presses stop when a wild
appears.** In every other mode the idle action is harmless during a
battle -- a D-pad step slides a menu cursor, a Y cast does nothing.
Here it throws move 1 at the shiny.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hunt_harness import (FAST_FLEE, FAST_RESET, FAST_SMASH,  # noqa: E402
                          SLOT, FakeInput, FakeMon, build_ctx,
                          finished, wire)
from pokebot.modes import encounter, game_reset  # noqa: E402


# ----------------------------------------------------------------------
def test_an_empty_room_gets_a_presses_and_nothing_else(monkeypatch):
    """The idle action is reached at all, and it is the right one."""
    window: list = []
    wire(monkeypatch, window)

    inp = FakeInput(stop_after=6)
    ctx = build_ctx(inp, {"idle_action": "rock_smash",
                          **FAST_FLEE, **FAST_SMASH})
    inp.on_stop = lambda: ctx.request_stop("enough")

    encounter.run(ctx)
    finished(ctx)

    assert inp.taps, "the rock smash idle action never ran"
    assert set(inp.taps) == {"A"}
    assert inp.moves == [], "a rock smash hunt must not walk"


def test_the_a_presses_stop_the_moment_a_wild_appears(monkeypatch):
    """The one that matters: no A press lands on the battle."""
    window: list = []
    wire(monkeypatch, window)

    inp = FakeInput()
    ctx = build_ctx(inp, {"idle_action": "rock_smash",
                          **FAST_FLEE, **FAST_SMASH})

    # The third A press is the one that "breaks the rock".
    real_tap = inp.tap
    state = {"a": 0}

    def tap(button, hold_s=0.05):
        real_tap(button, hold_s)
        if button == "A":
            state["a"] += 1
            if state["a"] == 3:
                window.append((SLOT, FakeMon(0xAAAA)))

    inp.tap = tap                               # type: ignore[method-assign]
    # Stop once the flee has happened, so the run is bounded.
    real_touch = inp.tap_touch

    def touch(x, y, hold_s=0.08):
        ok = real_touch(x, y, hold_s)
        ctx.request_stop("fled")
        return ok

    inp.tap_touch = touch                       # type: ignore[method-assign]

    encounter.run(ctx)
    finished(ctx)

    assert inp.touches, "never fled the encounter it found"
    after = inp.taps[inp.taps.index("A", 0) + state["a"]:]
    assert "A" not in after, \
        f"pressed A after the wild appeared: {inp.taps}"


def test_a_shiny_is_never_attacked_by_the_idle_action(monkeypatch):
    """A shiny stops the hunt dead in on_target=stop, and the smash
    loop must not have queued another A press on the way there."""
    window: list = []
    wire(monkeypatch, window)

    inp = FakeInput()
    ctx = build_ctx(inp, {"idle_action": "rock_smash", "on_target": "stop",
                          **FAST_FLEE, **FAST_SMASH})

    state = {"a": 0}
    real_tap = inp.tap

    def tap(button, hold_s=0.05):
        real_tap(button, hold_s)
        if button == "A":
            state["a"] += 1
            if state["a"] == 2:
                window.append((SLOT, FakeMon(0xBEEF, shiny=True)))

    inp.tap = tap                               # type: ignore[method-assign]

    encounter.run(ctx)
    finished(ctx)

    assert ctx.should_stop(), "a shiny did not stop the hunt"
    assert inp.taps.count("A") == 2, \
        f"kept pressing A with a shiny on screen: {inp.taps}"
    assert inp.touches == [], "fled a shiny instead of stopping"


def test_the_watchdog_resets_with_b_and_keeps_going(monkeypatch):
    """15 s of nothing is normal for Rock Smash, so the recovery has to
    be a screen clear the loop can survive -- not a RUN touch into the
    overworld, where the bottom screen is the PSS."""
    window: list = []
    wire(monkeypatch, window)

    inp = FakeInput()
    ctx = build_ctx(inp, {"idle_action": "rock_smash",
                          **FAST_FLEE, **FAST_SMASH,
                          "stuck_timeout": 0.001,
                          "smash_clear_gap": 0.0, "smash_restart_gap": 0.0})

    def tap(button, hold_s=0.05):
        inp.taps.append(button)
        if inp.taps.count("B") >= 8:
            ctx.request_stop("recovered")

    inp.tap = tap                               # type: ignore[method-assign]

    encounter.run(ctx)
    finished(ctx)

    assert inp.taps.count("B") >= 8, "the watchdog never cleared the screen"
    assert inp.touches == [], \
        "touched the bottom screen in the overworld (that is the PSS)"


# ----------------------------------------------------------------------
# Soft reset as the loop, not as an error path
# ----------------------------------------------------------------------
def fast_reset_plan(monkeypatch):
    """Shrink the reset's silences so the tests run instantly.

    The silences themselves are tested in test_game_reset; what these
    care about is that the hunt takes the reset branch at all and
    picks itself up afterwards.
    """
    monkeypatch.setattr(
        game_reset, "soft_reset_and_wait",
        lambda ctx, loaded, plan, last=None: (
            ctx.input.soft_reset() or True))


def test_a_non_target_encounter_resets_instead_of_fleeing(monkeypatch):
    """A smashed rock is gone; running away leaves the bot facing
    rubble. The reset is what produces the next attempt."""
    window: list = []
    wire(monkeypatch, window)
    fast_reset_plan(monkeypatch)

    inp = FakeInput()
    ctx = build_ctx(inp, {"idle_action": "rock_smash",
                          "no_target_action": "soft_reset",
                          **FAST_FLEE, **FAST_SMASH, **FAST_RESET})

    state = {"a": 0}

    def tap(button, hold_s=0.05):
        inp.taps.append(button)
        if button == "A":
            state["a"] += 1
            if state["a"] == 2:
                window.append((SLOT, FakeMon(0xAAAA)))

    def reset():
        inp.resets += 1
        window.clear()               # a fresh process: nothing lingers
        ctx.request_stop("reset once")

    inp.tap = tap                               # type: ignore
    inp.soft_reset = reset                      # type: ignore

    encounter.run(ctx)
    finished(ctx)

    assert inp.resets == 1, "never soft reset on a non-target encounter"
    assert inp.touches == [], "fled instead of resetting"


def test_the_watchdog_resets_too(monkeypatch):
    """No encounter at all is the same situation as an encounter with
    nothing in it, for a hunt whose attempts come from the reload."""
    window: list = []
    wire(monkeypatch, window)

    inp = FakeInput()
    ctx = build_ctx(inp, {"idle_action": "rock_smash",
                          "no_target_action": "soft_reset",
                          **FAST_FLEE, **FAST_SMASH, **FAST_RESET,
                          "stuck_timeout": 0.001})

    def reset(hold_s=0.5):
        inp.resets += 1
        ctx.request_stop("reset once")

    inp.soft_reset = reset                      # type: ignore

    encounter.run(ctx)
    finished(ctx)

    assert inp.resets == 1, "the watchdog did not soft reset"
    assert inp.touches == [], "touched the bottom screen (the PSS)"


def test_the_baseline_is_rebuilt_so_it_does_not_reset_forever(monkeypatch):
    """The trap this branch sets for itself.

    A reload brings up a FRESH process, and the records in its foe
    window are ones `seen` has never heard of. Leave the baseline
    alone and the very next scan reports one as a brand-new encounter
    -- which resets, which brings up another fresh window, which
    reports again. Forever, without ever smashing a rock.

    Getting this test to actually exercise that took two goes. The
    baseline is built from the foe window at the top of `run`, so a
    wild seeded before the call is already in it and no encounter ever
    happens -- the test passed with the rebuild deleted because it
    never reached the code it was about. The wild has to appear while
    the hunt is running, exactly as a real one does.
    """
    window: list = []
    wire(monkeypatch, window)
    fast_reset_plan(monkeypatch)

    inp = FakeInput()
    ctx = build_ctx(inp, {"idle_action": "rock_smash",
                          "no_target_action": "soft_reset",
                          **FAST_FLEE, **FAST_SMASH, **FAST_RESET},
                    seconds=3.0)

    fresh = {"n": 0}

    def reset(hold_s=0.5):
        inp.resets += 1
        # A fresh process: records nobody has seen before. THIS is
        # what a hunt that does not re-baseline reads as an encounter.
        fresh["n"] += 1
        window[:] = [(SLOT, FakeMon(0xF000 + fresh["n"]))]
        if inp.resets > 3:
            ctx.request_stop("runaway")

    def tap(button, hold_s=0.05):
        inp.taps.append(button)
        # The first press produces the one real encounter, which is
        # what gets the hunt into its first reset.
        if button == "A" and not window and fresh["n"] == 0:
            window.append((SLOT + 0x100, FakeMon(0xAAAA)))
        if inp.taps.count("A") >= 10:
            ctx.request_stop("smashed enough")

    inp.soft_reset = reset                      # type: ignore
    inp.tap = tap                               # type: ignore

    encounter.run(ctx)
    finished(ctx)

    assert inp.resets >= 1, "the test never got the hunt to reset at all"
    assert inp.resets <= 3, (
        f"reset {inp.resets} times -- each reload's fresh records were "
        f"read as new encounters because the baseline was not rebuilt")
    assert inp.taps.count("A") > 1, "never got back to smashing"


def test_a_catch_stops_the_hunt_rather_than_resetting_over_it(monkeypatch):
    """Catching does not save the game. Carrying on would undo it at
    the next non-target encounter -- the one outcome the hunt exists
    to avoid."""
    window: list = []
    wire(monkeypatch, window)
    fast_reset_plan(monkeypatch)

    import pokebot.modes.catch as catch_mod

    class Result:
        caught = True
        detail = "ball landed"
        party_full = False

    monkeypatch.setattr(catch_mod, "catch_wild",
                        lambda *a, **k: Result())
    monkeypatch.setattr(catch_mod, "settle_after_battle",
                        lambda ctx: None)
    monkeypatch.setattr(encounter, "_export_caught", lambda *a, **k: None)

    inp = FakeInput()
    ctx = build_ctx(inp, {"idle_action": "rock_smash",
                          "no_target_action": "soft_reset",
                          **FAST_FLEE, **FAST_SMASH, **FAST_RESET})

    def tap(button, hold_s=0.05):
        inp.taps.append(button)
        if button == "A" and not window:
            window.append((SLOT, FakeMon(0xB00B, shiny=True)))

    inp.tap = tap                               # type: ignore

    encounter.run(ctx)
    finished(ctx)

    assert ctx.should_stop(), "kept hunting after a catch"
    assert inp.resets == 0, (
        "soft reset after catching -- that undoes the catch")
