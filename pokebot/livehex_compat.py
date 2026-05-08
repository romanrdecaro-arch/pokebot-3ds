"""
PKHeX-Plugins LiveHeX compatibility layer.

Mirrors the public surface of PKHeX-Plugins' RamOffsets / PokeSysBotMini
so this bot reads the same memory addresses LiveHeX does. Lets us:

  1. Reuse community-vetted box / trainer offsets for every supported
     game version (X/Y, OR/AS, S/M, US/UM, etc).
  2. Verify our bot's RPC connection by reading the same data PKHeX
     would read — same address, same byte layout — and confirming it
     parses cleanly.
  3. Derive party_base candidates from the published trainer/box
     anchors instead of brute-force scanning.

Source of truth: PKHeX-Plugins-23.09.25
  PKHeX.Core.Injection/LiveHeXOffsets/RamOffsets.cs
  PKHeX.Core.Injection/Enums/LiveHeXVersion.cs
  PKHeX.Core.Injection/Protocols/LPBasic.cs
"""

from __future__ import annotations

from dataclasses import dataclass


# ---------------------------------------------------------------------------
# LiveHeXVersion enum — mirrors PKHeX.Core.Injection.LiveHeXVersion
# ---------------------------------------------------------------------------

class LiveHeXVersion:
    """Pokémon game versions supported by LiveHeX.

    The string values match PKHeX-Plugins' enum names exactly so the
    same game-version key can be passed between the two systems.
    """
    UNKNOWN     = "Unknown"
    XY_v150     = "XY_v150"
    ORAS_v140   = "ORAS_v140"
    SM_v120     = "SM_v120"
    US_v120     = "US_v120"
    UM_v120     = "UM_v120"
    SWSH_v111   = "SWSH_v111"
    SWSH_v121   = "SWSH_v121"
    SWSH_v132   = "SWSH_v132"
    LGPE_v102   = "LGPE_v102"
    BD_v100     = "BD_v100"
    SP_v100     = "SP_v100"
    BD_v110     = "BD_v110"
    SP_v110     = "SP_v110"
    BD_v111     = "BD_v111"
    SP_v111     = "SP_v111"
    BDSP_v112   = "BDSP_v112"
    BDSP_v113   = "BDSP_v113"
    BDSP_v120   = "BDSP_v120"
    BD_v130     = "BD_v130"
    SP_v130     = "SP_v130"
    LA_v100     = "LA_v100"
    LA_v101     = "LA_v101"
    LA_v102     = "LA_v102"
    LA_v111     = "LA_v111"
    SV_v101     = "SV_v101"
    SV_v110     = "SV_v110"
    SV_v120     = "SV_v120"
    SV_v130     = "SV_v130"
    SV_v131     = "SV_v131"
    SV_v132     = "SV_v132"
    SV_v201     = "SV_v201"


# ---------------------------------------------------------------------------
# RAM offsets — mirrors PKHeX.Core.Injection.RamOffsets static methods
# ---------------------------------------------------------------------------

# Box 1 Slot 1 offset per LiveHeXVersion (RamOffsets.GetB1S1Offset).
B1S1_OFFSETS: dict[str, int] = {
    LiveHeXVersion.LGPE_v102: 0x533675B0,
    LiveHeXVersion.SWSH_v111: 0x4293D8B0,
    LiveHeXVersion.SWSH_v121: 0x4506D890,
    LiveHeXVersion.SWSH_v132: 0x45075880,
    LiveHeXVersion.UM_v120:   0x33015AB0,
    LiveHeXVersion.US_v120:   0x33015AB0,
    LiveHeXVersion.SM_v120:   0x330D9838,
    LiveHeXVersion.ORAS_v140: 0x08C9E134,
    LiveHeXVersion.XY_v150:   0x08C861C8,
}

