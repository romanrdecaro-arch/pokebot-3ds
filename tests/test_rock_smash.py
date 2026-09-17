"""
Tests for the Rock Smash hunt.

Rock Smash is unlike every other idle action the bot has: the button
that MAKES the encounter is also the button that FIGHTS it. A is what
opens the "use Rock Smash?" prompt, what answers Yes, and what clears
the result text -- and the instant a wild appears, that same A press
becomes "attack with move 1", aimed at the shiny the hunt exists to
catch.

So the behaviour worth guarding is when the presses STOP, not when
they start. Most of what follows is some version of that question.

The mirror of it is B. In the Yes/No prompt B answers "No", so a loop
that helpfully pressed B to clear text would cancel every smash it
ever asked for -- the same trap fishing has with B reeling the line
back in.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot.modes import rock_smash_loop as rs  # noqa: E402


class FakeInput:
    def __init__(self):
        self.taps: list[str] = []

    def tap(self, button, hold_s=0.05):
        self.taps.append(button)

    def move_running(self, direction, hold_s=0.35):
        raise AssertionError("a rock smash hunt must not walk away "
                             "from the rock")


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


class RealWaitCtx(FakeCtx):
    """FakeCtx, but time actually passes -- needed to measure latency."""

    def __init__(self):
        super().__init__()
        self._stop_evt = threading.Event()


FAST = rs.SmashPlan(taps=3, settle=0.05, poll_gap=0.01,
                    clear_gap=0.0, restart_gap=0.0)


def appearing_after(n: int):
    """A detector that reports a wild on the n-th poll (0 = at once)."""
    state = {"n": 0}

    def detect():
        state["n"] += 1
        return state["n"] > n

    detect.state = state                   # type: ignore[attr-defined]
    return detect


def never():
    return False


# ----------------------------------------------------------------------
# Smashing
# ----------------------------------------------------------------------
def test_a_smash_presses_a():
    ctx = FakeCtx()
    rs.smash_once(ctx, never, FAST)
    assert ctx.input.taps[0] == "A"


def test_a_quiet_rock_uses_every_tap():
    """Rock Smash does not guarantee an encounter -- most attempts
    produce nothing, and the loop has to keep going on its own."""
    ctx = FakeCtx()
    assert rs.smash_once(ctx, never, FAST) == rs.NO_ENCOUNTER
    assert ctx.input.taps.count("A") == FAST.taps


def test_an_encounter_stops_the_a_presses_immediately():
    """The whole point. One more A here is an attack on the shiny."""
    ctx = FakeCtx()
    plan = rs.SmashPlan(taps=8, settle=0.05, poll_gap=0.01)

    assert rs.smash_once(ctx, appearing_after(1), plan) == rs.ENCOUNTER

    assert ctx.input.taps.count("A") == 1, \
        "kept pressing A after the wild was already on screen"


def test_a_battle_already_on_screen_is_never_pressed_into():
    """Entering with a wild already up must cost ZERO presses.

    The outer loop only calls this when its own scan found nothing,
    but the cheap watcher is faster than that scan and can see a wild
    it missed. Checking before the first press is what stops that
    difference from landing an attack.
    """
    ctx = FakeCtx()
    assert rs.smash_once(ctx, appearing_after(0), FAST) == rs.ENCOUNTER
    assert ctx.input.taps == []


def test_b_is_never_pressed_while_smashing():
    """B answers "No" to the Rock Smash prompt.

    A loop that pressed B to tidy up text would cancel the smash it
    just asked for and never generate an encounter at all.
    """
    ctx = FakeCtx()
    rs.smash_once(ctx, never, FAST)
    assert "B" not in ctx.input.taps


def test_a_battle_is_noticed_without_waiting_out_the_settle():
    """Detection runs INSIDE the settle, not only at the top of the
    next press.

    Checking only before each press would still be safe -- the battle
    is caught before another A goes out either way -- but it would
    cost a full settle of standing in a battle before the hunt hears
    about it, and the outer loop cannot evaluate the wild or start
    catching until this returns. So the property is latency, and the
    only honest way to pin it is the clock.
    """
    ctx = RealWaitCtx()
    plan = rs.SmashPlan(taps=6, settle=0.5, poll_gap=0.01)
    state = {"pressed": False}

    def detect():
        return state["pressed"]

    def tap(button, hold_s=0.05):
        ctx.input.taps.append(button)
        state["pressed"] = True        # the press that broke the rock

    ctx.input.tap = tap                # type: ignore[method-assign]

    started = time.monotonic()
    assert rs.smash_once(ctx, detect, plan) == rs.ENCOUNTER
    elapsed = time.monotonic() - started

    assert ctx.input.taps == ["A"]
    assert elapsed < plan.settle / 2, (
        f"took {elapsed:.3f}s to notice a battle that started "
        f"immediately after the press; the settle is {plan.settle}s, "
        f"so detection is not running inside it")


def test_a_custom_smash_button_is_used():
    ctx = FakeCtx()
    plan = rs.SmashPlan(smash_button="X", taps=1, settle=0.02,
                        poll_gap=0.01)
    rs.smash_once(ctx, never, plan)
    assert ctx.input.taps == ["X"]


# ----------------------------------------------------------------------
# Robustness
# ----------------------------------------------------------------------
def test_a_failing_detector_does_not_abandon_the_attempt():
    """One bad read should not cost the attempt -- but it must not be
    mistaken for "no wild here" either, which would keep A pressing."""
    ctx = FakeCtx()
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] < 3:
            raise RuntimeError("RPC hiccup")
        return True

    plan = rs.SmashPlan(taps=8, settle=0.1, poll_gap=0.005)
    assert rs.smash_once(ctx, flaky, plan) == rs.ENCOUNTER
    assert ctx.input.taps == [], (
        "pressed A while the foe read was failing -- a read that threw "
        "is not evidence that the screen is clear")


def test_stopping_before_the_smash_presses_nothing():
    ctx = FakeCtx()
    ctx.request_stop("user")
    assert rs.smash_once(ctx, never, FAST) == rs.STOPPED
    assert ctx.input.taps == []


def test_stopping_mid_poll_ends_the_attempt():
    ctx = FakeCtx()

    def stop_then_nothing():
        ctx.request_stop("user")
        return False

    assert rs.smash_once(ctx, stop_then_nothing, FAST) == rs.STOPPED


# ----------------------------------------------------------------------
# The reset
# ----------------------------------------------------------------------
def test_restart_only_presses_b():
    """The walking hunt recovers by re-touching RUN. In the overworld
    that lands on the PSS, and there is no battle to run from anyway --
    a wedged smash loop is a stale prompt, which B clears."""
    ctx = FakeCtx()
    rs.restart(ctx, FAST)

    assert ctx.input.taps
    assert set(ctx.input.taps) == {"B"}


def test_restart_clears_more_than_one_prompt():
    ctx = FakeCtx()
    plan = rs.SmashPlan(clear_taps=4, clear_gap=0.0, restart_gap=0.0)
    rs.restart(ctx, plan)
    assert ctx.input.taps.count("B") == 8


def test_clearing_stops_when_asked():
    ctx = FakeCtx()
    ctx.request_stop("user")
    rs.clear_text(ctx, FAST)
    assert ctx.input.taps == []


# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
def test_defaults_are_used_when_nothing_is_configured():
    plan = rs.SmashPlan.from_config({})
    assert plan.smash_button == "A"
    assert plan.clear_button == "B"
    assert plan.taps >= 1


def test_config_values_are_read():
    plan = rs.SmashPlan.from_config(
        {"smash_taps": 9, "smash_settle": 0.8, "smash_clear_taps": 7})
    assert plan.taps == 9
    assert plan.settle == 0.8
    assert plan.clear_taps == 7


def test_a_junk_value_falls_back_rather_than_raising():
    assert rs.SmashPlan.from_config(
        {"smash_settle": "soon"}).settle == rs.SmashPlan().settle


def test_taps_cannot_be_zero():
    """Zero A presses smashes no rocks."""
    assert rs.SmashPlan.from_config({"smash_taps": 0}).taps >= 1


def test_the_poll_gap_has_a_floor():
    """A floor only so a zero cannot busy-spin the emulator."""
    plan = rs.SmashPlan.from_config({"smash_poll_gap": 0})
    assert 0 < plan.poll_gap <= 0.01


def test_the_poll_is_fast_enough_to_catch_the_battle_starting():
    """Every unaware poll gap is a gap in which the next A press could
    land on a battle. Several checks per settle, not one."""
    d = rs.SmashPlan()
    assert d.poll_gap * 4 <= d.settle


@pytest.mark.parametrize("name", ["smash_once", "restart", "clear_text"])
def test_the_public_surface_exists(name):
    assert callable(getattr(rs, name))


def test_the_shipped_config_is_readable():
    import yaml

    cfg = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    plan = rs.SmashPlan.from_config(cfg["random_encounters"])
    assert plan.taps >= 1
    assert plan.settle > 0


# ----------------------------------------------------------------------
# The mode
# ----------------------------------------------------------------------
def test_the_mode_is_registered():
    from pokebot.modes import MODES

    assert "rock_smash" in MODES
    assert callable(MODES["rock_smash"])


def test_the_mode_selects_the_rock_smash_idle_action():
    from pokebot.modes import rock_smash

    assert rock_smash._DEFAULTS["idle_action"] == "rock_smash"


def test_the_mode_retries_after_thirty_seconds_with_the_shipped_config():
    """Rock Smash does not guarantee an encounter, so a quiet stretch
    is normal and the walking hunt's 60 s patience is too much.

    This has to be asserted against the SHIPPED config, not against
    the mode's defaults dict. The merge is {**defaults, **user}, and
    config.yaml already sets a generic stuck_timeout of 60 -- so the
    mode's own 30 lost every time, and a test that only fed it the
    defaults dict happily reported 30 while the hunt waited 60.
    """
    import yaml
    from pokebot.modes import rock_smash
    from pokebot.modes.encounter import FleePlan

    cfg = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    effective = rock_smash.merged_config(cfg["random_encounters"])

    assert effective["stuck_timeout"] == 30.0
    assert FleePlan.from_config(effective).stuck_timeout == 30.0


def test_the_shipped_generic_timeout_really_would_have_shadowed_it():
    """Guards the reasoning above: if config.yaml ever stops setting a
    generic stuck_timeout, this should be reconsidered rather than the
    override quietly deleted."""
    import yaml

    cfg = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["random_encounters"]["stuck_timeout"] != 30


def test_the_flee_delay_override_also_survives_the_shipped_config():
    import yaml
    from pokebot.modes import rock_smash
    from pokebot.modes.encounter import FleePlan

    cfg = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    effective = rock_smash.merged_config(cfg["random_encounters"])

    assert FleePlan.from_config(effective).delay == 2.0


def test_a_user_set_smash_timeout_still_wins():
    from pokebot.modes import rock_smash

    assert rock_smash.merged_config(
        {"smash_stuck_timeout": 12})["stuck_timeout"] == 12.0


def test_a_junk_override_falls_back_rather_than_raising():
    from pokebot.modes import rock_smash

    assert rock_smash.merged_config(
        {"smash_stuck_timeout": "soon"})["stuck_timeout"] == 30.0


def test_the_idle_action_survives_the_merge():
    import yaml
    from pokebot.modes import rock_smash

    cfg = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    assert rock_smash.merged_config(
        cfg["random_encounters"])["idle_action"] == "rock_smash"


def test_the_mode_is_offered_for_gen_six():
    from pokebot.games import methods_for

    modes = [m.mode for m in methods_for("XY")]
    assert "rock_smash" in modes


# ----------------------------------------------------------------------
# Wiring into the shared hunt engine
# ----------------------------------------------------------------------
def _encounter_src() -> str:
    return (REPO / "pokebot" / "modes" / "encounter.py").read_text(
        encoding="utf-8")


def test_the_idle_action_is_wired_into_the_hunt_loop():
    src = _encounter_src()
    assert 'idle_action == "rock_smash"' in src
    assert "rock_smash_loop.smash_once" in src


def test_the_watchdog_recovers_with_b_rather_than_a_run_touch():
    """A RUN touch in the overworld opens the PSS."""
    src = _encounter_src()
    assert "rock_smash_loop.restart" in src


def test_the_hunt_does_not_walk_away_from_the_rock():
    """Only the walking hunt gets the walker; a rock smash hunt stands
    facing one rock, exactly as a fishing hunt stands facing water."""
    src = _encounter_src()
    assert 'flee_walker = walker if idle_action == "walk" else None' in src


# ----------------------------------------------------------------------
# Fishing must not have been disturbed
# ----------------------------------------------------------------------
def test_fishing_defaults_are_untouched():
    """This was asked for as a SEPARATE mode. Fishing shares the same
    engine and the same config keys, so the cheapest way to break it
    is to "improve" something on the way past.
    """
    from pokebot.modes import fishing

    assert fishing._DEFAULTS == {
        "idle_action": "fish",
        "flee_delay": 2.0,
        "flee_intro_taps": 0,
        "flee_clear_taps": 0,
    }


def test_fishing_still_casts_with_y():
    from pokebot.modes import fishing_loop

    assert fishing_loop.FishPlan().cast_button == "Y"
    assert fishing_loop.FishPlan().hook_button == "A"
