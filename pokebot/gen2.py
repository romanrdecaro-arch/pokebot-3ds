"""
Generation II (Gold/Silver/Crystal) data structures.

Nothing here is shared with the Gen 6/7 path in ``parser.py``. Gen 2
records are not encrypted, not block-shuffled and carry no checksum —
a party Pokemon is 48 plain bytes. Shininess is not derived from a PID
either: Gen 2 has no PID and no TSV, only DVs.

Layout (Bulbapedia, "Pokemon data structure (Generation II)"), party
structure = 0x30 bytes, box structure = 0x20:

    0x00 species          0x15 DVs (2 bytes)
    0x01 held item        0x17 move PP (4)
    0x02 moves (4)        0x1B friendship
    0x06 OT ID (2)        0x1F level
    0x08 experience (3)   0x20 status
    0x0B EVs (5 x 2)      0x22 current HP (2)
                          0x24 max HP (2)

Multi-byte values in this structure are BIG-endian, unlike the Game
Boy's little-endian pointers.
"""
from __future__ import annotations

from dataclasses import dataclass

#: Sizes of the two record formats.
PARTY_STRUCT_SIZE = 0x30
BOX_STRUCT_SIZE = 0x20

#: Field offsets within a record.
OFF_SPECIES = 0x00
OFF_HELD_ITEM = 0x01
OFF_MOVES = 0x02
OFF_OT_ID = 0x06
OFF_EXP = 0x08
OFF_DVS = 0x15
OFF_FRIENDSHIP = 0x1B
OFF_LEVEL = 0x1F
OFF_STATUS = 0x20
OFF_CUR_HP = 0x22
OFF_MAX_HP = 0x24

#: Game Boy WRAM addresses for Pokemon Crystal (English).
#: WRAM is C000-DFFF; everything below is an absolute GB address.
GB_WRAM_LO = 0xC000
GB_WRAM_HI = 0xE000
GB_PARTY_COUNT = 0xDCD7
GB_PARTY_SPECIES = 0xDCD8      # 6 bytes
GB_PARTY_SPECIES_END = 0xDCDE  # 0xFF terminator
GB_PARTY_MON1 = 0xDCDF
GB_TRAINER_ID = 0xD47B
GB_ENEMY_MON = 0xD204

#: Offsets of those structures from the start of WRAM. The scanner
#: works in these terms so it never has to assume how the Virtual
#: Console emulator lays banks out in host memory.
PARTY_COUNT_FROM_WRAM = GB_PARTY_COUNT - GB_WRAM_LO      # 0x1CD7
PARTY_MON1_FROM_WRAM = GB_PARTY_MON1 - GB_WRAM_LO        # 0x1CDF

#: Highest valid species index in Gen 2 (Celebi).
MAX_SPECIES = 251
CELEBI_SPECIES = 251

#: A Gen 2 Pokemon is shiny when Defense, Speed and Special DVs are all
#: 10 and the Attack DV is one of these. (8/16) * (1/16)^3 = 1/8192.
SHINY_ATK_DVS = frozenset({2, 3, 6, 7, 10, 11, 14, 15})
SHINY_FIXED_DV = 10

_STAT_ORDER = ("Atk", "Def", "Spe", "Spc")


@dataclass(frozen=True)
class Gen2Pokemon:
    """One parsed Gen 2 record."""
    species: int
    level: int
    dvs: dict
    shiny: bool
    ot_id: int
    exp: int
    held_item: int
    moves: tuple
    friendship: int
    cur_hp: int
    max_hp: int

    @property
    def is_celebi(self) -> bool:
        return self.species == CELEBI_SPECIES


def parse_dvs(dv_word: int) -> dict:
    """Unpack the 2-byte DV word into the four stored DVs plus HP.

    Stored most-significant-first as Attack, Defense, Speed, Special —
    four bits each. HP is not stored: it is assembled from the least
    significant bit of each of the other four.
    """
    atk = (dv_word >> 12) & 0xF
    df = (dv_word >> 8) & 0xF
    spe = (dv_word >> 4) & 0xF
    spc = dv_word & 0xF
    hp = ((atk & 1) << 3) | ((df & 1) << 2) | ((spe & 1) << 1) | (spc & 1)
    return {"HP": hp, "Atk": atk, "Def": df, "Spe": spe, "Spc": spc}


def is_shiny(dvs: dict) -> bool:
    """Gen 2 shininess, straight from the DVs."""
    return (dvs.get("Def") == SHINY_FIXED_DV
            and dvs.get("Spe") == SHINY_FIXED_DV
            and dvs.get("Spc") == SHINY_FIXED_DV
            and dvs.get("Atk") in SHINY_ATK_DVS)


