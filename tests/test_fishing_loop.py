"""
Tests for the fishing cast loop.

    Y to cast -> watch the foe window -> A the instant something lands
    there -> recast.

The two behaviours worth guarding are opposite mistakes. The hook must
follow DETECTION, not a timer, or it lands outside a bite window the
bot cannot see. And B must NOT be pressed while the rod is out -- B
there reels the line in, so a loop that "helpfully" clears text during
the cast never catches anything at all.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot.modes import fishing_loop as fl  # noqa: E402


class FakeInput:
    def __init__(self):
        self.taps: list[str] = []

    def tap(self, button, hold_s=0.05):
        self.taps.append(button)


class FakeCtx:
    def __init__(self):
        self.input = FakeInput()
        self._stop_evt = threading.Event()
        real = self._stop_evt.wait

        def instant(timeout=None):
            return real(0)

        self._stop_evt.wait = instant      # type: ignore[method-assign]

    def should_stop(self):
        return self._stop_evt.is_set()

    def request_stop(self, reason=""):
        self._stop_evt.set()


FAST = fl.FishPlan(bite_timeout=0.3, poll_gap=0.01, hook_gap=0.0,
                   clear_gap=0.0, recast_gap=0.0)


def biting_after(n: int):
    """A detector that reports a bite on the n-th poll (0 = at once)."""
    state = {"n": 0}

    def detect():
        state["n"] += 1
        return state["n"] > n

    detect.state = state                   # type: ignore[attr-defined]
    return detect


def never():
    return False


# ----------------------------------------------------------------------
# The cast
# ----------------------------------------------------------------------
def test_a_cast_presses_y():
    ctx = FakeCtx()
    fl.cast_once(ctx, never, FAST)
    assert ctx.input.taps[0] == "Y"


def test_a_bite_is_hooked_with_a():
    ctx = FakeCtx()
    result = fl.cast_once(ctx, biting_after(0), FAST)

    assert result == fl.HOOKED
    assert ctx.input.taps == ["Y", "A"]


def test_the_hook_follows_the_bite_rather_than_a_timer():
    """A is pressed only after detection says something is there."""
    ctx = FakeCtx()
    detect = biting_after(3)

    fl.cast_once(ctx, detect, FAST)

    assert ctx.input.taps == ["Y", "A"]
    assert detect.state["n"] == 4          # polled until it saw one


def test_no_bite_reports_no_bite():
    ctx = FakeCtx()
    assert fl.cast_once(ctx, never, FAST) == fl.NO_BITE


def test_a_failed_cast_clears_the_text_so_the_next_one_lands():
    """"Not even a nibble..." eats the following Y press."""
    ctx = FakeCtx()
    fl.cast_once(ctx, never, fl.FishPlan(bite_timeout=0.05, poll_gap=0.01,
                                         clear_taps=4, clear_gap=0.0,
                                         recast_gap=0.0))
    assert ctx.input.taps.count("B") == 4


def test_b_is_never_pressed_while_the_rod_is_out():
    """B during a cast reels the line in; the loop would never fish."""
    ctx = FakeCtx()
    fl.cast_once(ctx, biting_after(2), FAST)

    hooked_at = ctx.input.taps.index("A")
    assert "B" not in ctx.input.taps[:hooked_at]


def test_a_hooked_cast_presses_no_b_at_all():
    ctx = FakeCtx()
    fl.cast_once(ctx, biting_after(0), FAST)
    assert "B" not in ctx.input.taps


def test_extra_hook_taps_are_configurable():
    ctx = FakeCtx()
    plan = fl.FishPlan(bite_timeout=0.3, poll_gap=0.01, hook_taps=3,
                       hook_gap=0.0)
    fl.cast_once(ctx, biting_after(0), plan)
    assert ctx.input.taps.count("A") == 3


def test_a_custom_cast_button_is_used():
    ctx = FakeCtx()
    plan = fl.FishPlan(cast_button="X", bite_timeout=0.05, poll_gap=0.01,
                       clear_taps=0, recast_gap=0.0)
    fl.cast_once(ctx, never, plan)
    assert ctx.input.taps[0] == "X"


# ----------------------------------------------------------------------
# Robustness
# ----------------------------------------------------------------------
def test_a_failing_detector_does_not_abandon_the_cast():
    """One bad read should not cost the whole cast."""
    ctx = FakeCtx()
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] < 3:
            raise RuntimeError("RPC hiccup")
        return True

    assert fl.cast_once(ctx, flaky, FAST) == fl.HOOKED
    assert "A" in ctx.input.taps


def test_stopping_before_the_cast_presses_nothing():
    ctx = FakeCtx()
    ctx.request_stop("user")
    assert fl.cast_once(ctx, biting_after(0), FAST) == fl.STOPPED
    assert ctx.input.taps == []


def test_stopping_mid_poll_ends_the_cast():
    ctx = FakeCtx()

    def stop_then_bite():
        ctx.request_stop("user")
        return False

    assert fl.cast_once(ctx, stop_then_bite, FAST) == fl.STOPPED


# ----------------------------------------------------------------------
# Recovery
# ----------------------------------------------------------------------
def test_restart_only_presses_b():
    """The walking hunt recovers with a RUN touch; that would hit the
    PSS in the overworld, so fishing uses buttons only."""
    ctx = FakeCtx()
    fl.restart(ctx, FAST)

    assert ctx.input.taps
    assert set(ctx.input.taps) == {"B"}


def test_restart_clears_more_than_a_single_failed_cast():
    ctx = FakeCtx()
    plan = fl.FishPlan(clear_taps=4, clear_gap=0.0, recast_gap=0.0)
    fl.restart(ctx, plan)
    assert ctx.input.taps.count("B") == 8


def test_clearing_stops_when_asked():
    ctx = FakeCtx()
    ctx.request_stop("user")
    fl.clear_text(ctx, FAST)
    assert ctx.input.taps == []


# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
def test_defaults_are_used_when_nothing_is_configured():
    plan = fl.FishPlan.from_config({})
    assert plan.cast_button == "Y"
    assert plan.hook_button == "A"
    assert plan.stuck_timeout == 60.0


def test_config_values_are_read():
    plan = fl.FishPlan.from_config(
        {"fish_cast_settle": 2.5, "fish_poll_gap": 0.1,
         "fish_clear_taps": 7})
    assert plan.bite_timeout == 2.5
    assert plan.poll_gap == 0.1
    assert plan.clear_taps == 7


def test_a_junk_value_falls_back_rather_than_raising():
    assert fl.FishPlan.from_config(
        {"fish_cast_settle": "soon"}).bite_timeout == 4.0


def test_the_poll_gap_has_a_floor():
    """Each poll is a foe-window scan; zero would flood the emulator."""
    assert fl.FishPlan.from_config({"fish_poll_gap": 0}).poll_gap >= 0.05


def test_hook_taps_cannot_be_zero():
    """A cast that never presses A catches nothing."""
    assert fl.FishPlan.from_config({"fish_hook_taps": 0}).hook_taps == 1


def test_the_shipped_config_is_readable():
    import yaml

    cfg = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    plan = fl.FishPlan.from_config(cfg["random_encounters"])
    assert plan.bite_timeout > 0
    assert plan.clear_taps > 0


def test_the_fishing_mode_shortened_its_flee():
    """It waited 9 s per encounter, tuned for a 100% emulator."""
    from pokebot.modes import fishing

    assert fishing._DEFAULTS["flee_delay"] <= 3.0
    assert fishing._DEFAULTS["idle_action"] == "fish"


@pytest.mark.parametrize("name", ["cast_once", "restart", "clear_text"])
def test_the_public_surface_exists(name):
    assert callable(getattr(fl, name))


# ----------------------------------------------------------------------
# The player must not wander off the water
# ----------------------------------------------------------------------
def test_fishing_does_not_walk_between_casts():
    """A fishing hunt stands facing one tile.

    The walking hunt spends its flee waits stepping left and right so
    no time is dead. Doing that here walks the player off the water and
    every later cast has nothing to fish in.
    """
    text = (REPO / "pokebot" / "modes" / "encounter.py").read_text(
        encoding="utf-8")
    assert 'flee_walker = walker if idle_action == "walk" else None' in text
    assert "flee_plan, walker)" not in text, \
        "a flee still gets the walker ungated"


def test_the_fishing_flee_is_only_a_screen_press():
    """No B presses either side of the touch -- just the touch."""
    from pokebot.modes import fishing
    from pokebot.modes.encounter import FleePlan

    plan = FleePlan.from_config({**fishing._DEFAULTS})
    assert plan.intro_taps == 0
    assert plan.clear_taps == 0


def test_the_fishing_flee_still_waits_for_the_menu():
    """Zero taps only works if something lets the menu draw first."""
    from pokebot.modes import fishing
    from pokebot.modes.encounter import FleePlan

    plan = FleePlan.from_config({**fishing._DEFAULTS})
    assert plan.delay > 0
    assert plan.run_settle > 0


def test_a_flee_with_no_taps_still_touches_run():
    """The one thing the fishing flee must not lose."""
    import threading
    from pokebot.modes import encounter

    class Inp:
        def __init__(self):
            self.taps = []
            self.touches = []

        def tap(self, b, hold_s=0.05):
            self.taps.append(b)

        def tap_touch(self, x, y, hold_s=0.08):
            self.touches.append((x, y))
            return True

        def move_running(self, d, hold_s=0.35):
            raise AssertionError("fishing must not move the player")

    class Ctx:
        def __init__(self):
            self.input = Inp()
            self._stop_evt = threading.Event()
            real = self._stop_evt.wait
            self._stop_evt.wait = lambda timeout=None: real(0)

        def should_stop(self):
            return self._stop_evt.is_set()

    ctx = Ctx()
    plan = encounter.FleePlan(delay=0.0, intro_taps=0, clear_taps=0,
                              run_settle=0.0, got_away=0.0, tail=0.0)

    encounter._flee(ctx, "side_by_side", [0.5, 0.86], [0.7, 0.7],
                    plan, walker=None)

    assert len(ctx.input.touches) == 1
    assert ctx.input.taps == []
