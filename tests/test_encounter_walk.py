"""
Tests for keeping the player moving through a battle.

A wild battle used to be ~12 seconds of the bot standing still. Those
waits are now spent walking, so the player is already in the grass the
instant the battle lets go. The risk being guarded here is the other
side of that: movement must NOT still be running once a shiny is found,
because direction presses slide the battle-menu cursor around under the
catch sequence.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot.modes import encounter  # noqa: E402

# Timings small enough that the tests run instantly; the shipped
# values are asserted separately below.
FAST = encounter.FleePlan(delay=0.0, intro_gap=0.0, run_settle=0.0,
                          got_away=0.0, clear_gap=0.0, tail=0.0)
SLOW = encounter.FleePlan(delay=1.0, intro_gap=1.0, run_settle=1.0,
                          got_away=1.0, clear_gap=1.0, tail=1.0)


class FakeInput:
    def __init__(self):
        self.moves: list[tuple[str, float]] = []
        self.taps: list[str] = []
        self.touches: list[tuple] = []

    def move_running(self, direction, hold_s=0.35):
        self.moves.append((direction, hold_s))
        return "postmessage"

    def tap(self, button, hold_s=0.05):
        self.taps.append(button)

    def tap_touch(self, x, y, hold_s=0.08):
        self.touches.append((x, y))
        return True


class FakeCtx:
    def __init__(self):
        self.input = FakeInput()
        self._stop_evt = threading.Event()

        real = self._stop_evt.wait

        def instant(timeout=None):
            return real(0)

        self._stop_evt.wait = instant       # type: ignore[method-assign]

    def should_stop(self):
        return self._stop_evt.is_set()

    def request_stop(self, reason=""):
        self._stop_evt.set()


def make_walker(ctx=None, hold=0.01, gap=0.0):
    ctx = ctx or FakeCtx()
    return encounter.Walker(ctx, ("DpadLeft", "DpadRight"), hold, gap), ctx


# ----------------------------------------------------------------------
# Alternating
# ----------------------------------------------------------------------
def test_steps_alternate_between_the_two_directions():
    w, ctx = make_walker()
    for _ in range(4):
        w.one_step()

    assert [d for d, _ in ctx.input.moves] == [
        "DpadLeft", "DpadRight", "DpadLeft", "DpadRight"]


def test_the_hold_is_passed_through():
    w, ctx = make_walker(hold=0.10)
    w.one_step()
    assert ctx.input.moves[0][1] == 0.10


def test_a_zero_hold_is_floored_rather_than_sent_as_zero():
    """A 0 s press goes down and up inside one polled frame: no move."""
    w, _ = make_walker(hold=0.0)
    assert w.hold_s > 0


def test_vertical_movement_uses_the_vertical_pair():
    ctx = FakeCtx()
    w = encounter.Walker(ctx, encounter._BTN["vertical"], 0.01, 0.0)
    w.one_step()
    w.one_step()
    assert [d for d, _ in ctx.input.moves] == ["DpadUp", "DpadDown"]


# ----------------------------------------------------------------------
# Walking through a wait
# ----------------------------------------------------------------------
def test_waiting_walks_instead_of_standing_still():
    w, ctx = make_walker()
    w.wait(0.05)
    assert ctx.input.moves, "the wait produced no movement at all"


def test_a_zero_wait_does_nothing():
    w, ctx = make_walker()
    w.wait(0)
    assert ctx.input.moves == []


def test_a_stop_request_ends_the_walk_immediately():
    ctx = FakeCtx()
    w, _ = make_walker(ctx)
    ctx.request_stop("user pressed stop")

    w.wait(5.0)

    assert ctx.input.moves == []


def test_idle_waits_without_moving():
    """Some waits genuinely must not move the player."""
    w, ctx = make_walker()
    w.idle(0.05)
    assert ctx.input.moves == []


# ----------------------------------------------------------------------
# The flee path
# ----------------------------------------------------------------------
def test_fleeing_keeps_walking_through_its_waits():
    w, ctx = make_walker()
    # Non-zero waits: FAST's zeroes mean there is nothing to walk
    # through, which would make this pass for the wrong reason.
    brief = encounter.FleePlan(delay=0.0, intro_gap=0.01,
                               run_settle=0.01, got_away=0.01,
                               clear_gap=0.01, tail=0.01)

    encounter._flee(ctx, "side_by_side", [0.5, 0.86], [0.7, 0.7],
                    brief, walker=w)

    assert ctx.input.touches, "RUN was never touched"
    assert ctx.input.moves, "the flee stood still the whole time"


def test_fleeing_without_a_walker_still_works():
    """Backwards compatible: the walker is optional."""
    ctx = FakeCtx()

    encounter._flee(ctx, "side_by_side", [0.5, 0.86], [0.7, 0.7],
                    FAST)

    assert ctx.input.touches
    assert ctx.input.moves == []


def test_fleeing_still_touches_run_exactly_once():
    w, ctx = make_walker()
    encounter._flee(ctx, "side_by_side", [0.5, 0.86], [0.7, 0.7],
                    FAST, walker=w)
    assert len(ctx.input.touches) == 1


def test_fleeing_still_clears_the_text_boxes():
    w, ctx = make_walker()
    encounter._flee(ctx, "side_by_side", [0.5, 0.86], [0.7, 0.7],
                    FAST, walker=w)
    assert ctx.input.taps.count("B") >= 7      # 4 before, 3 after


def test_a_stop_during_the_flee_does_not_keep_walking():
    ctx = FakeCtx()
    w, _ = make_walker(ctx)
    ctx.request_stop("stop")

    encounter._flee(ctx, "side_by_side", [0.5, 0.86], [0.7, 0.7],
                    SLOW, walker=w)

    assert ctx.input.moves == []


# ----------------------------------------------------------------------
# The catch path must be still
# ----------------------------------------------------------------------
def test_the_catch_sequence_does_not_move_the_player():
    """Direction presses slide the battle-menu cursor.

    The catch reaches BAG and the ball by touch, but a stray direction
    press during it is exactly the kind of thing that turns a caught
    shiny into a fled one, so nothing in the catch path may move.
    """
    from pokebot.modes import catch as catch_mod

    ctx = FakeCtx()
    plan = catch_mod.CatchPlan(
        intro_taps=1, intro_gap=0.0, menu_settle=0.0, bag_settle=0.0,
        pocket_settle=0.0, throw_settle=0.0, post_throw_taps=3,
        post_throw_gap=0.0, confirm_window=1.0, confirm_gap=1.0,
        attempts=1, overrides={"bag": (0.1, 0.2), "balls": (0.3, 0.4),
                               "ball": (0.5, 0.6)})

    catch_mod.catch_wild(ctx, plan, 0xABCD, lambda: {0xABCD})

    assert ctx.input.moves == []


def test_the_walker_state_survives_an_encounter_loop_variable():
    """Regression: `a` used to be rebound to a PK6 address.

    The alternating step and the button pair live on the object now,
    so nothing in the caller's scope can clobber them.
    """
    w, ctx = make_walker()
    w.one_step()
    a = 0x08800000              # the shadowing that used to break it
    w.one_step()
    assert a
    assert [d for d, _ in ctx.input.moves] == ["DpadLeft", "DpadRight"]


# ----------------------------------------------------------------------
# The shipped timings
# ----------------------------------------------------------------------
def test_the_shipped_walk_timing_is_the_faster_one():
    """The hold was 0.20 s; the request was to shorten it."""
    import yaml

    cfg = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    r = cfg["random_encounters"]
    assert r["walk_hold"] <= 0.10
    assert r["walk_gap"] <= 0.05


@pytest.mark.parametrize("hold,gap,expect_hold", [
    (0.10, 0.05, 0.10),
    (0.0, 0.0, 0.01),      # floored: a 0 s press never registers
    (-1.0, -1.0, 0.01),    # nonsense values cannot disable movement
])
def test_walker_timings_are_sanitised(hold, gap, expect_hold):
    ctx = FakeCtx()
    w = encounter.Walker(ctx, ("DpadLeft", "DpadRight"), hold, gap)
    assert w.hold_s == expect_hold
    assert w.gap >= 0.0


# ----------------------------------------------------------------------
# Flee timings
# ----------------------------------------------------------------------
def test_the_flee_is_much_faster_than_it_was():
    """It cost 12.7 s an encounter against a 100% emulator."""
    import yaml

    cfg = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    plan = encounter.FleePlan.from_config(cfg["random_encounters"])
    assert plan.total < 6.0, f"flee still costs {plan.total:.1f}s"


def test_the_flee_total_adds_up():
    plan = encounter.FleePlan(delay=1.0, intro_taps=2, intro_gap=0.5,
                              run_settle=0.5, got_away=1.0,
                              clear_taps=2, clear_gap=0.5, tail=1.0)
    assert plan.total == pytest.approx(1.0 + 1.0 + 0.5 + 1.0 + 1.0 + 1.0)


def test_flee_timings_come_from_config():
    plan = encounter.FleePlan.from_config(
        {"flee_delay": 0.25, "run_settle": 0.1, "flee_tail": 0.2})
    assert plan.delay == 0.25
    assert plan.run_settle == 0.1
    assert plan.tail == 0.2


def test_a_junk_flee_timing_falls_back_to_the_default():
    plan = encounter.FleePlan.from_config({"flee_delay": "quickly"})
    assert plan.delay == encounter.FleePlan().delay


def test_tap_counts_cannot_go_negative():
    plan = encounter.FleePlan.from_config({"flee_intro_taps": -5})
    assert plan.intro_taps == 0


def test_the_stall_watchdog_is_on_by_default():
    import yaml

    cfg = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    plan = encounter.FleePlan.from_config(cfg["random_encounters"])
    assert plan.stuck_timeout == 60.0


def test_the_stall_watchdog_can_be_disabled():
    assert encounter.FleePlan.from_config({"stuck_timeout": 0}).stuck_timeout == 0


def test_a_negative_stuck_timeout_is_clamped_not_inverted():
    """A negative would otherwise fire the watchdog on every step."""
    assert encounter.FleePlan.from_config(
        {"stuck_timeout": -30}).stuck_timeout == 0


def test_the_watchdog_resend_is_the_whole_flee_sequence():
    """The re-send must be safe to fire in the overworld too.

    It is B presses either side of one touch, so whatever the touch
    happens to open in the overworld gets closed again straight after.
    """
    ctx = FakeCtx()
    w, _ = make_walker(ctx)

    encounter._flee(ctx, "side_by_side", [0.5, 0.86], [0.7, 0.7],
                    FAST, walker=w)

    taps = ctx.input.taps
    assert taps[:4] == ["B"] * 4        # before the touch
    assert taps[-3:] == ["B"] * 3       # and after it
    assert len(ctx.input.touches) == 1
