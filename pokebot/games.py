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

    # UI state — flips when a dialog box / menu / interruptive prompt
    # is on screen (and back to 0 when the player can move). Found via
    # `python -m pokebot.find_dialog_flag`.
    dialog_flag:    int = 0

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
    """
    return [
        Method("Starters", "soft_reset"),
        Method("Manual control", "observe",
               notes="Bot sends NO inputs — you play normally. The "
                     "Recently Seen tab still logs wild encounters and "
                     "party additions as they happen."),
    ]


# 3DS virtual address ranges. The application heap (FCRAM mapped) spans
# roughly 0x08000000 - 0x40000000 from the game's perspective. The bulk
# of game-managed objects (including the player's party) live in the
# 0x30000000 - 0x40000000 range on N3DS-specific extended-memory titles
# (which is all of Gen 6/7).
HEAP_RANGE_3DS = (0x08000000, 0x40000000)
EXT_HEAP_RANGE_N3DS = (0x30000000, 0x40000000)