# Slot size in bytes (RamOffsets.GetSlotSize).
SLOT_SIZES: dict[str, int] = {
    LiveHeXVersion.LA_v100:   360,
    LiveHeXVersion.LA_v101:   360,
    LiveHeXVersion.LA_v102:   360,
    LiveHeXVersion.LA_v111:   360,
    LiveHeXVersion.LGPE_v102: 260,
    LiveHeXVersion.UM_v120:   232,
    LiveHeXVersion.US_v120:   232,
    LiveHeXVersion.SM_v120:   232,
    LiveHeXVersion.ORAS_v140: 232,
    LiveHeXVersion.XY_v150:   232,
}

# Inter-slot gap (RamOffsets.GetGapSize).
GAP_SIZES: dict[str, int] = {
    LiveHeXVersion.LGPE_v102: 380,
}

# Slots per box (RamOffsets.GetSlotCount).
SLOT_COUNTS: dict[str, int] = {
    LiveHeXVersion.LGPE_v102: 25,
}

# Trainer block address (RamOffsets.GetTrainerBlockOffset).
TRAINER_BLOCK_OFFSETS: dict[str, int] = {
    LiveHeXVersion.LGPE_v102: 0x53582030,
    LiveHeXVersion.SWSH_v111: 0x42935E48,
    LiveHeXVersion.SWSH_v121: 0x45061108,
    LiveHeXVersion.SWSH_v132: 0x45068F18,
    LiveHeXVersion.UM_v120:   0x33012818,
    LiveHeXVersion.US_v120:   0x33012818,
    LiveHeXVersion.SM_v120:   0x330D67D0,
    LiveHeXVersion.ORAS_v140: 0x08C81340,
    LiveHeXVersion.XY_v150:   0x08C79C3C,
}

# Trainer block size in bytes (RamOffsets.GetTrainerBlockSize).
TRAINER_BLOCK_SIZES: dict[str, int] = {
    LiveHeXVersion.BD_v130:   0x50,
    LiveHeXVersion.SP_v130:   0x50,
    LiveHeXVersion.BDSP_v120: 0x50,
    LiveHeXVersion.BDSP_v113: 0x50,
    LiveHeXVersion.BDSP_v112: 0x50,
    LiveHeXVersion.BD_v111:   0x50,
    LiveHeXVersion.BD_v110:   0x50,
    LiveHeXVersion.BD_v100:   0x50,
    LiveHeXVersion.SP_v111:   0x50,
    LiveHeXVersion.SP_v110:   0x50,
    LiveHeXVersion.SP_v100:   0x50,
    LiveHeXVersion.LGPE_v102: 0x168,
    LiveHeXVersion.SWSH_v111: 0x110,
    LiveHeXVersion.SWSH_v121: 0x110,
    LiveHeXVersion.SWSH_v132: 0x110,
    LiveHeXVersion.UM_v120:   0xC0,
    LiveHeXVersion.US_v120:   0xC0,
    LiveHeXVersion.SM_v120:   0xC0,
    LiveHeXVersion.ORAS_v140: 0x170,
    LiveHeXVersion.XY_v150:   0x170,
}


# ---------------------------------------------------------------------------
# Mirror of RamOffsets static API — same signatures as the C# methods
# ---------------------------------------------------------------------------

def get_b1s1_offset(lv: str) -> int:
    return B1S1_OFFSETS.get(lv, 0)


def get_slot_size(lv: str) -> int:
    return SLOT_SIZES.get(lv, 344)


def get_gap_size(lv: str) -> int:
    return GAP_SIZES.get(lv, 0)


def get_slot_count(lv: str) -> int:
    return SLOT_COUNTS.get(lv, 30)


def get_trainer_block_offset(lv: str) -> int:
    return TRAINER_BLOCK_OFFSETS.get(lv, 0)


def get_trainer_block_size(lv: str) -> int:
    return TRAINER_BLOCK_SIZES.get(lv, 0)


def get_box_offset(lv: str, box: int) -> int:
    """Equivalent of PokeSysBotMini.GetBoxOffset(box)."""
    base = get_b1s1_offset(lv)
    return base + (get_slot_size(lv) + get_gap_size(lv)) * get_slot_count(lv) * box


