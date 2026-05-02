"""
pk67_parser.py - Parser for Gen 6/7 (3DS) Pokémon data structures.

Targets games: X, Y, Omega Ruby, Alpha Sapphire, Sun, Moon,
               Ultra Sun, Ultra Moon

Layout of an encrypted PK6/PK7 record:

    0x00-0x03  Encryption Key  (u32, plaintext)
    0x04-0x05  Sanity placeholder (u16, plaintext)
    0x06-0x07  Checksum  (u16, plaintext; sum of u16 words 0x08..0xE7)
    0x08-0xE7  Encrypted body: four 56-byte blocks A, B, C, D, shuffled
    0xE8-0x103 Encrypted party-only stats (28 bytes), present only in
               260-byte party records

Total: 232 bytes (box record) or 260 bytes (party record).

Encryption is a 32-bit LCRNG keystream XORed over the body.
  seed_0 = encryption_key (the u32 at offset 0x00)
  seed_n = (seed_{n-1} * 0x41C64E6D + 0x6073) mod 2^32
  mask_n = (seed_n >> 16) & 0xFFFF       (XORed with the n-th u16 of body)

Block shuffling: index = ((encryption_key >> 13) & 31) % 24
The 24 permutations of A,B,C,D enumerated in lexical order are below.

References:
  - https://projectpokemon.org/home/docs/gen-6/pkm-structure-xy-r66/
  - https://projectpokemon.org/home/docs/gen-7/gen7-ram-map-wip-r101/
  - PKHeX source (https://github.com/kwsch/PKHeX) is the canonical
    reference for any field that is unclear here.

PK6 vs PK7 share the same 232/260-byte layout and encryption.
PK7 reuses some previously-unused bytes (e.g. Hyper Training flags,
Resort/Festa stats). The fields parsed below are common to both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# --------------------------------------------------------------------------
# Encryption / shuffling primitives
# --------------------------------------------------------------------------

LCRNG_MULT = 0x41C64E6D
LCRNG_ADD  = 0x00006073

# Lexical ordering of the 24 permutations of A,B,C,D.
# SHUFFLE_ORDER[i][slot] = the canonical name of the block that lives
# at encrypted slot `slot` when shuffle id == i.
SHUFFLE_ORDER = [
    "ABCD", "ABDC", "ACBD", "ACDB", "ADBC", "ADCB",
    "BACD", "BADC", "BCAD", "BCDA", "BDAC", "BDCA",
    "CABD", "CADB", "CBAD", "CBDA", "CDAB", "CDBA",
    "DABC", "DACB", "DBAC", "DBCA", "DCAB", "DCBA",
]
BLOCK_SIZE = 56
BLOCKS_BASE = 0x08
BODY_END   = 0xE8           # exclusive
PARTY_END  = 0x104          # exclusive

assert len(SHUFFLE_ORDER) == 24
assert all(sorted(s) == ["A", "B", "C", "D"] for s in SHUFFLE_ORDER)


def shuffle_id_from_key(enc_key: int) -> int:
    """Block-shuffle index derived from the encryption key."""
    return ((enc_key >> 13) & 31) % 24


def _xor_keystream(buf: bytearray, start: int, end: int, seed: int) -> None:
    """In-place XOR of buf[start:end] with the LCRNG keystream.
    Operates on 16-bit words; (end-start) must be even."""
    s = seed & 0xFFFFFFFF
    for i in range(start, end, 2):
        s = (s * LCRNG_MULT + LCRNG_ADD) & 0xFFFFFFFF
        mask = (s >> 16) & 0xFFFF
        buf[i]     ^= mask & 0xFF
        buf[i + 1] ^= (mask >> 8) & 0xFF


def _unshuffle_blocks(buf: bytearray, enc_key: int) -> None:
    """Reorder the four 56-byte blocks at 0x08..0xE7 from shuffled
    layout into canonical A,B,C,D layout."""
    sid = shuffle_id_from_key(enc_key)
    order = SHUFFLE_ORDER[sid]
    shuffled = [bytes(buf[BLOCKS_BASE + i*BLOCK_SIZE :
                          BLOCKS_BASE + (i+1)*BLOCK_SIZE]) for i in range(4)]
    for canonical_idx, name in enumerate("ABCD"):
        src_slot = order.index(name)
        buf[BLOCKS_BASE + canonical_idx*BLOCK_SIZE :
            BLOCKS_BASE + (canonical_idx+1)*BLOCK_SIZE] = shuffled[src_slot]


def _shuffle_blocks(buf: bytearray, enc_key: int) -> None:
    """Inverse of _unshuffle_blocks: take canonical A,B,C,D layout
    and put each block into its shuffled slot."""
    sid = shuffle_id_from_key(enc_key)
    order = SHUFFLE_ORDER[sid]
    canonical = [bytes(buf[BLOCKS_BASE + i*BLOCK_SIZE :
                           BLOCKS_BASE + (i+1)*BLOCK_SIZE]) for i in range(4)]
    for slot in range(4):
        src_idx = "ABCD".index(order[slot])
        buf[BLOCKS_BASE + slot*BLOCK_SIZE :
            BLOCKS_BASE + (slot+1)*BLOCK_SIZE] = canonical[src_idx]


# --------------------------------------------------------------------------
# High-level encrypt / decrypt
# --------------------------------------------------------------------------

def decrypt_pkm(raw: bytes) -> bytes:
    """Decrypt a 232- or 260-byte PK6/PK7 record. Returns plaintext."""
    if len(raw) not in (232, 260):
        raise ValueError(f"Expected 232 or 260 bytes, got {len(raw)}")
    buf = bytearray(raw)
    enc_key = int.from_bytes(buf[0:4], "little")
    _xor_keystream(buf, BLOCKS_BASE, BODY_END, enc_key)
    if len(buf) == 260:
        _xor_keystream(buf, BODY_END, PARTY_END, enc_key)
    _unshuffle_blocks(buf, enc_key)
    return bytes(buf)


def encrypt_pkm(plain: bytes) -> bytes:
    """Encrypt a plaintext PK6/PK7 record (inverse of decrypt_pkm)."""
    if len(plain) not in (232, 260):
        raise ValueError(f"Expected 232 or 260 bytes, got {len(plain)}")
    buf = bytearray(plain)
    enc_key = int.from_bytes(buf[0:4], "little")
    _shuffle_blocks(buf, enc_key)
    _xor_keystream(buf, BLOCKS_BASE, BODY_END, enc_key)
    if len(buf) == 260:
        _xor_keystream(buf, BODY_END, PARTY_END, enc_key)
    return bytes(buf)


def calc_checksum(plaintext: bytes) -> int:
    """Sum of u16 words from 0x08..0xE7, truncated to 16 bits."""
    s = 0
    for i in range(BLOCKS_BASE, BODY_END, 2):
        s += int.from_bytes(plaintext[i:i+2], "little")
    return s & 0xFFFF


# --------------------------------------------------------------------------
# Field-level parser
# --------------------------------------------------------------------------

NATURES = [
    "Hardy",  "Lonely",  "Brave",   "Adamant", "Naughty",
    "Bold",   "Docile",  "Relaxed", "Impish",  "Lax",
    "Timid",  "Hasty",   "Serious", "Jolly",   "Naive",
    "Modest", "Mild",    "Quiet",   "Bashful", "Rash",
    "Calm",   "Gentle",  "Sassy",   "Careful", "Quirky",
]

LANGUAGES = {
    1: "JPN", 2: "ENG", 3: "FRE", 4: "ITA",
    5: "GER", 7: "SPA", 8: "KOR",
}


@dataclass
class ParsedPokemon:
    # Identity
    species: int
    form: int
    nickname: str
    is_nicknamed: bool
    is_egg: bool

    # Trainer / origin
    ot_name: str
    ot_tid: int
    ot_sid: int
    ot_gender_female: bool
    ot_language: int
    pokeball: int
    met_level: int
    met_location: int
    egg_location: int

    # Genetics
    pid: int
    encryption_key: int
    nature: str
    nature_id: int
    gender: str            # "M", "F", or "G" (genderless)
    fateful_encounter: bool
    ability_id: int
    ability_num: int       # 1, 2, or 4 (=hidden)
    held_item: int
    exp: int

    # Stats
    ivs: dict
    evs: dict

    # Moves
    moves: list             # [m1,m2,m3,m4]
    move_pp: list
    move_pp_ups: list

    # Computed flags
    shiny: bool
    tsv: int                # trainer shiny value
    psv: int                # personality shiny value

    # Integrity
    checksum_stored: int
    checksum_computed: int
    checksum_valid: bool

    # Party-only (None for box records)
    party: Optional[dict] = None


def parse_pkm(plain: bytes) -> ParsedPokemon:
    """Parse a decrypted, unshuffled PK6/PK7 buffer into a structured object."""
    if len(plain) not in (232, 260):
        raise ValueError(f"Expected 232 or 260 bytes, got {len(plain)}")

    def u8(o):  return plain[o]
    def u16(o): return int.from_bytes(plain[o:o+2], "little")
    def u32(o): return int.from_bytes(plain[o:o+4], "little")

    enc_key  = u32(0x00)
    cs_store = u16(0x06)
    cs_calc  = calc_checksum(plain)

    # Block A (0x08-0x3F)
    species   = u16(0x08)
    held_item = u16(0x0A)
    ot_tid    = u16(0x0C)
    ot_sid    = u16(0x0E)
    exp       = u32(0x10)
    ability_id  = u8(0x14)
    ability_num = u8(0x15)
    pid       = u32(0x18)
    nature_id = u8(0x1C)
    flags_1d  = u8(0x1D)

    fateful       = bool(flags_1d & 0x01)
    is_female     = bool(flags_1d & 0x02)
    is_genderless = bool(flags_1d & 0x04)
    form          = (flags_1d >> 3) & 0x1F
    if is_genderless:
        gender = "G"
    elif is_female:
        gender = "F"
    else:
        gender = "M"

    evs = {
        "HP":  u8(0x1E), "Atk": u8(0x1F), "Def": u8(0x20),
        "Spe": u8(0x21), "SpA": u8(0x22), "SpD": u8(0x23),
    }

    # Block B (0x40-0x77)
    nickname = plain[0x40:0x58].decode("utf-16-le", errors="replace")
    nickname = nickname.split("\x00", 1)[0]
    moves       = [u16(0x5A + 2*i) for i in range(4)]
    move_pp     = [u8(0x62 + i)    for i in range(4)]
    move_pp_ups = [u8(0x66 + i)    for i in range(4)]
    iv_word     = u32(0x74)
    ivs = {
        "HP":  (iv_word >>  0) & 0x1F,
        "Atk": (iv_word >>  5) & 0x1F,
        "Def": (iv_word >> 10) & 0x1F,
        "Spe": (iv_word >> 15) & 0x1F,
        "SpA": (iv_word >> 20) & 0x1F,
        "SpD": (iv_word >> 25) & 0x1F,
    }
    is_egg       = bool((iv_word >> 30) & 1)
    is_nicknamed = bool((iv_word >> 31) & 1)

    # Block D (0xB0-0xE7)
    ot_name = plain[0xB0:0xC8].decode("utf-16-le", errors="replace")
    ot_name = ot_name.split("\x00", 1)[0]
    egg_loc      = u16(0xD8)
    met_loc      = u16(0xDA)
    pokeball     = u8(0xDC)
    met_byte     = u8(0xDD)
    met_level    = met_byte & 0x7F
    ot_female    = bool(met_byte & 0x80)
    ot_lang      = u8(0xE3)

    # Shiny computation
    psv = (pid >> 16) ^ (pid & 0xFFFF)
    tsv = ot_tid ^ ot_sid
    shiny = (psv ^ tsv) < 16    # threshold is 16 in Gen 6/7

    nature_name = NATURES[nature_id] if nature_id < 25 else f"?({nature_id})"

    parsed = ParsedPokemon(
        species=species, form=form, nickname=nickname,
        is_nicknamed=is_nicknamed, is_egg=is_egg,
        ot_name=ot_name, ot_tid=ot_tid, ot_sid=ot_sid,
        ot_gender_female=ot_female, ot_language=ot_lang,
        pokeball=pokeball, met_level=met_level,
        met_location=met_loc, egg_location=egg_loc,
        pid=pid, encryption_key=enc_key,
        nature=nature_name, nature_id=nature_id,
        gender=gender, fateful_encounter=fateful,
        ability_id=ability_id, ability_num=ability_num,
        held_item=held_item, exp=exp,
        ivs=ivs, evs=evs,
        moves=moves, move_pp=move_pp, move_pp_ups=move_pp_ups,
        shiny=shiny, tsv=tsv, psv=psv,
        checksum_stored=cs_store, checksum_computed=cs_calc,
        checksum_valid=(cs_store == cs_calc),
    )

    if len(plain) == 260:
        parsed.party = {
            "status":   u8(0xE8),
            "level":    u8(0xEC),
            "hp_cur":   u16(0xF0),
            "hp_max":   u16(0xF2),
            "atk":      u16(0xF4),
            "def":      u16(0xF6),
            "spe":      u16(0xF8),
            "spa":      u16(0xFA),
            "spd":      u16(0xFC),
        }

    return parsed


# --------------------------------------------------------------------------
# Self-test (synthetic round-trip)
# --------------------------------------------------------------------------

def _self_test():
    """Build a synthetic Pokémon, encrypt it, decrypt it, parse it."""
    plain = bytearray(260)

    # Choose an encryption key that produces a non-trivial shuffle id
    enc_key = 0xCAFEBABE
    plain[0:4] = enc_key.to_bytes(4, "little")

    # Block A
    plain[0x08:0x0A] = (25).to_bytes(2, "little")        # species: Pikachu
    plain[0x0A:0x0C] = (0).to_bytes(2, "little")         # no item
    plain[0x0C:0x0E] = (12345).to_bytes(2, "little")     # OT TID
    plain[0x0E:0x10] = (54321).to_bytes(2, "little")     # OT SID
    plain[0x10:0x14] = (125000).to_bytes(4, "little")    # EXP
    plain[0x14] = 9                                      # ability id
    plain[0x15] = 1                                      # ability num
    plain[0x18:0x1C] = (0xDEADBEEF).to_bytes(4, "little")# PID
    plain[0x1C] = 13                                     # Jolly
    plain[0x1D] = 0x00                                   # male, normal form
    plain[0x1E:0x24] = bytes([0, 252, 0, 252, 4, 0])     # EVs

    # Block B - nickname "Sparky" + moves
    nick = "Sparky".encode("utf-16-le")
    plain[0x40:0x40+len(nick)] = nick
    for i, m in enumerate([85, 86, 98, 87]):             # Thunderbolt, Thunder, Quick Attack, Thunder Wave
        plain[0x5A + 2*i : 0x5C + 2*i] = m.to_bytes(2, "little")
    plain[0x62:0x66] = bytes([15, 10, 30, 20])           # PP
    # IVs all 31, mark nicknamed
    iv_word = 0
    for shift in (0, 5, 10, 15, 20, 25):
        iv_word |= 31 << shift
    iv_word |= 1 << 31                                   # is_nicknamed
    plain[0x74:0x78] = iv_word.to_bytes(4, "little")

    # Block D - OT name "Trainer"
    ot = "Trainer".encode("utf-16-le")
    plain[0xB0:0xB0+len(ot)] = ot
    plain[0xDC] = 4                                      # Poké Ball
    plain[0xDD] = 5                                      # met level 5, OT male
    plain[0xE3] = 2                                      # English

    # Party stats
    plain[0xEC]      = 50                                # level
    plain[0xF0:0xF2] = (110).to_bytes(2, "little")       # cur HP
    plain[0xF2:0xF4] = (130).to_bytes(2, "little")       # max HP
    plain[0xF4:0xF6] = (95).to_bytes(2, "little")
    plain[0xF6:0xF8] = (60).to_bytes(2, "little")
    plain[0xF8:0xFA] = (140).to_bytes(2, "little")
    plain[0xFA:0xFC] = (90).to_bytes(2, "little")
    plain[0xFC:0xFE] = (75).to_bytes(2, "little")

    # Stamp checksum
    cs = calc_checksum(plain)
    plain[0x06:0x08] = cs.to_bytes(2, "little")

    plain_bytes = bytes(plain)
    encrypted = encrypt_pkm(plain_bytes)
    decrypted = decrypt_pkm(encrypted)

    assert decrypted == plain_bytes, "round-trip failed"
    parsed = parse_pkm(decrypted)

    print(f"Round-trip OK. Shuffle id = {shuffle_id_from_key(enc_key)} "
          f"({SHUFFLE_ORDER[shuffle_id_from_key(enc_key)]})")
    print(f"Encrypted first 16 bytes : {encrypted[:16].hex()}")
    print(f"Plaintext  first 16 bytes : {plain_bytes[:16].hex()}")
    print()
    print(f"Species     : #{parsed.species}")
    print(f"Nickname    : {parsed.nickname!r}  (nicknamed={parsed.is_nicknamed})")
    print(f"OT          : {parsed.ot_name!r}  TID/SID={parsed.ot_tid}/{parsed.ot_sid}")
    print(f"Language    : {LANGUAGES.get(parsed.ot_language, '?')}")
    print(f"Nature      : {parsed.nature}  (id={parsed.nature_id})")
    print(f"Gender      : {parsed.gender}     Form: {parsed.form}")
    print(f"Ability     : id={parsed.ability_id} num={parsed.ability_num}")
    print(f"PID         : {parsed.pid:#010x}   EncKey: {parsed.encryption_key:#010x}")
    print(f"IVs         : {parsed.ivs}")
    print(f"EVs         : {parsed.evs}")
    print(f"Moves       : {parsed.moves} (PP {parsed.move_pp})")
    print(f"Shiny?      : {parsed.shiny}   (TSV={parsed.tsv}, PSV={parsed.psv})")
    print(f"Met level   : {parsed.met_level}    Pokéball: {parsed.pokeball}")
    print(f"Checksum    : stored={parsed.checksum_stored:#06x} "
          f"computed={parsed.checksum_computed:#06x} "
          f"valid={parsed.checksum_valid}")
    if parsed.party:
        print(f"Party stats : {parsed.party}")


if __name__ == "__main__":
    _self_test()


# --------------------------------------------------------------------------
# How this slots into a future "pokebot-3ds"
# --------------------------------------------------------------------------
#
# Azahar (the active Citra fork) ships a Python module
# `dist/scripting/citra.py` that talks to the running emulator over UDP.
# The minimal integration looks like:
#
#     from citra import Citra
#     from pk67_parser import decrypt_pkm, parse_pkm
#
#     c = Citra()
#     c.set_active_process()                 # attach to the game
#     PARTY_BASE = 0x........                # per-game / per-version
#     PARTY_STRIDE = 484                     # 484-byte slot in Gen 6/7 RAM
#                                            #   (260 PK7 + 224 stat block)
#                                            # NB: this is the in-RAM
#                                            #   "Pokémon" struct, not the
#                                            #   pure PK7 buffer; party slots
#                                            #   are usually backed by a
#                                            #   contiguous PK7 plus extra
#                                            #   battle scratch. Verify per
#                                            #   game with PKHeX live mode.
#
#     for slot in range(6):
#         raw = c.read_memory(PARTY_BASE + slot * PARTY_STRIDE, 260)
#         pkm = parse_pkm(decrypt_pkm(raw))
#         print(slot, pkm.nickname, pkm.species, pkm.shiny)
#
# What we still need for a feature-equivalent pokebot-3ds:
#   1. PARTY_BASE / WILD_BASE / battle-state addresses per game per region
#      (X, Y, OR, AS, S, M, US, UM). PKHeX's `LiveHeX` source lists most
#      of these for SM/USUM.
#   2. Game-state machine logic per game (encounter, fishing, gift resets,
#      starter resets, hatching, SOS chains for Gen 7, ...).
#   3. Input layer: Azahar's RPC supports button/touchscreen events; needs
#      a frame-pacing helper analogous to pokebot-nds's joypad.set loop.
#   4. Dashboard: same architecture as pokebot-nds works fine -- a Python
#      broker process speaking to a Node/HTML dashboard over websockets.
