"""
Tests for the gift soft-reset hunt.

The starter hunt can ask a very simple question -- "is there anything
in the party?" -- because it is saved with an EMPTY one. A gift hunt
cannot: the player is mid-game and already has a team, so the question
becomes "is there anything in the party that was not there when we
saved?". Everything below follows from that difference.

Two failure modes are worth more than the rest.

Mashing A past the moment the gift lands walks into the nickname
keyboard, which nothing in this bot knows how to get out of. Detection
is what stops the presses, so how promptly it answers is a correctness
property, not a performance one.

And if a reset silently does not take, the gift from the LAST attempt
is still sitting in the party -- so the next attempt reads it as a
fresh gift, evaluates the same Pokemon again, and the hunt rolls
forever on one result. The reset has to be confirmed, not assumed.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot.modes import gifts  # noqa: E402


class FakeMon:
    def __init__(self, key, species=131, shiny=False, pid=0x1234,
                 addr=0x08C79DA8):
        self.encryption_key = key
        self.species = species
        self.shiny = shiny
        self.pid = pid
        self.nickname = ""
        self.nature = "Adamant"
        self.nature_id = 3
        self.gender = "F"
        self.ivs = {"HP": 31, "Atk": 31, "Def": 31,
                    "SpA": 31, "SpD": 31, "Spe": 31}
        self.tsv = 1234
        self.psv = 4321
        self.ability_id = 11
        self.ability_num = 1
        self.moves = []
        self.party = {"level": 30}
        self.source_address = addr


class FakeInput:
    def __init__(self):
        self.taps: list[str] = []
        self.resets = 0
        self.held: list[str] = []

    def tap(self, button, hold_s=0.05):
        self.taps.append(button)
        return None

    def soft_reset(self, hold_s=0.5):
        self.resets += 1

    def hold(self, button):
        self.held.append(button)
        return True

    def release(self, button):
        return True


class FakeCtx:
    def __init__(self, party=(), target=None):
        self.input = FakeInput()
        self._stop_evt = threading.Event()
        real = self._stop_evt.wait
        self._stop_evt.wait = lambda t=None: real(0)   # type: ignore
        self.party = list(party)
        self.target = target
        self.events: list[tuple] = []
        self.rpc = object()

        class Dash:
            @staticmethod
            def broadcast(kind, **kw):
                self.events.append((kind, kw))

        self.dashboard = Dash()

    def should_stop(self):
        return self._stop_evt.is_set()

    def request_stop(self, reason=""):
        self._stop_evt.set()

    def reasons(self):
        return [k for k, _ in self.events]


# ----------------------------------------------------------------------
# What counts as "the gift arrived"
# ----------------------------------------------------------------------
def test_a_new_key_in_the_party_is_the_gift():
    baseline = {0xAAAA, 0xBBBB}
    party = [FakeMon(0xAAAA), FakeMon(0xBBBB), FakeMon(0xCCCC)]

    assert gifts.new_arrivals(party, baseline) == [party[2]]


def test_the_existing_party_is_never_mistaken_for_a_gift():
    """The whole difference from the starter hunt: the player already
    has a team, and it must not read as six gifts."""
    baseline = {0xAAAA, 0xBBBB}
    party = [FakeMon(0xAAAA), FakeMon(0xBBBB)]

    assert gifts.new_arrivals(party, baseline) == []


def test_an_empty_party_reports_no_gift():
    assert gifts.new_arrivals([], {0xAAAA}) == []


def test_several_arrivals_keep_their_order():
    baseline: set = set()
    party = [FakeMon(0xA), FakeMon(0xB)]
    assert gifts.new_arrivals(party, baseline) == party


# ----------------------------------------------------------------------
# Deciding on one
# ----------------------------------------------------------------------
def test_a_shiny_is_a_target():
    assert gifts.is_target(None, FakeMon(0x1, shiny=True))


def test_a_plain_gift_is_not():
    assert not gifts.is_target(None, FakeMon(0x1))


def test_a_configured_filter_can_also_match():
    class Target:
        rules = ["nature"]

        @staticmethod
        def matches(pkm):
            return pkm.nature == "Adamant"

        @staticmethod
        def describe(pkm):
            return "Adamant"

    assert gifts.is_target(Target(), FakeMon(0x1))


def test_a_filter_that_does_not_match_leaves_a_non_shiny_alone():
    class Target:
        rules = ["nature"]

        @staticmethod
        def matches(pkm):
            return False

        @staticmethod
        def describe(pkm):
            return ""

    assert not gifts.is_target(Target(), FakeMon(0x1))


def test_a_shiny_beats_a_filter_that_does_not_match():
    """Never soft-reset away a shiny over a preference. The starter
    hunt learned this one the hard way: a species gate that ran before
    the target check would have thrown away a shiny Fennekin."""
    class Target:
        rules = ["species"]

        @staticmethod
        def matches(pkm):
            return False

        @staticmethod
        def describe(pkm):
            return ""

    assert gifts.is_target(Target(), FakeMon(0x1, shiny=True))


# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
def test_the_press_rate_is_as_fast_as_the_emulator_will_take():
    """"As fast as possible" has a floor: below about 10 ms Azahar can
    see the key go down and up inside one polled frame and score no
    press at all."""
    plan = gifts.GiftPlan.from_config({})
    assert plan.press_hold <= 0.05
    assert plan.press_gap == 0.0
    assert plan.press_hold >= 0.01


def test_a_too_fast_press_is_floored_rather_than_sent():
    assert gifts.GiftPlan.from_config(
        {"press_hold": 0.0}).press_hold >= 0.01


def test_no_button_is_held_even_with_the_shipped_config():
    """The starter hunt holds a direction to steer its cursor, and
    config.yaml ships hold_button: DpadLeft for it. This mode reads the
    SAME soft_reset section, so asserting from_config({}) proves
    nothing -- it has to be asserted against what actually ships.

    Getting it wrong is not cosmetic: a direction held through a
    gift's Yes/No prompt moves the cursor onto "No", the gift is
    declined, and the hunt presses A forever at a Pokemon it refused.
    """
    import yaml

    cfg = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    shipped = cfg["soft_reset"]

    assert shipped.get("hold_button"), (
        "precondition: the starter hunt still ships a held direction")
    assert not gifts.GiftPlan.from_config(shipped).hold_button


def test_a_gift_specific_hold_can_still_be_set():
    assert gifts.GiftPlan.from_config(
        {"gift_hold_button": "DpadUp"}).hold_button == "DpadUp"


def test_config_values_are_read():
    plan = gifts.GiftPlan.from_config(
        {"receive_timeout": 90, "reset_timeout": 45})
    assert plan.receive_timeout == 90
    assert plan.reset_timeout == 45


def test_a_junk_value_falls_back_rather_than_raising():
    d = gifts.GiftPlan()
    assert gifts.GiftPlan.from_config(
        {"receive_timeout": "soon"}).receive_timeout == d.receive_timeout


def test_the_reset_silences_are_the_ones_soft_reset_paid_for():
    """Reading Azahar's memory while it tears the title down is what
    crashed it; these numbers should not drift from their source."""
    from pokebot.modes import soft_reset as sr

    d = gifts.GiftPlan()
    assert d.pre_reset_quiet == sr._PRE_RESET_QUIET_S
    assert d.reload_grace == sr._RELOAD_READ_GRACE_S


# ----------------------------------------------------------------------
# The mode
# ----------------------------------------------------------------------
def test_the_mode_is_registered():
    from pokebot.modes import MODES

    assert "gifts" in MODES
    assert callable(MODES["gifts"])


def test_the_launcher_offers_it():
    from pokebot.games import methods_for

    methods = methods_for("XY")
    assert "gifts" in [m.mode for m in methods]
    assert any("Gift" in m.label for m in methods)


@pytest.mark.parametrize("name", ["run", "new_arrivals", "is_target",
                                  "GiftPlan"])
def test_the_public_surface_exists(name):
    assert hasattr(gifts, name)