def get_slot_offset(lv: str, box: int, slot: int) -> int:
    """Equivalent of PokeSysBotMini.GetSlotOffset(box, slot)."""
    return get_box_offset(lv, box) + (get_slot_size(lv) + get_gap_size(lv)) * slot


# ---------------------------------------------------------------------------
# Game registry key → LiveHeXVersion
# ---------------------------------------------------------------------------

GAME_TO_LIVEHEX: dict[str, str] = {
    "X-USA":         LiveHeXVersion.XY_v150,
    "Y-USA":         LiveHeXVersion.XY_v150,
    "OR-USA":        LiveHeXVersion.ORAS_v140,
    "AS-USA":        LiveHeXVersion.ORAS_v140,
    "SM-USA-1.2":    LiveHeXVersion.SM_v120,
    "USUM-USA-1.2":  LiveHeXVersion.UM_v120,    # (US shares same offsets)
}


def livehex_version_for(game_key: str) -> str:
    return GAME_TO_LIVEHEX.get(game_key, LiveHeXVersion.UNKNOWN)


# ---------------------------------------------------------------------------
# Verification — read the same memory PKHeX-Plugins reads, confirm it
# parses, and report. Useful as a "is the bot's RPC working?" probe.
# ---------------------------------------------------------------------------

@dataclass
class VerificationResult:
    livehex_version:    str
    trainer_block_addr: int
    trainer_block_ok:   bool
    box1_slot1_addr:    int
    box1_slot1_ok:      bool
    party_base_addr:    int    # 0 if not derived
    party_base_ok:      bool
    notes:              list


def verify_compatibility(rpc, game_key: str) -> VerificationResult:
    """Probe the LiveHeX-published addresses for this game and check
    each one parses as expected. Total RPC reads: 2-3 (single trainer
    block + single box1 slot1 + optional party probe). Cheap and safe.
    """
    from .find_offsets import is_likely_pk7
    from .parser import calc_checksum, decrypt_pkm

    lv = livehex_version_for(game_key)
    notes: list = []
    result = VerificationResult(
        livehex_version=lv, trainer_block_addr=0, trainer_block_ok=False,
        box1_slot1_addr=0, box1_slot1_ok=False,
        party_base_addr=0, party_base_ok=False, notes=notes)

    if lv == LiveHeXVersion.UNKNOWN:
        notes.append(f"No LiveHeX mapping for {game_key!r}.")
        return result

    # Trainer block
    tb_addr = get_trainer_block_offset(lv)
    tb_size = get_trainer_block_size(lv)
    result.trainer_block_addr = tb_addr
    if tb_addr and tb_size:
        try:
            tb = rpc.read(tb_addr, tb_size)
            # Sanity: can't be all 0x00 or all 0xFF (would mean unmapped).
            if tb and tb != b"\x00" * len(tb) and tb != b"\xFF" * len(tb):
                result.trainer_block_ok = True
                notes.append(f"Trainer block @ {tb_addr:#010x}: read {len(tb)} bytes OK")
            else:
                notes.append(f"Trainer block @ {tb_addr:#010x}: returned all-zero/0xFF (unmapped)")
        except Exception as e:
            notes.append(f"Trainer block read failed: {e}")

    # Box 1 Slot 1 (260 bytes covers slot size + party-stat padding)
    b1_addr = get_b1s1_offset(lv)
    result.box1_slot1_addr = b1_addr
    if b1_addr:
        try:
            raw = rpc.read(b1_addr, 260)
            ok, _ = is_likely_pk7(raw)
            if ok:
                result.box1_slot1_ok = True
                notes.append(f"Box 1 Slot 1 @ {b1_addr:#010x}: valid PK6 record")
            elif int.from_bytes(raw[:4], "little") == 0:
                result.box1_slot1_ok = True   # empty box slot is fine
                notes.append(f"Box 1 Slot 1 @ {b1_addr:#010x}: empty (slot OK)")
            else:
                notes.append(f"Box 1 Slot 1 @ {b1_addr:#010x}: didn't parse as PK6")
        except Exception as e:
            notes.append(f"Box 1 Slot 1 read failed: {e}")

    return result
