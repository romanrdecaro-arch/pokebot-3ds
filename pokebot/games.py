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
    # Party (your team). The save-block party in Gen 6/7 is six
    # contiguous party-format PK6/PK7 records of 0x104 = 260 bytes
    # each. Confirmed on Y-USA: slot 0 = a clean 260-byte record.
    # (484 was a wrong Gen-7-battle-structure guess.)
    party_base:     int = 0   # first party slot (260-byte PK7)
    party_stride:   int = 260 # bytes between party slot N and N+1
    party_count:    int = 0   # u8 byte: how many slots are filled

    # Wild / battle foe (the Pokémon currently fighting you). In Gen 6
    # the encounter is NOT at a fixed sub-offset — PKMN-NTR reads a
    # ~128 KB window at WildOffset1 and pattern-matches the PK6. So
    # foe_base is the WINDOW START and foe_scan_len is how far to scan.
    foe_base:       int = 0   # WildOffset1 region start (scan, don't index)
    foe_stride:     int = 260
    foe_scan_len:   int = 0x20000  # bytes to scan from foe_base for a PK6
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
    generation: int                       # 2 (VC), 6 or 7
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


# ---- Gen 2: Crystal (3DS Virtual Console) ------------------------------
_register(Game(
    key="CRYSTAL-USA",
    title="Pokémon Crystal (US, Virtual Console)",
    # Read off a running Azahar session: ProcessList reported this
    # title id with process name "trl" (the GB VC emulator), which is
    # why the Gen 6/7 attach never recognised it.
    title_ids=(0x0004000000172800,),
    generation=2,
    offsets=GameOffsets(),
    notes=("Virtual Console: a Game Boy emulator inside the 3DS "
           "process, so Crystal's own addresses are NOT 3DS addresses. "
           "Its WRAM base is discovered at runtime by pokebot.crystal, "
           "not stored here. Gen 2 records are unencrypted 48-byte "
           "structures and shininess is DV-based — none of the "
           "PK6/PK7 offsets above apply."),
))

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
#
# party_base is DERIVED rather than typed in: trainer_block + 0x16C is
# the relationship confirmed against a running Y-USA, and the ORAS
# trainer block is a published LiveHeX address. Writing the sum here
# by hand would let the two drift apart silently.
#
# Cross-checked against ProjectPokemon's ORAS save structure
# (projectpokemon.org/home/docs/gen-6/oras-save-structure-r80/):
#   * Trainer Card 0x19400 (len 0x170) is immediately followed by
#     Party 0x19600 -- the same arrangement as X/Y, which is what
#     makes X/Y's confirmed +0x16C carry over.
#   * Party len 0x61C = 6 x 260 + 4: 260-byte party slots.
#   * Box data: 31 x 30 slots of 232 bytes.
# Those are SAVE-FILE offsets. RAM is not the file with its 0x200
# padding stripped: packing the listed blocks back to back puts the
# box 0xB24 short of the published RAM gap (0x1CDF4). So the page
# supports the relative offset, not the absolute address -- the
# first live run is what confirms that.
#
# Leaving these empty is what caused the bug this block fixes. An
# empty GameOffsets is not "use sensible defaults", it is "whatever
# config.yaml says" -- and config.yaml ships X/Y's addresses, so
# picking Omega Ruby quietly ran it on Pokemon Y's memory map.
_ORAS_TB = 0x08C81340                   # see LIVEHEX_REFERENCES below

#: ORAS wild-encounter window.
#:
#: PKMN-NTR lists the same WildOffset1 for ORAS as for X/Y, and the
#: README has carried that claim as "same code path, not user-tested"
#: for a while. It is UNVERIFIED here. It does not affect the soft
#: reset hunts, which read the party and never touch this -- but an
#: encounter hunt on ORAS should be treated as unproven until someone
#: confirms a wild is actually found in this window.
_ORAS_FOE_BASE = 0x08800000

