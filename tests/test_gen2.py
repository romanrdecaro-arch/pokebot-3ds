"""
Tests for the Generation II (Crystal) layer.

Crystal is a Virtual Console title, so its Game Boy WRAM sits at an
undocumented host address inside Azahar. Everything here is therefore
built on synthetic WRAM images: the parsing and the scan signature can
be proven correct without the game, which is what has to be right
before any address hunting is worth doing.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from pokebot import gen2  # noqa: E402


def make_mon(species: int = 251, level: int = 30, dv_word: int = 0xFFFF,
             ot_id: int = 12345, cur_hp: int = 80,
             max_hp: int = 80) -> bytearray:
    """A plausible 48-byte party record."""
    rec = bytearray(gen2.PARTY_STRUCT_SIZE)
    rec[gen2.OFF_SPECIES] = species
    rec[gen2.OFF_OT_ID:gen2.OFF_OT_ID + 2] = ot_id.to_bytes(2, "big")
    rec[gen2.OFF_EXP:gen2.OFF_EXP + 3] = (27000).to_bytes(3, "big")
    rec[gen2.OFF_DVS:gen2.OFF_DVS + 2] = dv_word.to_bytes(2, "big")
    rec[gen2.OFF_LEVEL] = level
    rec[gen2.OFF_CUR_HP:gen2.OFF_CUR_HP + 2] = cur_hp.to_bytes(2, "big")
    rec[gen2.OFF_MAX_HP:gen2.OFF_MAX_HP + 2] = max_hp.to_bytes(2, "big")
    return rec


def make_wram(mons: list, base_noise: int = 0x2000) -> tuple:
    """A WRAM-sized buffer with a party block planted in random noise.

    Returns (buffer, offset_of_party_block).
    """
    rng = random.Random(1234)
    buf = bytearray(rng.getrandbits(8) for _ in range(0x2000 + base_noise))
    off = 0x1CD7                       # where it really lives in WRAM
    buf[off] = len(mons)
    for i in range(6):
        buf[off + 1 + i] = mons[i][gen2.OFF_SPECIES] if i < len(mons) else 0xFF
    buf[off + 7] = 0xFF                # species-list terminator
    for i, rec in enumerate(mons):
        start = off + 8 + i * gen2.PARTY_STRUCT_SIZE
        buf[start:start + len(rec)] = rec
    return bytes(buf), off


# --------------------------------------------------------------------
# DVs and shininess
# --------------------------------------------------------------------

def test_dv_word_unpacks_in_the_documented_order() -> None:
    """Most significant first: Attack, Defense, Speed, Special."""
    dvs = gen2.parse_dvs(0x1234)
    assert dvs["Atk"] == 0x1
    assert dvs["Def"] == 0x2
    assert dvs["Spe"] == 0x3
    assert dvs["Spc"] == 0x4


def test_hp_dv_is_assembled_from_the_low_bits() -> None:
    # Atk=15(1) Def=15(1) Spe=15(1) Spc=15(1) -> HP = 0b1111
    assert gen2.parse_dvs(0xFFFF)["HP"] == 15
    # all even -> HP 0
    assert gen2.parse_dvs(0x2244)["HP"] == 0


def test_the_canonical_shiny_dv_spread_is_shiny() -> None:
    """Atk 10, Def 10, Spe 10, Spc 10 — the classic shiny spread."""
    assert gen2.is_shiny(gen2.parse_dvs(0xAAAA))


@pytest.mark.parametrize("atk", sorted(gen2.SHINY_ATK_DVS))
def test_every_allowed_attack_dv_is_shiny(atk: int) -> None:
    word = (atk << 12) | (10 << 8) | (10 << 4) | 10
    assert gen2.is_shiny(gen2.parse_dvs(word))


@pytest.mark.parametrize("atk", [0, 1, 4, 5, 8, 9, 12, 13])
def test_disallowed_attack_dvs_are_not_shiny(atk: int) -> None:
    word = (atk << 12) | (10 << 8) | (10 << 4) | 10
    assert not gen2.is_shiny(gen2.parse_dvs(word))


@pytest.mark.parametrize("shift,name", [(8, "Def"), (4, "Spe"), (0, "Spc")])
def test_any_fixed_dv_off_ten_is_not_shiny(shift: int, name: str) -> None:
    word = (10 << 12) | (10 << 8) | (10 << 4) | 10
    word &= ~(0xF << shift)
    word |= (9 << shift)               # one off
    assert not gen2.is_shiny(gen2.parse_dvs(word)), f"{name} 9 counted shiny"


def test_shiny_rate_is_one_in_8192() -> None:
    """Exhaustive over all 65536 DV words — the published 1/8192."""
    shiny = sum(1 for w in range(0x10000) if gen2.is_shiny(gen2.parse_dvs(w)))
    assert shiny == 8, f"{shiny} shiny spreads per 65536 words"
    assert 0x10000 // shiny == 8192


# --------------------------------------------------------------------
# Record parsing
# --------------------------------------------------------------------

def test_parses_a_party_record() -> None:
    p = gen2.parse_pokemon(bytes(make_mon(species=251, level=30,
                                          dv_word=0xAAAA)))
    assert p.species == gen2.CELEBI_SPECIES
    assert p.is_celebi
    assert p.level == 30
    assert p.shiny is True
    assert p.ot_id == 12345
    assert p.exp == 27000


def test_multi_byte_fields_are_big_endian() -> None:
    rec = make_mon(ot_id=0x1234, cur_hp=0x0102, max_hp=0x0304)
    p = gen2.parse_pokemon(bytes(rec))
    assert p.ot_id == 0x1234
    assert p.cur_hp == 0x0102
    assert p.max_hp == 0x0304


def test_box_record_is_accepted_without_the_party_tail() -> None:
    p = gen2.parse_pokemon(bytes(make_mon())[:gen2.BOX_STRUCT_SIZE])
    assert p.species == 251
    assert p.level == 0          # not present in a box record


def test_short_buffer_is_rejected() -> None:
    with pytest.raises(ValueError):
        gen2.parse_pokemon(b"\x00" * 8)


# --------------------------------------------------------------------
# The WRAM scan signature — the part that must not false-positive
# --------------------------------------------------------------------

def test_signature_finds_a_planted_party() -> None:
    buf, off = make_wram([make_mon(251, 30), make_mon(155, 12)])
    assert gen2.looks_like_party(buf, off)
    party = gen2.read_party(buf, off)
    assert [p.species for p in party] == [251, 155]


def test_signature_locates_the_block_by_scanning() -> None:
    """A scan over the whole buffer must find it, and only it."""
    buf, off = make_wram([make_mon(251, 30, dv_word=0xAAAA)])
    found = [i for i in range(len(buf) - 8 - gen2.PARTY_STRUCT_SIZE * 6)
             if gen2.looks_like_party(buf, i)]
    assert found == [off], f"expected exactly one hit, got {found}"


def test_implied_wram_base_is_recoverable() -> None:
    """The scan reports a party address; the WRAM base derives from it."""
    buf, off = make_wram([make_mon()])
    host_base = 0x30000000
    party_addr = host_base + off
    assert party_addr - gen2.PARTY_COUNT_FROM_WRAM == host_base


def test_random_noise_does_not_false_positive() -> None:
    """The decisive property: a wrong hit sends the bot to a dead
    address forever, so noise must never satisfy the signature."""
    rng = random.Random(99)
    for trial in range(40):
        noise = bytes(rng.getrandbits(8) for _ in range(4096))
        hits = [i for i in range(len(noise) - 8 - gen2.PARTY_STRUCT_SIZE * 6)
                if gen2.looks_like_party(noise, i)]
        assert hits == [], f"false positive in noise (trial {trial}): {hits}"


def test_mismatched_species_copies_are_rejected() -> None:
    """The species list and each record hold the same data twice; that
    cross-check is what makes the signature trustworthy."""
    buf, off = make_wram([make_mon(251, 30)])
    broken = bytearray(buf)
    broken[off + 8 + gen2.OFF_SPECIES] = 100      # record disagrees with list
    assert not gen2.looks_like_party(bytes(broken), off)


def test_impossible_level_is_rejected() -> None:
    buf, off = make_wram([make_mon(251, 30)])
    broken = bytearray(buf)
    broken[off + 8 + gen2.OFF_LEVEL] = 200
    assert not gen2.looks_like_party(bytes(broken), off)


def test_hp_over_max_is_rejected() -> None:
    buf, off = make_wram([make_mon(251, 30, cur_hp=90, max_hp=80)])
    assert not gen2.looks_like_party(buf, off)


def test_missing_terminator_is_rejected() -> None:
    buf, off = make_wram([make_mon()])
    broken = bytearray(buf)
    broken[off + 7] = 0x00
    assert not gen2.looks_like_party(bytes(broken), off)


@pytest.mark.parametrize("count", [0, 7, 255])
def test_impossible_party_count_is_rejected(count: int) -> None:
    buf, off = make_wram([make_mon()])
    broken = bytearray(buf)
    broken[off] = count
    assert not gen2.looks_like_party(bytes(broken), off)


def test_truncated_buffer_is_rejected_not_crashed() -> None:
    buf, off = make_wram([make_mon()])
    assert not gen2.looks_like_party(buf[:off + 20], off)


# --------------------------------------------------------------------
# The actual goal
# --------------------------------------------------------------------

def test_shiny_celebi_is_detected() -> None:
    """Level 30 Celebi at the Ilex Forest shrine, shiny spread."""
    buf, off = make_wram([make_mon(species=gen2.CELEBI_SPECIES, level=30,
                                   dv_word=0xAAAA)])
    (celebi,) = gen2.read_party(buf, off)
    assert celebi.is_celebi
    assert celebi.level == 30
    assert celebi.shiny


def test_non_shiny_celebi_is_not_flagged() -> None:
    buf, off = make_wram([make_mon(species=gen2.CELEBI_SPECIES, level=30,
                                   dv_word=0x1234)])
    (celebi,) = gen2.read_party(buf, off)
    assert celebi.is_celebi
    assert not celebi.shiny


# --------------------------------------------------------------------
# The scanner's chunking — where an overlap bug would hide a hit
# --------------------------------------------------------------------

class _FakeRPC:
    """Serves a synthetic address space to the scanner."""

    def __init__(self, base: int, data: bytes):
        self.base = base
        self.data = data
        self.reads = 0

    def read(self, addr: int, size: int) -> bytes:
        self.reads += 1
        lo = addr - self.base
        if lo < 0 or lo >= len(self.data):
            raise RuntimeError("unmapped")
        return self.data[lo:lo + size]


def _planted_space(party_at: int, span: int = 0x30000) -> tuple:
    """Random space with one real party block at ``party_at``."""
    rng = random.Random(4242)
    space = bytearray(rng.getrandbits(8) for _ in range(span))
    mons = [make_mon(gen2.CELEBI_SPECIES, 30, dv_word=0xAAAA)]
    space[party_at] = len(mons)
    for i in range(6):
        space[party_at + 1 + i] = (mons[i][gen2.OFF_SPECIES]
                                   if i < len(mons) else 0xFF)
    space[party_at + 7] = 0xFF
    for i, rec in enumerate(mons):
        s = party_at + 8 + i * gen2.PARTY_STRUCT_SIZE
        space[s:s + len(rec)] = rec
    return bytes(space)


def test_scanner_finds_a_block_mid_chunk() -> None:
    from pokebot.crystal import scan_range
    base = 0x30000000
    at = 0x12000
    rpc = _FakeRPC(base, _planted_space(at))
    hits = scan_range(rpc, base, base + 0x30000)
    assert [h["party_addr"] for h in hits] == [base + at]
    assert hits[0]["party"][0].shiny


def test_scanner_finds_a_block_straddling_a_chunk_boundary() -> None:
    """The overlap exists for exactly this case."""
    from pokebot.crystal import scan_range, CHUNK
    base = 0x30000000
    at = CHUNK - 40                     # header in chunk 0, records in 1
    rpc = _FakeRPC(base, _planted_space(at))
    hits = scan_range(rpc, base, base + 0x30000)
    assert [h["party_addr"] for h in hits] == [base + at], (
        "a party block spanning a chunk boundary was missed")


def test_scanner_reports_the_implied_wram_base() -> None:
    from pokebot.crystal import scan_range
    base = 0x30000000
    at = 0x8000
    rpc = _FakeRPC(base, _planted_space(at))
    hits = scan_range(rpc, base, base + 0x30000)
    assert hits[0]["wram_base"] == base + at - gen2.PARTY_COUNT_FROM_WRAM


def test_scanner_survives_unmapped_pages() -> None:
    """An unmapped read must skip, not abort the whole scan."""
    from pokebot.crystal import scan_range
    base = 0x30000000
    rpc = _FakeRPC(base, _planted_space(0x8000))
    hits = scan_range(rpc, base, base + 0x80000)   # runs past the data
    assert len(hits) == 1


def test_scanner_terminates_at_an_awkward_range_tail() -> None:
    """Regression: a step of zero span the scan loop forever.

    `size = min(CHUNK, end - cur)` can come out exactly equal to
    SIGNATURE_LEN at the tail of a range; advancing by
    `size - SIGNATURE_LEN` was then 0 and the loop never progressed.
    """
    from pokebot.crystal import scan_range, SIGNATURE_LEN
    base = 0x30000000
    rpc = _FakeRPC(base, _planted_space(0x100, span=0x2000))
    for extra in (0, 1, 2, SIGNATURE_LEN):
        hits = scan_range(rpc, base, base + SIGNATURE_LEN + extra)
        assert isinstance(hits, list)      # reaching here means it ended


# --------------------------------------------------------------------
# Registry: Crystal must be identified, but must NOT offer Gen 6 modes
# --------------------------------------------------------------------

CRYSTAL_TID = 0x0004000000172800


def test_crystal_title_id_is_recognised() -> None:
    """Read off a live Azahar session running Crystal VC."""
    from pokebot.games import find_game_by_title_id
    from pokebot.citra_rpc import POKEMON_TITLE_IDS
    game = find_game_by_title_id(CRYSTAL_TID)
    assert game is not None, "Crystal VC title id not in the registry"
    assert game.generation == 2
    assert CRYSTAL_TID in POKEMON_TITLE_IDS, "attach would not recognise it"


def test_crystal_offers_only_its_own_modes() -> None:
    """Crystal gets a mode it can actually run, and nothing else.

    Every Gen 6/7 mode reads PK6/PK7 over Azahar RPC at 3DS addresses.
    A VC title has neither, so offering one would let the launcher
    start a hunt that cannot possibly work.
    """
    from pokebot.games import methods_for
    from pokebot.modes import MODES
    methods = methods_for("CRYSTAL-USA")
    assert {m.mode for m in methods} >= {"crystal_observe",
                                         "crystal_encounter",
                                         "crystal_celebi"}
    # Asserted as a property rather than a frozen list, so adding a Gen 2
    # mode does not fail this while adding a Gen 6 one still does.
    for m in methods:
        assert m.mode.startswith("crystal_"), (
            f"offered '{m.mode}', which is not a Gen 2 mode")
        assert m.mode in MODES, f"offered '{m.mode}', which cannot run"

    gen6_modes = {"observe", "encounter", "horde", "fishing",
                  "soft_reset", "livehex", "debug"}
    assert not {m.mode for m in methods} & gen6_modes


def test_every_offered_method_is_a_real_mode() -> None:
    """A dropdown entry with no matching mode fails only at Start."""
    from pokebot.games import GAMES, methods_for
    from pokebot.modes import MODES
    for key in GAMES:
        for m in methods_for(key):
            assert m.mode in MODES, f"{key}: '{m.mode}' is not a mode"


def test_gen6_games_still_offer_their_methods() -> None:
    from pokebot.games import methods_for
    assert len(methods_for("X-USA")) > 0
    assert len(methods_for("USUM-USA-1.2")) > 0


# --------------------------------------------------------------------
# The enemy-DV correlation, which must not be fooled by a coincidence
# --------------------------------------------------------------------

def test_dv_word_round_trips_through_the_correlator() -> None:
    from find_enemy_dvs import dv_word
    dvs = gen2.parse_dvs(0xEAAA)
    assert dv_word(dvs) == 0xEAAA


def test_correlator_locates_a_planted_dv_word() -> None:
    """The whole method: find a known DV word inside a WRAM snapshot."""
    from find_enemy_dvs import find_all
    buf = bytearray(0x2000)
    buf[0x0210:0x0212] = (0xEAAA).to_bytes(2, "big")   # GB 0xC210
    hits = find_all(bytes(buf), 0xEAAA)
    assert hits == [0xC210]


def test_correlator_reports_every_copy() -> None:
    """Party and battle copies both matter — the caller decides which
    region each address falls in."""
    from find_enemy_dvs import find_all
    buf = bytearray(0x2000)
    for off in (0x0210, 0x1CF4):
        buf[off:off + 2] = (0x1234).to_bytes(2, "big")
    assert find_all(bytes(buf), 0x1234) == [0xC210, 0xDCF4]


def test_correlator_finds_nothing_when_absent() -> None:
    from find_enemy_dvs import find_all
    assert find_all(bytes(0x2000), 0xBEEF) == []


# --------------------------------------------------------------------
# Gender — derived from the Attack DV, never stored
# --------------------------------------------------------------------

def test_gender_ratio_table_covers_every_gen2_species() -> None:
    from pokebot.gen2_gender import GENDER_RATIOS
    assert set(GENDER_RATIOS) == set(range(1, gen2.MAX_SPECIES + 1))


@pytest.mark.parametrize("species,expected", [
    (81, "N"),    # Magnemite, genderless
    (151, "N"),   # Mew
    (251, "N"),   # Celebi
    (113, "F"),   # Chansey, always female
])
def test_fixed_gender_species(species: int, expected: str) -> None:
    for atk in range(16):
        assert gen2.gender(species, atk) == expected


def test_fifty_fifty_species_splits_evenly() -> None:
    """Ratio 127: female when Atk*16 < 127, i.e. Atk 0..7."""
    genders = [gen2.gender(25, atk) for atk in range(16)]
    assert genders.count("F") == 8 and genders.count("M") == 8
    assert gen2.gender(25, 7) == "F" and gen2.gender(25, 8) == "M"


def test_one_eighth_female_species_splits_two_of_sixteen() -> None:
    genders = [gen2.gender(1, atk) for atk in range(16)]
    assert genders.count("F") == 2, "12.5% female is 2 of 16 Attack DVs"


def test_unknown_species_is_not_guessed() -> None:
    assert gen2.gender(9999, 5) == "?"


def test_female_of_a_seven_to_one_species_can_never_be_shiny() -> None:
    """The classic Gen 2 quirk, and it falls straight out of the maths.

    Shiny needs Attack in {2,3,6,7,...} (minimum 2), but a female of a
    12.5%-female species needs Atk <= 1. The two cannot both hold.
    """
    for starter in (1, 4, 7, 152, 155, 158):      # Gen 1 + Gen 2 starters
        assert not gen2.female_can_be_shiny(starter)
        for atk in gen2.SHINY_ATK_DVS:
            assert gen2.gender(starter, atk) == "M"


def test_even_ratio_species_can_have_a_shiny_female() -> None:
    assert gen2.female_can_be_shiny(25)
    shiny_females = [atk for atk in gen2.SHINY_ATK_DVS
                     if gen2.gender(25, atk) == "F"]
    assert shiny_females


def test_genderless_species_has_no_shiny_female() -> None:
    assert not gen2.female_can_be_shiny(251)      # Celebi


def test_gender_and_shininess_share_the_attack_dv() -> None:
    """They are not independent — both read the same DV, which is why
    the quirk above exists at all."""
    for atk in range(16):
        dvs = {"HP": 0, "Atk": atk, "Def": 10, "Spe": 10, "Spc": 10}
        if gen2.is_shiny(dvs):
            assert atk in gen2.SHINY_ATK_DVS
            assert gen2.gender(1, atk) == "M"     # Bulbasaur, 12.5% F
