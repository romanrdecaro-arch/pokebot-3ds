"""
A fake emulator for driving the REAL hunt loop.

Shared by the mode integration tests. Component tests can fake one
function; these run ``encounter.run`` end to end and watch the buttons
that come out, which is the only way to tell whether a branch is wired
to anything.
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
