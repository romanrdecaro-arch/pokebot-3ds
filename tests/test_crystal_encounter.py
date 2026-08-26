"""
Tests for Crystal random-encounter mode.

The load-bearing behaviour is the shiny check. Reading the wrong byte
for the opponent's DVs would silently MISS a shiny, which is the one
failure a hunt cannot recover from, so paranoid mode must be provably
unable to miss one.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot import gen2                                   # noqa: E402
from pokebot.modes import MODES, crystal_encounter as ce   # noqa: E402


def test_mode_and_both_axes_are_registered() -> None:
    from pokebot.games import methods_for
    assert "crystal_encounter" in MODES
    axes = {m.movement for m in methods_for("CRYSTAL-USA")
            if m.mode == "crystal_encounter"}
    assert axes == {"horizontal", "vertical"}


def test_axes_map_to_the_right_buttons() -> None:
    assert ce._AXES["horizontal"] == ("DpadLeft", "DpadRight")
    assert ce._AXES["vertical"] == ("DpadUp", "DpadDown")


# --------------------------------------------------------------------
# Paranoid detection: must not miss a shiny at ANY offset
# --------------------------------------------------------------------

def _region_with(word: int, at: int, size: int = 0x2A) -> bytes:
    buf = bytearray(size)
    buf[at:at + 2] = word.to_bytes(2, "big")
    return bytes(buf)


def test_paranoid_finds_a_shiny_at_every_possible_offset() -> None:
    """The point of the mode: wherever the real DV offset turns out to
    be, a shiny opponent is still caught."""
    shiny_word = 0xEAAA
    assert gen2.is_shiny(gen2.parse_dvs(shiny_word))
    size = 0x2A
    for at in range(size - 1):
        hits = ce._shiny_candidates(_region_with(shiny_word, at, size))
        addrs = [a for a, _w in hits]
        assert ce._ENEMY_REGION_LO + at in addrs, (
            f"a shiny at offset {at} would have been missed")


def test_paranoid_reports_the_address_and_word() -> None:
    hits = ce._shiny_candidates(_region_with(0xEAAA, 8))
    assert (ce._ENEMY_REGION_LO + 8, 0xEAAA) in hits


def test_all_eight_shiny_spreads_are_detected() -> None:
    for atk in sorted(gen2.SHINY_ATK_DVS):
        word = (atk << 12) | (10 << 8) | (10 << 4) | 10
        assert ce._shiny_candidates(_region_with(word, 4)), f"missed Atk {atk}"


def test_a_zeroed_region_reads_as_no_shiny() -> None:
    assert ce._shiny_candidates(bytes(0x2A)) == []


def test_non_shiny_words_are_not_flagged() -> None:
    for word in (0x0000, 0x1234, 0xFFFF, 0xAAA0, 0x0AAB):
        if gen2.is_shiny(gen2.parse_dvs(word)):
            continue
        assert ce._shiny_candidates(_region_with(word, 6)) == []


def test_false_positive_rate_is_tolerable() -> None:
    """Paranoid mode trades precision for never missing.

    8 of 65536 words are shiny and the region gives ~41 windows, so a
    spurious stop should land around 1 in 200 encounters — infrequent
    enough for a human to wave through.
    """
    windows = (ce._ENEMY_REGION_HI - ce._ENEMY_REGION_LO) - 1
    rate = windows * 8 / 65536
    assert 1 / 400 < rate < 1 / 100, f"one stop per {1 / rate:.0f} encounters"
