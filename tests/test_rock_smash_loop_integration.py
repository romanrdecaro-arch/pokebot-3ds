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
import threading
import time
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot.bot import BotContext            # noqa: E402
from pokebot.modes import encounter, foe_watch, game_reset  # noqa: E402

FOE_BASE = 0x08800000
SLOT = FOE_BASE + 0x3ECC


@dataclass
class FakeMon:
    encryption_key: int
    species: int = 659
    shiny: bool = False
    nickname: str = ""
    exp: int = 100
    gender: str = "M"
    pid: int = 0x12345678
    nature: str = "Adamant"
    ivs: tuple = (31, 31, 31, 31, 31, 31)
    source_address: int = SLOT


class FakeInput:
    """Records every button, and can stop the hunt after N of them."""

    def __init__(self, stop_after=None, on_stop=None):
        self.taps: list[str] = []
        self.touches: list[tuple] = []
        self.moves: list[str] = []
        self.resets = 0
        self.stop_after = stop_after
        self.on_stop = on_stop

    def soft_reset(self, hold_s=0.5):
        self.resets += 1

    def diagnose(self):
        return {"dry_run": False, "driver": "fake"}

    def tap(self, button, hold_s=0.05):
        self.taps.append(button)
        if (self.stop_after is not None
                and len(self.taps) >= self.stop_after and self.on_stop):
            self.on_stop()

    def tap_touch(self, x, y, hold_s=0.08):
        self.touches.append((x, y))
        return True

    def move_running(self, direction, hold_s=0.35):
        self.moves.append(direction)


class FakeDashboard:
    def __init__(self):
        self.events: list[tuple] = []

    def broadcast(self, kind, **kw):
        self.events.append((kind, kw))


@dataclass
class FakeOffsets:
    foe_base: int = FOE_BASE
    foe_scan_len: int = 0x20000
    party_base: int = 0x08C79DA8
    party_stride: int = 484


@dataclass
class FakeGame:
    offsets: FakeOffsets
    key: str = "XY"
    display: str = "Fake X/Y"


def build_ctx(inp, rcfg, seconds=5.0):
    """A context wired to a fake emulator, with a hard stop.

    The deadline is not a convenience. These tests drive a `while not
    should_stop()` loop, so a branch that behaves WRONGLY often just
    never reaches whatever the test was going to stop it with -- and
    the test hangs instead of failing, which is strictly worse than a
    red line. Tripping the deadline sets `ran_away`, so "it never
    finished" becomes an assertion like any other.
    """
    evt = threading.Event()
    real_wait = evt.wait
    evt.wait = lambda timeout=None: real_wait(0)   # type: ignore
    ctx = BotContext(
        rpc=object(), game=FakeGame(FakeOffsets()),
        dashboard=FakeDashboard(), input=inp, target=None,
        config={"random_encounters": rcfg}, _stop_evt=evt)
    deadline = time.monotonic() + seconds
    ctx.ran_away = False                           # type: ignore

    def should_stop():
        if evt.is_set():
            return True
        if time.monotonic() > deadline:
            ctx.ran_away = True                    # type: ignore
            return True
        return False

    ctx.should_stop = should_stop                  # type: ignore
    return ctx


def finished(ctx):
    """The run ended because the test said so, not because it hung."""
    assert not getattr(ctx, "ran_away", False), (
        "the hunt never stopped on its own -- it looped until the test "
        "deadline, which means it is not doing what this test expects")


FAST_FLEE = {
    "flee_delay": 0.0, "flee_intro_gap": 0.0, "run_settle": 0.0,
    "flee_got_away": 0.0, "flee_clear_gap": 0.0, "flee_tail": 0.0,
    "stuck_timeout": 0,          # watchdog off; tested separately
}
FAST_SMASH = {"smash_settle": 0.01, "smash_poll_gap": 0.005,
              "smash_taps": 2}
FAST_RESET = {"reset_quiet": 0.05, "reset_grace": 0.5,
              "reset_boot_timeout": 3.0, "reset_press_hold": 0.001,
              "reset_read_every": 0.25}


def wire(monkeypatch, window):
    """Point the whole detection stack at one mutable ``window`` list
    of (address, mon) pairs, so a test can make a wild appear."""
    monkeypatch.setattr(encounter, "ensure_targets_dir", lambda: None)
    monkeypatch.setattr(encounter, "get_party",
                        lambda *a, **k: [])
    monkeypatch.setattr(encounter, "broadcast_party", lambda ctx, p: set())
    monkeypatch.setattr(encounter, "_report_encounter",
                        lambda *a, **k: None)
    monkeypatch.setattr(encounter, "scan_nonparty",
                        lambda *a, **k: list(window))
    monkeypatch.setattr(foe_watch, "scan_nonparty",
                        lambda *a, **k: list(window))
    monkeypatch.setattr(foe_watch, "read_pk6_at",
                        lambda ctx, addr: dict(window).get(addr))
    import pokebot.platform_utils as pu
    monkeypatch.setattr(pu, "focus_azahar", lambda *a, **k: None)


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
    """30 s of nothing is normal for Rock Smash, so the recovery has to
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
