"""
Game registry and per-game RAM offsets.

How to use the offsets in this file:
  - The fields are the addresses of in-memory structures in the running
    game's process address space. Azahar's ReadMemory takes that address
    directly.
  - All addresses here are flagged with `verified=False` until you confirm
    them on a real running game. Use `pokebot.find_offsets` to scan.
  - The "stride" field is how far apart consecutive party slots sit in
    memory. In Gen 6/7 the in-RAM party slot is the same encrypted PK6/PK7
    structure (260 bytes), often padded to a round size. 484 is a common
    observed stride; verify per game.

How to find offsets yourself:
  1. Boot the game in Azahar with at least one Pokémon in your party.
  2. Run `python -m pokebot.find_offsets` while the game is on the
     overworld. The scanner brute-forces likely PK7 addresses by looking
     for buffers whose decrypted form has a valid checksum.
  3. Plug the discovered party_base into the entry below for your game.

References (community offset tables — verify against your version):
  - PKHeX LiveHeX (3DS NTR mode): https://github.com/architdate/PKHeX-Plugins
  - sumoCheatMenu: https://github.com/AnalogMan151/sumoCheatMenu
  - 3DSRNGTool source for static encounter offsets
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class GameOffsets:
    """Addresses of the structures we care about in this game's RAM.

    Use 0 for "unknown / not yet found"; modes that require an offset will
    refuse to start if it's still 0.
    """
    # Party (your team)
    party_base:     int = 0   # first party slot (260-byte PK7)
    party_stride:   int = 484 # bytes between party slot N and N+1
    party_count:    int = 0   # u8 byte: how many slots are filled

    # Wild / battle foe (the Pokémon currently fighting you)
    foe_base:       int = 0   # opposing Pokémon slot 1 (260-byte PK7)
    foe_stride:     int = 484
    foe_count:      int = 0   # u8: number of foes (1 single, 2 double, ...)

    # Battle state
    in_battle_flag: int = 0   # u8/u32 that flips when a battle starts
    battle_state:   int = 0   # broader state machine (menu, attack, etc.)

    # Overworld
    map_id:         int = 0
    player_x:       int = 0
    player_y:       int = 0

    # SOS chaining (Gen 7 only)
    sos_chain_len:  int = 0   # u8: current SOS chain length
    sos_state:      int = 0   # status block from gen7-ram-map

    # Misc
    rng_state:      int = 0   # SFMT state for RNG observation/manipulation
    save_block:     int = 0   # for save-block reads (rarely needed live)


@dataclass
class Game:
    """One specific game/region/version combination."""
    key: str                              # e.g. "USUM-USA-1.2"
    title: str                            # human-readable name
    title_ids: tuple                      # u64 title IDs that map to this entry
    generation: int                       # 6 or 7
    offsets: GameOffsets = field(default_factory=GameOffsets)
    verified: bool = False                # have THESE offsets been tested?
    notes: str = ""

    @property
    def display(self) -> str:
        flag = "✓" if self.verified else "✗"
        return f"[{flag}] {self.title}  ({self.key})"


# --------------------------------------------------------------------
# Registry. Add entries here as you confirm offsets.
#
# IMPORTANT: All offsets below are PLACEHOLDERS marked verified=False.
# They will not work until populated with values found via the offset
# finder or community sources. The scaffolding (party_stride, etc.) is
# correct for the format and shouldn't need changing.
# --------------------------------------------------------------------

GAMES: dict[str, Game] = {}


def _register(g: Game):
    GAMES[g.key] = g


# ---- Gen 6: X / Y ------------------------------------------------------
_register(Game(
    key="X-USA",
    title="Pokémon X (US)",
    title_ids=(0x0004000000055D00,),
    generation=6,
    offsets=GameOffsets(),
    notes="XY heap layout drifted between versions. Verify on v1.5 (final).",
))
_register(Game(
    key="Y-USA",
    title="Pokémon Y (US)",
    title_ids=(0x0004000000055E00,),
    generation=6,
    offsets=GameOffsets(),
    notes="Same engine as X; offsets may match or be very close.",
))

# ---- Gen 6: ORAS ------------------------------------------------------
_register(Game(
    key="OR-USA",
    title="Pokémon Omega Ruby (US)",
    title_ids=(0x000400000011C400,),
    generation=6,
    offsets=GameOffsets(),
))
_register(Game(
    key="AS-USA",
    title="Pokémon Alpha Sapphire (US)",
    title_ids=(0x000400000011C500,),
    generation=6,
    offsets=GameOffsets(),
))

# ---- Gen 7: SM --------------------------------------------------------
_register(Game(
    key="SM-USA-1.2",
    title="Pokémon Sun/Moon (US, v1.2)",
    title_ids=(0x0004000000164800, 0x0004000000175E00),
    generation=7,
    offsets=GameOffsets(
        # SOS state block address from projectpokemon.org's Gen7 RAM Map
        # (published as USUM addresses; SM's location differs):
        sos_state=0x30038C44,
    ),
    notes="Verified-public addresses: SOS status block (per Gen7 RAM Map). "
          "Party / foe addresses still need finder verification.",
))

# ---- Gen 7: USUM ------------------------------------------------------
_register(Game(
    key="USUM-USA-1.2",
    title="Pokémon Ultra Sun/Ultra Moon (US, v1.2)",
    title_ids=(0x00040000001B5000, 0x00040000001B5100),
    generation=7,
    offsets=GameOffsets(
        sos_state=0x30038E20,        # public
        # Berry-pile data (one example of a published address):
        # 0x32DE3208 -- not used for bot but proves we can reach FCRAM ranges
    ),
    notes="Verified-public: SOS status block. Party offset is well known "
          "in PKHeX LiveHeX source for v1.2; plug it in once confirmed.",
))


def find_game_by_title_id(tid: int) -> Optional[Game]:
    for g in GAMES.values():
        if tid in g.title_ids:
            return g
    return None


def list_games() -> list[Game]:
    return sorted(GAMES.values(), key=lambda g: g.key)


# --------------------------------------------------------------------
# Starter Pokémon per game (national-dex IDs).
# Keys are lowercase nicknames; the launcher uses them in its dropdown.
# --------------------------------------------------------------------

STARTERS: dict[str, dict[str, int]] = {
    "X-USA":         {"chespin": 650, "fennekin": 653, "froakie": 656},
    "Y-USA":         {"chespin": 650, "fennekin": 653, "froakie": 656},
    "OR-USA":        {"treecko": 252, "torchic": 255, "mudkip": 258},
    "AS-USA":        {"treecko": 252, "torchic": 255, "mudkip": 258},
    "SM-USA-1.2":    {"rowlet": 722,  "litten": 725,   "popplio": 728},
    "USUM-USA-1.2":  {"rowlet": 722,  "litten": 725,   "popplio": 728},
}


def starters_for(game_key: str) -> dict[str, int]:
    return STARTERS.get(game_key, {})


def starter_species(game_key: str, name: str) -> Optional[int]:
    return STARTERS.get(game_key, {}).get(name.lower())


# --------------------------------------------------------------------
# Per-game bot methods. Each method tells the launcher which bot mode
# to run, an optional starter constraint, and whether the target is
# shiny-locked by the game (so the UI can warn the user).
# --------------------------------------------------------------------

@dataclass
class Method:
    label: str                       # what the dropdown shows
    mode: str                        # "observe" | "encounter" | "soft_reset"
    starter: Optional[str] = None    # name from STARTERS for the game
    shiny_locked: bool = False       # flagged in the UI before starting
    notes: str = ""


def methods_for(game_key: str) -> list[Method]:
    """Bot methods available for this game.

    "Starters" runs the full automated soft-reset hunt and pairs with
    the launcher's starter sub-dropdown.

    "Manual control" runs observe mode — the bot sends no inputs at
    all, so the player drives Azahar themselves. Useful for hands-on
    play while still letting the launcher's "Recently Seen" panel pick
    up wild encounters and any Pokémon added to the party (gifts,
    starters, hatched eggs).

    "Debug — find offsets" runs a one-shot brute-force scan to
    discover party_base and cache the trainer-name anchor offset.
    Run this once after a fresh save (with at least one Pokémon in
    slot 0) so subsequent Starters / Manual runs can use the fast
    anchor path.
    """
    return [
        Method("Starters", "soft_reset"),
        Method("Manual control", "observe",
               notes="Bot sends NO inputs — you play normally. The "
                     "Recently Seen tab still logs wild encounters and "
                     "party additions as they happen."),
        Method("Debug — find offsets", "debug",
               notes="One-shot offset bootstrap. Sends NO inputs. "
                     "Brute-force scans memory for party_base, then "
                     "caches the trainer-name anchor offset to "
                     "config.yaml. Run once with a Pokémon in slot 0; "
                     "after that the bot uses the fast anchor path."),
    ]


# 3DS virtual address ranges. Where the player's party block lives
# depends on the game:
#
#   - Gen 7 (S/M, US/UM) — N3DS-only titles. Party data lives in the
#     extended linear heap at 0x30000000 - 0x40000000.
#   - Gen 6 (X/Y, OR/AS) — originally O3DS titles. Party data lives in
#     the standard linear heap at 0x14000000 - 0x20000000.
#
# Scanning the right range matters: targeting 0x30M+ for an X/Y session
# returns zero hits because the data simply isn't there.
HEAP_RANGE_3DS         = (0x08000000, 0x40000000)
LINEAR_HEAP_RANGE_3DS  = (0x14000000, 0x20000000)   # Gen 6 (O3DS) full
LINEAR_HEAP_HOT_3DS    = (0x14000000, 0x18000000)   # Gen 6 active region
EXT_HEAP_RANGE_N3DS    = (0x30000000, 0x40000000)   # Gen 7


def heap_range_for(gen: int) -> tuple[int, int]:
    """Return the heap range most likely to contain party data.

    For Gen 6 we return only the first 64 MB of the linear heap —
    where every published X/Y party_base address lives. The full
    128 MB linear range is the *fallback* if the hot region misses,
    avoiding ~half the unmapped-read log spam that Azahar emits
    during a wider scan.
    """
    if gen == 6:
        return LINEAR_HEAP_HOT_3DS
    return EXT_HEAP_RANGE_N3DS


# --------------------------------------------------------------------
# Known RAM reference points from PKHeX-Plugins LiveHeX
# (BotController/PokeSysBotMini.cs, LiveHeXOffsets/RamOffsets.cs)
# Pulled from PKHeX-Plugins-23.09.25.
#
# These are the RAM addresses LiveHeX uses for box / trainer data.
# The party block is adjacent to the trainer card in the save layout,
# so we generate candidate party_base addresses by adding the
# observed save-layout offsets to the trainer block address.
# --------------------------------------------------------------------

LIVEHEX_REFERENCES: dict[str, dict] = {
    # Pokémon X/Y v1.5 — final patch.
    "X-USA": {
        "trainer_block": 0x08C79C3C,    # size 0x170
        "box1_slot1":    0x08C861C8,    # 232-byte slots, 30 per box
        "version":       "XY_v150",
    },
    "Y-USA": {
        "trainer_block": 0x08C79C3C,
        "box1_slot1":    0x08C861C8,
        "version":       "XY_v150",
    },
    # Pokémon Omega Ruby / Alpha Sapphire v1.4.
    "OR-USA": {
        "trainer_block": 0x08C81340,
        "box1_slot1":    0x08C9E134,
        "version":       "ORAS_v140",
    },
    "AS-USA": {
        "trainer_block": 0x08C81340,
        "box1_slot1":    0x08C9E134,
        "version":       "ORAS_v140",
    },
    # Sun / Moon / USUM (USA) — extended heap, different layout.
    "SM-USA-1.2": {
        "trainer_block": 0x330D67D0,    # size 0xC0
        "box1_slot1":    0x330D9838,
        "version":       "SM_v120",
    },
    "USUM-USA-1.2": {
        "trainer_block": 0x33012818,    # size 0xC0
        "box1_slot1":    0x33015AB0,
        "version":       "UM_v120",
    },
}


def party_base_candidates(game_key: str) -> list[int]:
    """Return likely RAM addresses for party slot 0, ordered most→least
    likely. Derived from the trainer-block reference + observed
    save-layout offsets. Each is a single-read verification away from
    being confirmed.
    """
    ref = LIVEHEX_REFERENCES.get(game_key)
    if not ref:
        return []
    tb = ref["trainer_block"]
    b1 = ref["box1_slot1"]
    if game_key in ("X-USA", "Y-USA", "OR-USA", "AS-USA"):
        # Gen 6 save layout: trainer card 0x19400, party 0x19600,
        # box 0x27A00. Distances: trainer→party = 0x200,
        # party→box = 0xE400.
        return [
            tb + 0x200,         # mirror save spacing (most likely)
            tb + 0x170,         # right after trainer block
            b1 - 0xE400,        # mirror save party→box spacing
            b1 - 0xC58C,        # alternative: RAM trainer→box delta
        ]
    # Gen 7 save layout differs significantly; provide a few reasonable
    # guesses around the trainer block.
    return [tb + 0xC0, tb + 0x140, b1 - 0x3000]
