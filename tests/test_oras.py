"""
Tests for Omega Ruby / Alpha Sapphire support.

ORAS was half-wired: registered with title IDs, starters and LiveHeX
addresses, and already offered every Gen 6 mode — but with EMPTY
GameOffsets, so the only addresses it ever ran with came from
config.yaml's flat ``offsets:`` block, which is tuned for X/Y. Pick
Omega Ruby in the launcher and you silently got Pokemon Y's memory
map.

That is the shape of bug this file is mostly about: a thing that
*looks* configured because something else filled the gap. So the
assertions are about what a game actually RESOLVES to at run time,
not about what its registry entry says in isolation.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot.games import (GAMES, methods_for,  # noqa: E402
                           party_base_candidates, resolve_offsets,
                           soft_reset_targets_for, starters_for)

ORAS = ("OR-USA", "AS-USA")
XY = ("X-USA", "Y-USA")


def shipped_offsets() -> dict:
    cfg = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    return cfg.get("offsets") or {}


# ----------------------------------------------------------------------
# Registered at all
# ----------------------------------------------------------------------
@pytest.mark.parametrize("key", ORAS)
def test_the_game_is_registered(key):
    assert key in GAMES
    assert GAMES[key].generation == 6


@pytest.mark.parametrize("key", ORAS)
def test_it_has_a_title_id_to_attach_by(key):
    assert GAMES[key].title_ids


def test_omega_ruby_and_alpha_sapphire_are_distinct_titles():
    assert GAMES["OR-USA"].title_ids != GAMES["AS-USA"].title_ids


# ----------------------------------------------------------------------
# Soft reset: the thing that was asked for
# ----------------------------------------------------------------------
@pytest.mark.parametrize("key", ORAS)
def test_soft_reset_is_offered(key):
    assert "soft_reset" in [m.mode for m in methods_for(key)]


@pytest.mark.parametrize("key", ORAS)
def test_the_hoenn_starters_are_the_ones_offered(key):
    """Treecko / Torchic / Mudkip, not Kalos's three."""
    assert starters_for(key) == {"treecko": 252, "torchic": 255,
                                 "mudkip": 258}


@pytest.mark.parametrize("key", ORAS)
def test_starters_is_a_soft_reset_target(key):
    assert "Starters" in soft_reset_targets_for(key)


@pytest.mark.parametrize("key", ORAS)
def test_no_kalos_only_target_is_offered(key):
    """Snorlax on Route 7 and the Route 12 Lapras are X/Y locations.
    Offering them here would start a hunt that cannot work."""
    targets = soft_reset_targets_for(key)
    assert "Snorlax" not in targets
    assert "Lapras" not in targets


@pytest.mark.parametrize("key", ORAS)
def test_the_party_is_locatable(key):
    """The starter hunt finds the party by content, but it still needs
    a candidate address to start from."""
    cands = party_base_candidates(key)
    assert cands, "no party base candidates — discovery has no anchor"
    assert all(0x08000000 <= a < 0x10000000 for a in cands), \
        f"candidates outside the Gen 6 heap: {[hex(a) for a in cands]}"


# ----------------------------------------------------------------------
# Its own addresses, not X/Y's
# ----------------------------------------------------------------------
@pytest.mark.parametrize("key", ORAS)
def test_the_registry_carries_its_own_party_base(key):
    """Empty GameOffsets is what let config.yaml's X/Y block through."""
    assert GAMES[key].offsets.party_base, \
        "party_base is 0, so whatever config.yaml says wins"


def test_oras_does_not_share_xy_party_base():
    assert (GAMES["OR-USA"].offsets.party_base
            != GAMES["Y-USA"].offsets.party_base
            or not GAMES["Y-USA"].offsets.party_base)


def test_the_party_base_comes_from_the_published_trainer_block():
    """tb + 0x16C is the offset confirmed against a running Y-USA, and
    the ORAS trainer block is a published LiveHeX address. Derived, so
    the two cannot drift apart."""
    from pokebot.games import LIVEHEX_REFERENCES

    tb = LIVEHEX_REFERENCES["OR-USA"]["trainer_block"]
    assert GAMES["OR-USA"].offsets.party_base == tb + 0x16C


# ----------------------------------------------------------------------
# Resolving offsets: the config block must not cross games
# ----------------------------------------------------------------------
def test_a_flat_config_block_no_longer_silently_crosses_games():
    """The actual bug. config.yaml's offsets: block is X/Y's, and it
    was applied to whatever game was selected."""
    flat = {"party_base": 0x08CE1CF8, "foe_base": 0x08800000}
    out = resolve_offsets("OR-USA", flat)

    assert out["party_base"] != 0x08CE1CF8, (
        "Omega Ruby still takes Pokemon Y's party_base from the flat "
        "config block")


def test_a_flat_block_still_applies_to_the_game_it_was_written_for():
    flat = {"party_base": 0x08CE1CF8}
    assert resolve_offsets("Y-USA", flat)["party_base"] == 0x08CE1CF8


def test_a_per_game_block_wins_for_that_game():
    cfg = {"party_base": 0x08CE1CF8,
           "OR-USA": {"party_base": 0x0BADF00D}}
    assert resolve_offsets("OR-USA", cfg)["party_base"] == 0x0BADF00D