def _be(raw: bytes, off: int, size: int) -> int:
    return int.from_bytes(raw[off:off + size], "big")


def parse_pokemon(raw: bytes) -> Gen2Pokemon:
    """Parse a party (0x30) or box (0x20) record.

    Box records stop before the level/HP fields, which come back as 0.
    """
    if len(raw) < BOX_STRUCT_SIZE:
        raise ValueError(
            f"need at least {BOX_STRUCT_SIZE} bytes, got {len(raw)}")
    has_party_tail = len(raw) >= PARTY_STRUCT_SIZE
    dvs = parse_dvs(_be(raw, OFF_DVS, 2))
    return Gen2Pokemon(
        species=raw[OFF_SPECIES],
        level=raw[OFF_LEVEL] if has_party_tail else 0,
        dvs=dvs,
        shiny=is_shiny(dvs),
        ot_id=_be(raw, OFF_OT_ID, 2),
        exp=_be(raw, OFF_EXP, 3),
        held_item=raw[OFF_HELD_ITEM],
        moves=tuple(raw[OFF_MOVES + i] for i in range(4)),
        friendship=raw[OFF_FRIENDSHIP],
        cur_hp=_be(raw, OFF_CUR_HP, 2) if has_party_tail else 0,
        max_hp=_be(raw, OFF_MAX_HP, 2) if has_party_tail else 0,
    )


def looks_like_party(buf: bytes, off: int) -> bool:
    """Is ``buf[off:]`` the party block (count, species list, records)?

    This is the signature the WRAM scan keys on, so it has to be tight:
    a false positive sends the bot reading a wrong address forever. The
    decisive check is the cross-reference — the species list and the
    species byte inside each record are two independent copies of the
    same data, and garbage will not agree on both.
    """
    need = 8 + PARTY_STRUCT_SIZE * 6
    if off < 0 or off + need > len(buf):
        return False

    count = buf[off]
    if not 1 <= count <= 6:
        return False
    if buf[off + 7] != 0xFF:            # species-list terminator
        return False

    species = buf[off + 1:off + 7]
    mon1 = off + 8
    for i in range(count):
        if not 1 <= species[i] <= MAX_SPECIES:
            return False
        rec = mon1 + i * PARTY_STRUCT_SIZE
        if buf[rec + OFF_SPECIES] != species[i]:
            return False                # the two copies disagree
        level = buf[rec + OFF_LEVEL]
        if not 1 <= level <= 100:
            return False
        cur_hp = _be(buf, rec + OFF_CUR_HP, 2)
        max_hp = _be(buf, rec + OFF_MAX_HP, 2)
        if max_hp == 0 or max_hp > 999 or cur_hp > max_hp:
            return False
    return True


def read_party(buf: bytes, off: int) -> list:
    """Parse the party block validated by :func:`looks_like_party`."""
    count = buf[off]
    mon1 = off + 8
    out = []
    for i in range(count):
        rec = mon1 + i * PARTY_STRUCT_SIZE
        out.append(parse_pokemon(buf[rec:rec + PARTY_STRUCT_SIZE]))
    return out


#: Hidden Power's 16 types, indexed by the Gen 2 type formula.
#: (Gen 2 has no Fairy, and Normal is not reachable.)
HP_TYPES = (
    "Fighting", "Flying", "Poison", "Ground",
    "Rock", "Bug", "Ghost", "Steel",
    "Fire", "Water", "Grass", "Electric",
    "Psychic", "Ice", "Dragon", "Dark",
)


def hidden_power(dvs: dict) -> tuple:
    """Gen 2 Hidden Power as ``(type_name, power)``.

    Gen 2 computes this completely differently from Gen 3 onward, so
    the Gen 6 helper produces wrong numbers here:

        type  = 4*(Atk mod 4) + (Def mod 4)
        power = floor((5*(v + 2w + 4x + 8y) + Z) / 2) + 31

    where v/w/x/y are the most significant bits of the Special, Speed,
    Defense and Attack DVs, and Z is Special mod 4. Power therefore
    ranges 31..70 — a property the tests assert exhaustively.
    """
    atk = dvs.get("Atk", 0)
    df = dvs.get("Def", 0)
    spe = dvs.get("Spe", 0)
    spc = dvs.get("Spc", 0)

    type_index = 4 * (atk % 4) + (df % 4)

    v = 1 if spc >= 8 else 0
    w = 1 if spe >= 8 else 0
    x = 1 if df >= 8 else 0
    y = 1 if atk >= 8 else 0
    power = (5 * (v + 2 * w + 4 * x + 8 * y) + (spc % 4)) // 2 + 31

    return HP_TYPES[type_index], power
