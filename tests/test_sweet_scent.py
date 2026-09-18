"""
Tests for Sweet Scent mode (formerly "horde").

Two things are being pinned here.

The menu walk, because it is the last OPEN-LOOP idle action in the
bot: seven presses fired on a timer with nothing reading back where
the cursor actually got to. Every other idle action watches the foe
window and reacts. That makes the exact sequence load-bearing in a way
fishing's cast or rock smash's A press is not -- get one press wrong
and the rest land on whatever is on screen.

And the flee and catch, because the whole point of this mode is that
it does NOT have its own. A horde is five wild Pokemon in one battle,
not a different kind of battle; one RUN press ends it exactly as it
ends a single. The mode used to carry a flee_delay of its own that
never took effect, and a test asserting the DEFAULTS dict would have
happily agreed with it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pokebot.modes import encounter, sweet_scent  # noqa: E402
from pokebot.modes.catch import CatchPlan  # noqa: E402
from pokebot.modes.encounter import FleePlan  # noqa: E402


def shipped() -> dict:
    cfg = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    return cfg["random_encounters"]


def effective() -> dict:
    """What the mode actually runs with, merge and all."""
    return {**sweet_scent._DEFAULTS, **shipped()}


# ----------------------------------------------------------------------
# The name
# ----------------------------------------------------------------------
def test_the_mode_is_registered_as_sweet_scent():
    from pokebot.modes import MODES

    assert "sweet_scent" in MODES


def test_the_old_horde_name_still_works():
    """Configs and command lines written before the rename."""
    from pokebot.modes import MODES

    assert MODES["horde"] is MODES["sweet_scent"]


def test_the_launcher_offers_it_by_the_new_name():
    from pokebot.games import methods_for

    methods = methods_for("XY")
    names = [m.label for m in methods]
    modes = [m.mode for m in methods]

    assert "sweet_scent" in modes
    assert any("Sweet Scent" in n for n in names), names
    assert not any(n.startswith("Horde") for n in names), names


# ----------------------------------------------------------------------
# The menu walk
# ----------------------------------------------------------------------
def test_the_sequence_is_the_verified_one():
    assert encounter.SWEET_SCENT_SEQ == [
        "X", "A", "DpadRight", "A", "DpadDown", "A", "A"]


def test_the_cursor_moves_across_before_the_slot_is_chosen():
    """The Right press selects WHICH party slot is used, so it has to
    land before the A that picks one -- after it, it would be moving a
    cursor in the field-move list instead."""
    seq = encounter.SWEET_SCENT_SEQ
    right = seq.index("DpadRight")
    down = seq.index("DpadDown")

    assert seq[0] == "X", "must open the menu first"
    assert right < down, "the slot is chosen before the move is"
    assert seq[right + 1] == "A", "nothing confirms the slot"


def test_the_sequence_is_pressed_in_order(monkeypatch):
    pressed = []

    class Ctx:
        class input:
            @staticmethod
            def tap(b, hold_s=0.05):
                pressed.append(b)

        class _stop_evt:
            @staticmethod
            def wait(t=None):
                return False

        @staticmethod
        def should_stop():
            return False

    encounter._use_sweet_scent(Ctx, 0.0)
    assert pressed == encounter.SWEET_SCENT_SEQ


def test_a_stop_halts_the_sequence_part_way():
    pressed = []
    state = {"stop": False}

    class Ctx:
        class input:
            @staticmethod
            def tap(b, hold_s=0.05):
                pressed.append(b)
                if len(pressed) == 2:
                    state["stop"] = True

        class _stop_evt:
            @staticmethod
            def wait(t=None):
                return False

        @staticmethod
        def should_stop():
            return state["stop"]

    encounter._use_sweet_scent(Ctx, 0.0)
    assert len(pressed) == 2


# ----------------------------------------------------------------------
# The sequence is configurable, because the slot is the player's
# ----------------------------------------------------------------------
def test_the_default_is_used_when_nothing_is_configured():
    assert encounter._sweet_scent_seq({}) == encounter.SWEET_SCENT_SEQ


def test_a_configured_sequence_is_used():
    seq = encounter._sweet_scent_seq(
        {"sweet_scent_sequence": ["X", "A", "A", "DpadDown", "A", "A"]})
    assert seq == ["X", "A", "A", "DpadDown", "A", "A"]


@pytest.mark.parametrize("raw", [
    "X, A, A, DpadDown, A, A",
    "X → A → A → DpadDown → A → A",
])
def test_a_sequence_written_as_a_string_is_understood(raw):
    assert encounter._sweet_scent_seq(
        {"sweet_scent_sequence": raw}) == ["X", "A", "A", "DpadDown",
                                           "A", "A"]


@pytest.mark.parametrize("raw", [[], "", None, ["  "]])
def test_an_empty_sequence_falls_back_rather_than_pressing_nothing(raw):
    """A mode whose idle action presses nothing never encounters
    anything, and the stall watchdog would reset forever."""
    assert encounter._sweet_scent_seq(
        {"sweet_scent_sequence": raw}) == encounter.SWEET_SCENT_SEQ


# ----------------------------------------------------------------------
# The flee and the catch are the random-encounter hunt's, unmodified
# ----------------------------------------------------------------------
def test_the_flee_is_exactly_the_random_encounter_flee():
    """A horde is five wild Pokemon in one battle, not a different
    kind of battle. One RUN press ends it either way."""
    assert FleePlan.from_config(effective()) == FleePlan.from_config(shipped())


def test_the_catch_is_exactly_the_random_encounter_catch():
    assert CatchPlan.from_config(effective()) == \
        CatchPlan.from_config(shipped())


def test_the_mode_carries_no_flee_or_catch_overrides_at_all():
    """The surest version of the two tests above: if it does not set
    them, it cannot differ on them."""
    touched = {k for k in sweet_scent._DEFAULTS
               if k.startswith(("flee_", "catch_", "run_", "on_"))}
    assert not touched, f"mode is overriding flee/catch keys: {touched}"


def test_the_dead_flee_delay_override_is_gone():
    """It claimed 9.0 for the 5-mon intro and never once took effect:
    config.yaml sets flee_delay, and {**defaults, **config} means a
    default cannot beat a key that is set. Asserting the defaults dict
    would have agreed with it; asserting the merge does not."""
    assert "flee_delay" not in sweet_scent._DEFAULTS
    assert FleePlan.from_config(effective()).delay == \
        float(shipped()["flee_delay"])


def test_the_idle_action_is_the_only_thing_it_changes():
    diff = {k: v for k, v in sweet_scent._DEFAULTS.items()
            if shipped().get(k) != v}
    assert set(diff) <= {"idle_action", "sweet_scent_gap",
                         "sweet_scent_settle"}, diff


def test_a_horde_still_stops_on_any_shiny_among_the_five():
    """The multi-mon evaluation lives in the shared encounter branch,
    so it is worth knowing it is still reached."""
    src = (REPO / "pokebot" / "modes" / "encounter.py").read_text(
        encoding="utf-8")
    assert "ordered = sorted(new, key=lambda ap: ap[0])" in src
    assert "if target_hit is None and _is_target(ctx, p):" in src


# ----------------------------------------------------------------------
# End to end: the configured sequence is what actually gets pressed
# ----------------------------------------------------------------------
def test_a_configured_sequence_reaches_the_controller(monkeypatch):
    """The gap the unit tests above leave open.

    One test proves _sweet_scent_seq reads config; another proves
    _use_sweet_scent presses what it is handed. Neither notices if the
    hunt loop forgets to pass the first to the second -- and dropping
    that one argument silently reverts every user's override to the
    default. Only running the loop catches it.
    """
    from hunt_harness import FAST_FLEE, FakeInput, build_ctx, finished, wire

    window: list = []
    wire(monkeypatch, window)

    custom = ["X", "A", "DpadLeft", "DpadLeft", "A", "A"]
    inp = FakeInput()
    ctx = build_ctx(inp, {"idle_action": "sweet_scent",
                          "sweet_scent_sequence": custom,
                          "sweet_scent_gap": 0.0,
                          "sweet_scent_settle": 0.0,
                          **FAST_FLEE}, seconds=3.0)

    def tap(button, hold_s=0.05):
        inp.taps.append(button)
        if len(inp.taps) >= len(custom):
            ctx.request_stop("one pass is enough")

    inp.tap = tap                               # type: ignore

    encounter.run(ctx)
    finished(ctx)

    assert inp.taps == custom, (
        f"pressed {inp.taps}, not the configured {custom} -- the "
        f"override is being dropped between config and controller")


def test_the_default_sequence_reaches_the_controller(monkeypatch):
    from hunt_harness import FAST_FLEE, FakeInput, build_ctx, finished, wire

    window: list = []
    wire(monkeypatch, window)

    inp = FakeInput()
    ctx = build_ctx(inp, {"idle_action": "sweet_scent",
                          "sweet_scent_gap": 0.0,
                          "sweet_scent_settle": 0.0,
                          **FAST_FLEE}, seconds=3.0)

    def tap(button, hold_s=0.05):
        inp.taps.append(button)
        if len(inp.taps) >= len(encounter.SWEET_SCENT_SEQ):
            ctx.request_stop("one pass is enough")

    inp.tap = tap                               # type: ignore

    encounter.run(ctx)
    finished(ctx)

    assert inp.taps == encounter.SWEET_SCENT_SEQ
