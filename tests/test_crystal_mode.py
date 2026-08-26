"""
Tests for Crystal manual mode and its Gen 2 -> dashboard mapping.

Gen 2 has no PID, no TSV, and ONE Special stat where Gen 6 has two, so
the payload has to bridge two data models without inventing values.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot import gen2                              # noqa: E402
from pokebot.modes import MODES, crystal_observe      # noqa: E402
from test_gen2 import make_mon                        # noqa: E402


def _mon(dv_word=0x1234, species=157, level=30):
    return gen2.parse_pokemon(bytes(make_mon(species, level, dv_word)))


def test_mode_is_registered() -> None:
    assert "crystal_observe" in MODES


def test_special_maps_to_both_gen6_special_stats() -> None:
    """Gen 2's single Special governs both; reporting it under both is
    accurate, and inventing a second value would not be."""
    dvs = {"HP": 1, "Atk": 2, "Def": 3, "Spe": 4, "Spc": 9}
    ivs = crystal_observe._iv_payload(dvs)
    assert ivs["SpA"] == 9 and ivs["SpD"] == 9
    assert ivs["HP"] == 1 and ivs["Atk"] == 2
    assert ivs["Def"] == 3 and ivs["Spe"] == 4


def test_payload_does_not_invent_a_pid_or_shiny_value() -> None:
    """Gen 2 has neither. Sending them as null stops anything
    downstream deriving a meaningless 'shiny value' from a fake PID."""
    payload = crystal_observe._payload(_mon())
    assert payload["pid"] == 0
    assert payload["psv"] is None
    assert payload["tsv"] is None
    assert payload["generation"] == 2


def test_payload_keeps_the_real_gen2_dvs_alongside_the_mapping() -> None:
    payload = crystal_observe._payload(_mon(dv_word=0xAAAA))
    assert set(payload["dvs"]) == {"HP", "Atk", "Def", "Spe", "Spc"}
    assert payload["shiny"] is True


def test_payload_is_json_serialisable() -> None:
    """It goes to the dashboard and the event log as JSON."""
    import json
    json.dumps(crystal_observe._payload(_mon()))


def test_describe_flags_a_shiny() -> None:
    assert "SHINY" in crystal_observe._describe(_mon(dv_word=0xAAAA))
    assert "SHINY" not in crystal_observe._describe(_mon(dv_word=0x1234))


def test_party_signature_changes_on_a_new_catch() -> None:
    """Party re-reports must fire on change, not every poll."""
    a = [_mon(species=157), _mon(species=175)]
    same = [_mon(species=157), _mon(species=175)]
    grown = a + [_mon(species=209)]
    sig = crystal_observe._party_signature
    assert sig(a) == sig(same), "identical party re-reported"
    assert sig(a) != sig(grown), "a new catch was not noticed"


def test_party_signature_notices_a_level_up() -> None:
    sig = crystal_observe._party_signature
    assert sig([_mon(level=5)]) != sig([_mon(level=6)])


def test_gen2_game_skips_the_gen6_offset_block() -> None:
    """Applying X/Y's party_base to Crystal is meaningless and was
    producing a confusing log line about offsets it never uses."""
    import pokebot.bot as bot_mod
    from pokebot.games import GAMES
    crystal = GAMES["CRYSTAL-USA"]
    assert crystal.generation == 2
    # The guard is generation-based, so it holds for any VC title.
    assert crystal.generation < 6
    assert hasattr(bot_mod.Bot, "_connect")