_register(Game(
    key="OR-USA",
    title="Pokémon Omega Ruby (US)",
    title_ids=(0x000400000011C400,),
    generation=6,
    offsets=GameOffsets(
        party_base=_ORAS_TB + 0x16C,
        foe_base=_ORAS_FOE_BASE,
    ),
    notes="Party address derived from the published LiveHeX trainer "
          "block. foe_base is X/Y's, per PKMN-NTR — unverified on "
          "ORAS, so encounter modes are unproven here.",
))
_register(Game(
    key="AS-USA",
    title="Pokémon Alpha Sapphire (US)",
    title_ids=(0x000400000011C500,),
    generation=6,
    offsets=GameOffsets(
        party_base=_ORAS_TB + 0x16C,
        foe_base=_ORAS_FOE_BASE,
    ),
    notes="Same engine and same save layout as Omega Ruby.",
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


#: Where the starter is chosen, in the words the player needs to find
#: the spot. The hunt mechanics are identical across these games --
#: hold a direction, mash A, wait for the party to stop being empty --
#: but telling a Hoenn player to stand at "the starter table" sends
#: them looking for a Kalos room.
STARTER_SPOT = {
    "X-USA": "the starter table in Aquacorde Town",
    "Y-USA": "the starter table in Aquacorde Town",
    "OR-USA": "Professor Birch's bag on Route 101",
    "AS-USA": "Professor Birch's bag on Route 101",
}


def starter_spot(game_key: str) -> str:
    return STARTER_SPOT.get(game_key, "where the starter is chosen")


def starters_for(game_key: str) -> dict[str, int]:
    return STARTERS.get(game_key, {})


def starter_species(game_key: str, name: str) -> Optional[int]:
    return STARTERS.get(game_key, {}).get(name.lower())


# --------------------------------------------------------------------
# Soft-reset target catalogue.
#
# "Starters" is the always-present option — the bot picks one of the
# three lab starters and resets until the species + target filter
# match. X/Y additionally support per-encounter soft-resets for the
# in-game GIFT / STATIC legendaries (Snorlax on Route 7, Lucario
# from Korrina, Lapras at Ambrette/Sea Spirit's Den). Other games
# can add their own list as the sequences are implemented.
# --------------------------------------------------------------------

SOFT_RESET_TARGETS = {
    # Lucario (Korrina's gift at Tower of Mastery) is SHINY-LOCKED in
    # X/Y — the cutscene script presets its PID, so soft-resetting
    # has no chance of producing one. Omitted from the dropdown.
    "X-USA":  ["Starters", "Snorlax", "Lapras"],
    "Y-USA":  ["Starters", "Snorlax", "Lapras"],
    # ORAS gets Starters only, and says so rather than falling through
    # to the default. Snorlax (Route 7) and Lapras (Route 12) are
    # KALOS encounters -- offering them here would start a hunt with
    # nothing to find. Hoenn has plenty of its own static legendaries;
    # none has had its sequence written and verified yet.
    "OR-USA": ["Starters"],
    "AS-USA": ["Starters"],
}


def soft_reset_targets_for(game_key: str) -> list[str]:
    return SOFT_RESET_TARGETS.get(game_key, ["Starters"])


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
    movement: Optional[str] = None   # "horizontal" | "vertical" (encounter only)
    shiny_locked: bool = False       # flagged in the UI before starting
    notes: str = ""


#: Games the place-specific modes below were built against.
_KALOS_ONLY_GAMES = ("X-USA", "Y-USA")
_KALOS_ONLY_MODES = ("noibat",)


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

    Generation 2 (Virtual Console) games get an EMPTY list. Every mode
    below reads PK6/PK7 records over Azahar's RPC at 3DS addresses,
    and a VC title has neither — its Game Boy RAM is nested inside the
    VC emulator and its records are 48-byte Gen 2 structures. Offering
    these would let the launcher start a hunt that cannot possibly
    work; Crystal is read with scripts/crystal_watch.py for now.
    """
    game = GAMES.get(game_key)
    if game is not None and game.generation == 2:
        # Gen 2 gets its own manual mode. The Gen 6/7 methods below all
        # read PK6/PK7 records at 3DS addresses; a Virtual Console title
        # has neither, so offering them would start a hunt that cannot
        # work. Automated Gen 2 hunting is not built yet.
        return [
            Method("Manual control", "crystal_observe",
                   notes="Bot sends NO inputs — you play normally. It "
                         "watches Crystal's memory and reports your "
                         "party, wild battles, and anything shiny "
                         "(Gen 2 shininess is DV-based, 1 in 8192)."),
            Method("Random encounters (horizontal)", "crystal_encounter",
                   movement="horizontal",
                   notes="Walks Left/Right to trigger wild battles, "
                         "checks each one and runs from the rest."),
            Method("Random encounters (vertical)", "crystal_encounter",
                   movement="vertical",
                   notes="Walks Up/Down to trigger wild battles, "
                         "checks each one and runs from the rest."),
            Method("Celebi soft-reset hunt", "crystal_celebi",
                   notes="Save in front of the Ilex Forest shrine with "
                         "the GS Ball placed. Spams A until Celebi "
                         "appears, checks its DVs, and soft-resets if "
                         "it is not shiny. STOPS COMPLETELY on a shiny "
                         "so you can catch it yourself."),
        ]
    if game is not None and game.generation < 6:
        return []

    methods = [
        Method("Manual control", "observe",
               notes="Bot sends NO inputs — you play normally. The "
                     "Recently Seen tab still logs wild encounters and "
                     "party additions as they happen."),
        Method("LiveHeX bridge (PKHeX)", "livehex",
               notes="Sends NO inputs. Runs an NTR-protocol bridge on "
                     "port 8000 so PKHeX + PKHeX-Plugins LiveHeX can "
                     "read/write the running Azahar game. In PKHeX: "
                     "Auto-Legality → LiveHeX → protocol NTR, IP "
                     "127.0.0.1, port 8000, Connect."),
        Method("Soft reset", "soft_reset",
               notes="Resets the game until a target candidate appears. "
                     "Pick what to reset for from the Target "
                     "sub-dropdown. " + (
                         "X/Y also support Snorlax and Lapras."
                         if game_key in ("X-USA", "Y-USA")
                         else "Starters only for this game — the other "
                              "targets are Kalos encounters.")),
        Method("Gift Pokémon (soft reset)", "gifts",
               notes="Soft-resets for ANY gift Pokémon — Lapras from "
                     "the Route 12 Hiker, the bike-shop Eevee, fossil "
                     "revivals, in-game trades. Mashes A as fast as "
                     "Azahar registers, stops the instant something new "
                     "lands in your party, and resets unless it is a "
                     "shiny / target. Needs an OPEN PARTY SLOT and a "
                     "save made in front of the giver."),
        Method("Random encounters", "encounter",
               notes="Walks back-and-forth in tall grass on the chosen "
                     "axis. Every wild Pokémon is recorded to the "
                     "Recently Seen tab; non-targets are fled and the "
                     "bot resumes walking. Requires foe_base + "
                     "in_battle_flag offsets — run Debug first if "
                     "those aren't set."),
        Method("Fishing", "fishing",
               notes="Casts a registered fishing rod (Y) and hooks "
                     "bites with rapid A presses. Foe-window detection "
                     "picks up the wild; misses recast automatically. "
                     "Requires a rod registered to Y and the player "
                     "facing fishable water."),
        Method("Noibat (shaking spot)", "noibat",
               notes="Soft-reset hunt for the Terminus Cave shaking "
                     "spot. Holds the direction you pick to walk into "
                     "it, reads the wild, and resets unless it is a "
                     "SHINY NOIBAT — a shiny of any other species is "
                     "reset over too (its .pk6 is saved first). Needs "
                     "a save made standing beside the spot, facing it."),
        Method("Rock Smash", "rock_smash",
               notes="Presses A at the breakable rock in front of you "
                     "and stops the instant a wild appears, so it never "
                     "attacks a shiny. No shiny (or no encounter within "
                     "15s) soft-resets the game, which is what respawns "
                     "the rock. Requires a party member that knows Rock "
                     "Smash and a SAVE made facing a breakable rock — "
                     "every attempt restarts from that save. A catch "
                     "stops the hunt so you can save it."),
        Method("Sweet Scent (hordes)", "sweet_scent",
               notes="Uses Sweet Scent from the party menu to pull a "
                     "guaranteed 5-mon horde, so every battle is ~5× "
                     "the shiny chance of a single encounter. All five "
                     "are reported and the bot acts on ANY shiny among "
                     "them; detection, the flee and the catch are the "
                     "Random-encounter ones unchanged. Needs a party "
                     "member that knows Sweet Scent and a horde-enabled "
                     "route (Route 5+; routes 1-3 have no horde table)."),
        Method("Debug — find offsets", "debug",
               notes="One-shot offset bootstrap. Sends NO inputs. "
                     "Brute-force scans memory for party_base, then "
                     "caches the trainer-name anchor offset to "
                     "config.yaml. Run once with a Pokémon in slot 0; "
                     "after that the bot uses the fast anchor path."),
    ]

    # Noibat is the one mode written against a specific PLACE: its
    # defaults are a Terminus Cave shaking spot and species 714, and
    # neither has been built or checked for any game but X/Y. This is
    # a statement about the code, not about what lives in Hoenn --
    # offering it elsewhere would start a hunt aimed at a spot the
    # save is nowhere near.
    if game_key not in _KALOS_ONLY_GAMES:
        methods = [m for m in methods if m.mode not in _KALOS_ONLY_MODES]
    return methods


# 3DS virtual address ranges. Where the player's party block lives
# depends on the game:
#
#   - Gen 6 (X/Y, OR/AS) — O3DS titles. The save block (trainer card,
#     party, boxes) is allocated by the game in the application heap
#     around 0x08000000-0x10000000. PKHeX-Plugins LiveHeX confirms
#     trainer_block @ 0x08C79C3C and box1_slot1 @ 0x08C861C8 for
#     X/Y v1.5. The linear heap (0x14000000+) holds graphics scratch
#     and battle effect buffers, not party data.
#   - Gen 7 (S/M, US/UM) — N3DS-only titles. Party data lives in the
#     extended linear heap at 0x30000000 - 0x40000000.
#
# Scanning the right range matters: targeting the linear heap on X/Y
# (which the bot did pre-2026-05-10) returns zero hits because the
# data simply isn't there.
#: Fallback in-game OT name, used as a RAM anchor when the config
#: does not set ``trainer_name``. This was the maintainer's own name
#: hardcoded in eight places; it is a placeholder, not a useful
#: default — set ``trainer_name`` in config.yaml to your real OT name
#: so the anchor scan can actually find your party.
DEFAULT_OT_NAME = "TRAINER"


HEAP_RANGE_3DS         = (0x08000000, 0x40000000)   # all-3DS catch-all
APP_HEAP_RANGE_3DS     = (0x08000000, 0x10000000)   # Gen 6 save-block region
APP_HEAP_HOT_3DS       = (0x08000000, 0x0A000000)   # Gen 6 hot 32 MB
LINEAR_HEAP_RANGE_3DS  = (0x14000000, 0x20000000)   # gfx scratch / FX buffers
LINEAR_HEAP_HOT_3DS    = (0x14000000, 0x18000000)
EXT_HEAP_RANGE_N3DS    = (0x30000000, 0x40000000)   # Gen 7

#: Where the Game Boy Virtual Console emulator ("trl") keeps the
#: emulated WRAM. Confirmed by locating Crystal's party block at
#: 0x08a2ffac and 0x08a3bf1a on separate runs — both inside the first
#: 16 MB of the application heap, which is what makes a small hot band
#: worth trying before anything wider.
GB_VC_HOT_3DS          = (0x08000000, 0x09000000)   # Gen 2 VC hot 16 MB


def heap_range_for(gen: int) -> tuple[int, int]:
    """Return the most-likely heap range for party data.

    For Gen 6 we return the hot 32 MB at the start of the app heap —
    every published LiveHeX party_base for X/Y / OR/AS lives in
    0x08C00000-0x08D00000 — so a small targeted scan finishes fast
    AND has near-zero noise from unmapped pages.

    Callers that want broader coverage should use
    ``scan_ranges_for(gen)`` which returns a list of fallback
    ranges in priority order.
    """
    if gen == 6:
        return APP_HEAP_HOT_3DS
    return EXT_HEAP_RANGE_N3DS


def scan_ranges_for(gen: int) -> list[tuple[int, int]]:
    """Heap ranges to scan, in priority order.

    Callers walk this list, scanning each region until they find what
    they need. Lets us start with the hot region (fast) and fall back
    to wider coverage only when the targeted scan misses.

    Gen 6:
      1. APP_HEAP_HOT_3DS    32 MB centred on the LiveHeX save block
      2. APP_HEAP_RANGE_3DS  full 128 MB application heap
      3. LINEAR_HEAP_HOT_3DS 64 MB graphics scratch (battle FX may live here)

    Gen 7: just the extended heap — that's where everything lives.
    """
    if gen == 2:
        return GB_VC_SCAN_RANGES
    if gen == 6:
        return [APP_HEAP_HOT_3DS, APP_HEAP_RANGE_3DS, LINEAR_HEAP_HOT_3DS]
    return [EXT_HEAP_RANGE_N3DS]


#: Ranges the Crystal scanner walks, narrowest first.
#:
#: Deliberately NOT ``HEAP_RANGE_3DS``. That catch-all spans 896 MB,
#: which at Azahar's 1024-byte-per-read RPC is 917,504 round trips, and
#: it covers the linear heap and VRAM (0x14000000-0x20000000) — pages
#: the renderer owns, that a Game Boy title never touches, and that were
#: measured returning "unmapped ReadBlock" for every single request.
#: Sweeping it once wrote 100 MB of emulator log in 21 seconds and took
#: Azahar down with it.
GB_VC_SCAN_RANGES = [GB_VC_HOT_3DS, APP_HEAP_RANGE_3DS]


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


def resolve_offsets(game_key: str, offset_cfg: dict | None) -> dict:
    """The config offset overrides that apply to THIS game.

    ``offsets:`` in config.yaml was a single flat block applied to
    whatever game was selected, and the shipped values are X/Y's. So
    choosing Omega Ruby ran it on Pokemon Y's memory map -- silently,
    because an address that is merely wrong reads as "nothing found"
    rather than as an error.

    The block now has two parts, and either may be omitted::

        offsets:
          party_base: 0x08CE1CF8        # flat: the default
          OR-USA:
            party_base: 0x08C814AC      # per game: wins for that one

    Flat keys still apply, because a user editing them to fix a bad
    address expects to be obeyed. What changed is that a flat key is
    only honoured when it does not contradict a game that ships its
    own: a registry address the maintainers derived for ORAS beats a
    flat value written for Kalos, while a flat value still works
    unchanged for any game whose registry entry is empty.

    Returns the EFFECTIVE offsets for this game -- the registry's own
    values overlaid with whatever config legitimately overrides. Not
    "the overrides", because the question a caller actually has is
    "what addresses will this run use", and answering the narrower
    one is how a game ended up running on another game's map without
    anybody noticing.

    0 and unparseable values are dropped rather than applied: 0 is the
    registry's "unknown" sentinel, so letting it through would blank
    out a known address.
    """
    cfg = dict(offset_cfg or {})
    game = GAMES.get(game_key)
    known = game.offsets if game is not None else None

    def parse(val):
        try:
            return int(val, 0) if isinstance(val, str) else int(val)
        except (TypeError, ValueError):
            return None

    per_game = {}
    flat = {}
    for key, val in cfg.items():
        if isinstance(val, dict):
            if key == game_key:
                per_game = val
            # Any other game's block is simply not ours.
            continue
        flat[key] = val

    # Start from what the game itself declares.
    out: dict = {}
    if known is not None:
        for key in vars(known):
            val = getattr(known, key, 0)
            if isinstance(val, int) and val:
                out[key] = val

    for key, val in flat.items():
        if known is not None and getattr(known, key, 0):
            # This game ships its own value for this field, so a flat
            # block written for a different game must not overwrite
            # it. The per-game block below is how you override on
            # purpose.
            continue
        parsed = parse(val)
        if parsed:
            out[key] = parsed
    for key, val in (per_game or {}).items():
        parsed = parse(val)
        if parsed:
            out[key] = parsed
    return out


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
        # CONFIRMED for Y-USA (2026-05-15): party slot 0 was found at
        # trainer_block + 0x16C — a strict PK6 record decoding to the
        # player's Fennekin (#653). The older +0x200 / +0x170 guesses
        # were wrong. 0x16C is tried first; the rest stay as fallbacks
        # for X / OR / AS until each is independently confirmed.
        return [
            tb + 0x16C,         # CONFIRMED Y-USA
            tb + 0x200,
            tb + 0x170,
            b1 - 0xE400,
            b1 - 0xC58C,
        ]
    # Gen 7 (SM / USUM) save layout differs significantly. These are
    # UNVERIFIED guesses around the trainer block — none has been
    # confirmed against a running game yet (unlike Y-USA's tb+0x16C
    # above). Each is single-read validated by is_likely_pk7() before
    # use, so a wrong guess is harmless: it's rejected and discovery
    # falls through to the trainer-name anchor path / manual
    # Memory-Viewer steps.
    # TODO: confirm one on a live SM/USUM session and mark it CONFIRMED.
    return [tb + 0xC0, tb + 0x140, b1 - 0x3000]