def test_a_per_game_block_does_not_leak_to_other_games():
    cfg = {"OR-USA": {"party_base": 0x0BADF00D}}
    assert resolve_offsets("Y-USA", cfg).get("party_base") != 0x0BADF00D


def test_an_unknown_game_key_in_config_is_ignored_not_applied():
    cfg = {"NOT-A-GAME": {"party_base": 0x0BADF00D}}
    assert resolve_offsets("OR-USA", cfg).get("party_base") \
        != 0x0BADF00D


def test_hex_strings_are_accepted():
    assert resolve_offsets(
        "Y-USA", {"party_base": "0x08CE1CF8"})["party_base"] == 0x08CE1CF8


def test_a_junk_value_is_dropped_rather_than_crashing_the_run():
    out = resolve_offsets("Y-USA", {"party_base": "not an address"})
    assert "party_base" not in out


def test_zero_means_unset_and_does_not_override():
    """0 is the registry's "unknown" sentinel; a config 0 must not
    blank out a known address."""
    assert resolve_offsets("OR-USA", {"party_base": 0}).get(
        "party_base") != 0


def test_the_shipped_config_does_not_misconfigure_oras():
    """End to end against what actually ships."""
    out = resolve_offsets("OR-USA", shipped_offsets())
    xy = resolve_offsets("Y-USA", shipped_offsets())
    if out.get("party_base") and xy.get("party_base"):
        assert out["party_base"] != xy["party_base"], (
            "Omega Ruby and Pokemon Y resolve to the same party_base")


# ----------------------------------------------------------------------
# Modes that cannot work here are not offered
# ----------------------------------------------------------------------
@pytest.mark.parametrize("key", ORAS)
def test_the_noibat_mode_is_not_offered(key):
    """Not a claim about Hoenn's wildlife -- a claim about this code.
    The mode defaults to a Terminus Cave shaking spot and species 714,
    and neither was built or checked against ORAS."""
    assert "noibat" not in [m.mode for m in methods_for(key)]


@pytest.mark.parametrize("key", XY)
def test_the_noibat_mode_is_still_offered_for_xy(key):
    assert "noibat" in [m.mode for m in methods_for(key)]


@pytest.mark.parametrize("key", ORAS)
def test_the_generic_modes_are_still_offered(key):
    """Walking, fishing, Rock Smash and Sweet Scent are all generic
    Gen 6 mechanics; nothing about them is Kalos-specific."""
    modes = [m.mode for m in methods_for(key)]
    for mode in ("encounter", "fishing", "rock_smash", "sweet_scent",
                 "observe", "gifts"):
        assert mode in modes, f"{mode} missing for {key}"


@pytest.mark.parametrize("key", ORAS)
def test_the_soft_reset_note_does_not_send_the_player_to_kalos(key):
    """It used to name Snorlax and Lapras, which are X/Y encounters."""
    note = [m for m in methods_for(key) if m.mode == "soft_reset"][0].notes
    assert "Snorlax" not in note
    assert "Lapras" not in note


@pytest.mark.parametrize("key", XY)
def test_the_xy_soft_reset_note_still_mentions_its_targets(key):
    note = [m for m in methods_for(key) if m.mode == "soft_reset"][0].notes
    assert "Snorlax" in note


# ----------------------------------------------------------------------
# The key has to be a real one
# ----------------------------------------------------------------------
def test_an_unknown_game_key_does_not_pose_as_a_full_gen6_game():
    """Four tests in this suite asked methods_for("XY") -- not a
    registry key -- and passed, because an unknown key fell through to
    the complete Gen 6 list. They were asserting against a game that
    does not exist, and only the ORAS gating exposed it.

    A caller that gets this wrong should get LESS than a real game,
    never the same or more.
    """
    real = {m.mode for m in methods_for("Y-USA")}
    unknown = {m.mode for m in methods_for("XY")}

    assert unknown <= real
    assert unknown != real, (
        "an unknown key still returns everything a real game does")


@pytest.mark.parametrize("key", ORAS + XY)
def test_every_offered_mode_is_actually_registered(key):
    """A method pointing at a mode that does not exist would fail only
    when the user pressed Start."""
    from pokebot.modes import MODES

    for m in methods_for(key):
        assert m.mode in MODES, f"{key} offers unknown mode {m.mode!r}"


# ----------------------------------------------------------------------
# Telling the player where to stand
# ----------------------------------------------------------------------
@pytest.mark.parametrize("key", ORAS)
def test_the_starter_spot_is_hoenn_not_kalos(key):
    """The hunt mechanics are the same; the instructions are not.
    "Save in front of the starter table" sends a Hoenn player looking
    for a room that does not exist in their game."""
    from pokebot.games import starter_spot

    spot = starter_spot(key)
    assert "Birch" in spot
    assert "table" not in spot


@pytest.mark.parametrize("key", XY)
def test_the_xy_starter_spot_is_unchanged(key):
    from pokebot.games import starter_spot

    assert "table" in starter_spot(key)


def test_the_starter_hunt_uses_the_game_aware_wording():
    src = (REPO / "pokebot" / "modes" / "soft_reset.py").read_text(
        encoding="utf-8")
    assert "starter_spot(ctx.game.key)" in src
    assert '"save made in front of the starter table' not in src
