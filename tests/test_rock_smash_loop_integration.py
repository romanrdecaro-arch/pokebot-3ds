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
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot.bot import BotContext            # noqa: E402
from pokebot.modes import encounter, foe_watch  # noqa: E402

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
        self.stop_after = stop_after
        self.on_stop = on_stop

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


def build_ctx(inp, rcfg):
    evt = threading.Event()
    real_wait = evt.wait
    evt.wait = lambda timeout=None: real_wait(0)   # type: ignore
    return BotContext(
        rpc=object(), game=FakeGame(FakeOffsets()),
        dashboard=FakeDashboard(), input=inp, target=None,
        config={"random_encounters": rcfg}, _stop_evt=evt)


FAST_FLEE = {
    "flee_delay": 0.0, "flee_intro_gap": 0.0, "run_settle": 0.0,
    "flee_got_away": 0.0, "flee_clear_gap": 0.0, "flee_tail": 0.0,
    "stuck_timeout": 0,          # watchdog off; tested separately
}
FAST_SMASH = {"smash_settle": 0.01, "smash_poll_gap": 0.005,
              "smash_taps": 2}


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

    assert inp.taps.count("B") >= 8, "the watchdog never cleared the screen"
    assert inp.touches == [], \
        "touched the bottom screen in the overworld (that is the PSS)"
