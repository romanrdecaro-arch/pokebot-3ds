"""
Tests for the Noibat shaking-spot hunt.

This mode breaks the rule every other hunt in the bot follows, and it
does so on purpose: **a shiny is not automatically a hit.** Everywhere
else, shiny wins over any configured preference -- the starter hunt
has a comment about never soft-resetting a shiny Fennekin over a
species gate, and the gift hunt repeats it. Here the ask was explicit:
only a shiny NOIBAT is wanted, and a shiny of any other species is
reset over like anything else.

So the tests that matter are the ones that would normally be
backwards. A shiny Zubat must NOT stop this hunt. Getting that wrong
in the safe direction (stopping on it) wastes the user's time; getting
it wrong in the other direction is unrecoverable, which is why a
discarded shiny is exported before the reset.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot.modes import noibat  # noqa: E402


class Mon:
    def __init__(self, species=714, shiny=False, key=1):
        self.species = species
        self.shiny = shiny
        self.encryption_key = key
        self.pid = 0x1000 + key
        self.nickname = ""
        self.nature = "Timid"
        self.nature_id = 10
        self.gender = "M"
        self.ivs = {"HP": 31, "Atk": 0, "Def": 31,
                    "SpA": 31, "SpD": 31, "Spe": 31}
        self.tsv, self.psv = 7452, 1
        self.ability_id, self.ability_num = 1, 1
        self.moves = []
        self.party = {"level": 25}
        self.exp = 8000
        self.source_address = 0x08800000 + key


# ----------------------------------------------------------------------
# The rule: shiny AND the right species
# ----------------------------------------------------------------------
def test_a_shiny_noibat_is_the_hit():
    assert noibat.is_hit(None, Mon(species=714, shiny=True), 714)


def test_a_plain_noibat_is_not():
    assert not noibat.is_hit(None, Mon(species=714, shiny=False), 714)


def test_a_shiny_of_another_species_is_NOT_a_hit():
    """The whole point of this mode, and the opposite of every other
    hunt here. A shiny Zubat in Terminus Cave is still not Noibat."""
    assert not noibat.is_hit(None, Mon(species=41, shiny=True), 714)


def test_a_plain_wild_of_another_species_is_not():
    assert not noibat.is_hit(None, Mon(species=41, shiny=False), 714)


@pytest.mark.parametrize("species", [41, 304, 524, 715])
def test_no_other_species_passes_even_when_shiny(species):
    """715 is Noivern -- the evolution, and the easiest near-miss."""
    assert not noibat.is_hit(None, Mon(species=species, shiny=True), 714)


def test_a_configured_filter_narrows_further():
    """An IV or nature filter should still apply on top."""
    class Target:
        rules = ["nature"]

        @staticmethod
        def matches(pkm):
            return pkm.nature == "Timid"

        @staticmethod
        def describe(pkm):
            return "Timid"

    assert noibat.is_hit(Target(), Mon(714, shiny=True), 714)


def test_a_filter_that_does_not_match_rejects_even_a_shiny_noibat():
    class Target:
        rules = ["nature"]

        @staticmethod
        def matches(pkm):
            return pkm.nature == "Adamant"

        @staticmethod
        def describe(pkm):
            return "Adamant"

    assert not noibat.is_hit(Target(), Mon(714, shiny=True), 714)


def test_an_empty_filter_does_not_reject():
    """A Target with no rules is "no preference", not "match nothing"."""
    class Target:
        rules: list = []

        @staticmethod
        def matches(pkm):
            return False

        @staticmethod
        def describe(pkm):
            return ""

    assert noibat.is_hit(Target(), Mon(714, shiny=True), 714)


# ----------------------------------------------------------------------
# A discarded shiny is still worth recording
# ----------------------------------------------------------------------
def test_a_shiny_miss_is_worth_exporting():
    """Resetting over a shiny is irreversible. The .pk6 costs nothing
    and is the only record that it ever existed."""
    assert noibat.worth_saving(Mon(species=41, shiny=True), 714)


def test_a_plain_miss_is_not_worth_exporting():
    """Otherwise targets/ fills with thousands of ordinary Zubat."""
    assert not noibat.worth_saving(Mon(species=41, shiny=False), 714)


def test_a_plain_noibat_is_not_worth_exporting():
    assert not noibat.worth_saving(Mon(species=714, shiny=False), 714)


# ----------------------------------------------------------------------
# The direction
# ----------------------------------------------------------------------
@pytest.mark.parametrize("word,button", [
    ("up", "DpadUp"), ("down", "DpadDown"),
    ("left", "DpadLeft"), ("right", "DpadRight"),
])
def test_each_direction_maps_to_its_dpad_button(word, button):
    assert noibat.NoibatPlan.from_config({"direction": word}).button == button


@pytest.mark.parametrize("word", ["UP", "Up", " up ", "DpadUp"])
def test_the_direction_is_read_leniently(word):
    """It comes from a dropdown, a CLI flag and a config file."""
    assert noibat.NoibatPlan.from_config({"direction": word}).button \
        == "DpadUp"


def test_an_unknown_direction_falls_back_rather_than_crashing():
    plan = noibat.NoibatPlan.from_config({"direction": "northeast"})
    assert plan.button in ("DpadUp", "DpadDown", "DpadLeft", "DpadRight")


def test_the_direction_is_held_not_tapped():
    """The ask was to HOLD it. A tap moves one tile and stops; the
    spot may be several tiles away."""
    src = (REPO / "pokebot" / "modes" / "noibat.py").read_text(
        encoding="utf-8")
    assert "ctx.input.hold(" in src
    assert "ctx.input.release(" in src


def test_the_hold_is_always_released():
    """A latched direction outlives the bot process: the player takes
    back a game that is still walking."""
    src = (REPO / "pokebot" / "modes" / "noibat.py").read_text(
        encoding="utf-8")
    assert "finally:" in src


# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
def test_the_species_is_configurable():
    """714 is Noibat from memory, not from a table in this repo. If it
    is wrong, that should cost a config edit and not a code change."""
    assert noibat.NoibatPlan.from_config({"species": 999}).species == 999


def test_the_species_defaults_to_noibat():
    assert noibat.NoibatPlan.from_config({}).species == 714


def test_a_junk_value_falls_back_rather_than_raising():
    d = noibat.NoibatPlan()
    assert noibat.NoibatPlan.from_config(
        {"encounter_timeout": "soon"}).encounter_timeout \
        == d.encounter_timeout


def test_the_shipped_config_is_readable():
    import yaml

    cfg = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    plan = noibat.NoibatPlan.from_config(cfg.get("noibat") or {})
    assert plan.species == 714
    assert plan.encounter_timeout > 0


# ----------------------------------------------------------------------
# Registration
# ----------------------------------------------------------------------
def test_the_mode_is_registered():
    from pokebot.modes import MODES

    assert "noibat" in MODES


def test_the_launcher_offers_it():
    from pokebot.games import methods_for

    methods = methods_for("Y-USA")
    assert "noibat" in [m.mode for m in methods]


def test_the_launcher_has_a_four_way_direction_picker():
    src = (REPO / "launcher.py").read_text(encoding="utf-8")
    assert "_noibat_dir_var" in src
    for word in ("Up", "Down", "Left", "Right"):
        assert word in src


def test_the_direction_reaches_the_bot():
    src = (REPO / "launcher.py").read_text(encoding="utf-8")
    assert "--noibat-direction" in src
    run_src = (REPO / "run.py").read_text(encoding="utf-8")
    assert "--noibat-direction" in run_src
