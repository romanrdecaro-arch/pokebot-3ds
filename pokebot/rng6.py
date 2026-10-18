"""
Gen 6 (X/Y) RNG: read the game's MT19937 live and predict what it makes.

Found by reverse engineering kujira code.bin and confirmed on a running
X in Azahar (2026-10-18):

* pml's Random32 / RandomBelow first check a hook table, and X/Y fill it
  (code.bin 0x0010D528 / 0x0010D4EC), so every Pokemon is built from the
  game's main MT19937. Its object pointer lives at 0x005EB51C.
* The object is ``u32 index; u32 mt[624]``. Game Freak's genrand
  (0x00119C48) regenerates one word per call in place, which yields the
  standard MT19937 output sequence. The index is always 0..623.
* RandomBelow(n) is ``(n * rand) >> 32`` for n < 0xFFFF.
* FillRandomSpec (0x0014F324) draws, in order: EC; PID, rerolled while
  not shiny up to ``pid_rolls`` times (3 with the Shiny Charm); one
  RandomBelow(6) per guaranteed-31 IV slot, redrawing taken slots; one
  RandomBelow(32) per remaining IV in HP/Atk/Def/SpA/SpD/Spe order;
  ability RandomBelow(2) (3 when a hidden ability is allowed); nature
  RandomBelow(25); gender RandomBelow(252), female when ``r + 1 <
  ratio``. Fixed fields draw nothing. Random forms are not modelled;
  they would sit between nature and gender.

Checked on the cartridge build of X: the live code image was identical
to the dumped code.bin apart from 19 import slots the CRO loader fills
in. Y's code.bin differs from X's by 28 bytes in place, so the addresses
are the same, but Y has not been checked live.

The MT advances constantly in the overworld (background activity; about
110 calls a second at normal speed where it was measured), so prediction
starts from a fresh read, not from the boot seed.
"""
from __future__ import annotations

import struct
from array import array
from dataclasses import dataclass
from typing import Callable, Iterator, Optional, Sequence

MT_N = 624
#: X/Y code.bin global holding the pointer to the main MT19937 object.
MAIN_MT_PTR_XY = 0x005EB51C
_MT_BYTES = 4 + 4 * MT_N
#: The pointer targets the 3DS application heap.
_HEAP_LO, _HEAP_HI = 0x08000000, 0x10000000
_U32 = 0xFFFFFFFF
#: The Shiny Charm makes 3; anything near this is a typo, and each roll
#: costs a draw at every scanned offset.
MAX_PID_ROLLS = 64

IV_NAMES = ("HP", "Atk", "Def", "SpA", "SpD", "Spe")


class RNGReadError(RuntimeError):
    """The live MT state could not be read, or is not the MT."""


class GFMT:
    """Game Freak's incremental MT19937 (kujira code.bin 0x00119C48)."""

    __slots__ = ("_i", "_mt")

    def __init__(self, index: int, words: Sequence[int]):
        if not 0 <= index < MT_N:
            raise ValueError(f"MT index {index} outside 0..{MT_N - 1}")
        if len(words) != MT_N:
            raise ValueError(f"MT state needs {MT_N} words, got {len(words)}")
        self._i = index
        self._mt = [w & _U32 for w in words]

    @classmethod
    def from_seed(cls, seed: int) -> "GFMT":
        """State after init_genrand(seed), as the game seeds it."""
        mt = [seed & _U32]
        for k in range(1, MT_N):
            prev = mt[-1]
            mt.append((1812433253 * (prev ^ (prev >> 30)) + k) & _U32)
        return cls(0, mt)

    @classmethod
    def from_bytes(cls, block: bytes) -> "GFMT":
        """Parse the game's 2,500-byte object (index, then the words)."""
        if len(block) != _MT_BYTES:
            raise ValueError(f"MT object is {_MT_BYTES} bytes, got {len(block)}")
        words = struct.unpack(f"<{MT_N + 1}I", block)
        return cls(words[0], words[1:])

    @property
    def index(self) -> int:
        return self._i

    @property
    def words(self) -> tuple[int, ...]:
        return tuple(self._mt)

    def copy(self) -> "GFMT":
        return GFMT(self._i, self._mt)

    def __eq__(self, other) -> bool:
        if not isinstance(other, GFMT):
            return NotImplemented
        return self._i == other._i and self._mt == other._mt

    __hash__ = None  # mutable

    def next(self) -> int:
        """Advance once and return the tempered output."""
        i, mt = self._i, self._mt
        j = i + 1 if i + 1 < MT_N else 0
        k = i + 397 if i + 397 < MT_N else i + 397 - MT_N
        y = (mt[i] & 0x80000000) | (mt[j] & 0x7FFFFFFF)
        v = mt[k] ^ (y >> 1) ^ (0x9908B0DF if mt[j] & 1 else 0)
        mt[i] = v
        self._i = j
        v ^= v >> 11
        v ^= (v << 7) & 0x9D2C5680
        v ^= (v << 15) & 0xEFC60000
        return v ^ (v >> 18)


def advances_between(a: GFMT, b: GFMT, limit: int) -> Optional[int]:
    """How many advances take state ``a`` to ``b`` (at most ``limit``)."""
    s = a.copy()
    for n in range(limit + 1):
        if s == b:
            return n
        s.next()
    return None


def random_below(r: int, n: int) -> int:
    """pml RandomBelow(n) from one 32-bit draw (code.bin 0x001122A8).

    Small bounds scale the draw; bounds of 0xFFFF and up fall back to a
    remainder. Nothing in Pokemon generation uses the second path."""
    if n < 1:
        raise ValueError("RandomBelow needs n >= 1")
    if n < 0xFFFF:
        return (n * r) >> 32
    return r % n


def is_shiny(pid: int, tid: int, sid: int) -> bool:
    """Gen 6 shiny check (code.bin 0x001688A4): XOR under 16."""
    return ((pid >> 16) ^ (pid & 0xFFFF) ^ tid ^ sid) < 16


@dataclass(frozen=True)
class GenSpec:
    """What the encounter fixes and what it leaves to the RNG.

    ``gender_ratio`` is the species' personal byte: 0 male only, 254
    female only, 255 genderless, otherwise the female threshold.
    ``nature`` is only for a nature known to be fixed (a gift that always
    has one); Synchronize in X/Y is a coin flip and is not modelled.
    """
    pid_rolls: int = 1
    perfect_ivs: int = 0
    hidden_ability: bool = False
    fixed_ability: Optional[int] = None
    nature: Optional[int] = None
    gender_ratio: int = 127

    def __post_init__(self):
        if not 1 <= self.pid_rolls <= MAX_PID_ROLLS:
            raise ValueError(f"pid_rolls must be 1..{MAX_PID_ROLLS}")
        if not 0 <= self.perfect_ivs <= 6:
            raise ValueError("perfect_ivs must be 0..6")
        if not 0 <= self.gender_ratio <= 255:
            raise ValueError("gender_ratio must be 0..255")
        if self.nature is not None and not 0 <= self.nature <= 24:
            raise ValueError("nature must be 0..24")
        if self.fixed_ability is not None and self.fixed_ability not in (0, 1, 2):
            raise ValueError("fixed_ability must be slot 0, 1 or 2")
        if self.fixed_ability is not None and self.hidden_ability:
            raise ValueError("hidden_ability means the slot is rolled; "
                             "it cannot be combined with fixed_ability")


@dataclass(frozen=True)
class Prediction:
    """The Pokemon one generation would produce.

    ``ivs`` is in generation order, HP/Atk/Def/SpA/SpD/Spe, which is not
    the PK6 storage order; compare against pokebot.parser with
    ``ivs_dict()``, which uses the same stat names it does."""
    ec: int
    pid: int
    shiny: bool
    ivs: tuple[int, ...]
    ability_slot: int
    nature: int
    gender: str                # "M", "F" or "G", as in pokebot.parser
    draws: int                 # MT outputs this generation consumed

    def ivs_dict(self) -> dict[str, int]:
        return dict(zip(IV_NAMES, self.ivs))


def generate(rand: Callable[[], int], spec: GenSpec, tid: int, sid: int) -> Prediction:
    """Run FillRandomSpec's draws against ``rand`` (one u32 per call)."""
    draws = 0

    def draw() -> int:
        nonlocal draws
        draws += 1
        return rand()

    ec = draw()
    pid = draw()
    for _ in range(spec.pid_rolls - 1):
        if is_shiny(pid, tid, sid):
            break
        pid = draw()

    ivs: list[Optional[int]] = [None] * 6
    for _ in range(spec.perfect_ivs):
        slot = random_below(draw(), 6)
        while ivs[slot] is not None:
            slot = random_below(draw(), 6)
        ivs[slot] = 31
    for slot in range(6):
        if ivs[slot] is None:
            ivs[slot] = random_below(draw(), 32)

    if spec.fixed_ability is not None:
        ability = spec.fixed_ability
    else:
        ability = random_below(draw(), 3 if spec.hidden_ability else 2)
    nature = spec.nature if spec.nature is not None else random_below(draw(), 25)

    ratio = spec.gender_ratio
    if ratio == 0:
        gender = "M"
    elif ratio == 254:
        gender = "F"
    elif ratio == 255:
        gender = "G"
    else:
        gender = "F" if random_below(draw(), 252) + 1 < ratio else "M"

    return Prediction(ec=ec, pid=pid, shiny=is_shiny(pid, tid, sid),
                      ivs=tuple(ivs), ability_slot=ability, nature=nature,
                      gender=gender, draws=draws)


class _Outputs:
    """MT outputs from a fixed start, generated on demand and kept."""

    def __init__(self, mt: GFMT):
        self._mt = mt.copy()
        self._out = array("I")

    def __getitem__(self, i: int) -> int:
        while len(self._out) <= i:
            self._out.append(self._mt.next())
        return self._out[i]

    def reader(self, start: int) -> Callable[[], int]:
        pos = start

        def rand() -> int:
            nonlocal pos
            pos += 1
            return self[pos - 1]
        return rand


def scan(mt: GFMT, spec: GenSpec, tid: int, sid: int,
         count: int) -> Iterator[tuple[int, Prediction]]:
    """(offset, Prediction) for a generation starting ``offset`` advances
    from ``mt``, for offsets 0..count-1. ``mt`` itself is not advanced."""
    outputs = _Outputs(mt)
    for offset in range(count):
        yield offset, generate(outputs.reader(offset), spec, tid, sid)


def shiny_hits(mt: GFMT, spec: GenSpec, tid: int, sid: int,
               count: int) -> Iterator[tuple[int, Prediction]]:
    """Only the shiny entries of ``scan``, much faster.

    The rerolls stop at the first shiny PID, so a generation is shiny
    exactly when one of its PID draws is; only those offsets need the
    full generation run."""
    outputs = _Outputs(mt)
    rolls = spec.pid_rolls
    for offset in range(count):
        if any(is_shiny(outputs[offset + 1 + r], tid, sid) for r in range(rolls)):
            yield offset, generate(outputs.reader(offset), spec, tid, sid)


def find_shiny(mt: GFMT, spec: GenSpec, tid: int, sid: int,
               max_advances: int) -> Optional[tuple[int, Prediction]]:
    """First offset under ``max_advances`` that would generate a shiny."""
    return next(shiny_hits(mt, spec, tid, sid, max_advances), None)


def read_main_mt(rpc, ptr_addr: int = MAIN_MT_PTR_XY, attempts: int = 20) -> GFMT:
    """The live main MT19937 from a running X/Y.

    The object spans three RPC reads while the game keeps running, so a
    read can straddle an advance. Every advance moves the index and every
    624 rewrite mt[0], so a block whose first 8 bytes still match a
    re-read afterwards is untorn.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    ptr = rpc.read_u32(ptr_addr)
    if ptr % 4 or not _HEAP_LO <= ptr <= _HEAP_HI - _MT_BYTES:
        raise RNGReadError(f"MT pointer {ptr:#010x} is not a heap address; "
                           f"is X/Y running and past the title screen?")
    for _ in range(attempts):
        block = rpc.read(ptr, _MT_BYTES)
        index = int.from_bytes(block[:4], "little")
        if index >= MT_N:
            raise RNGReadError(f"MT index {index} at {ptr:#010x} is out of range; "
                               f"the pointer does not lead to the MT object "
                               f"(a different game revision?)")
        if rpc.read(ptr, 8) == block[:8]:
            return GFMT.from_bytes(block)
    raise RNGReadError(f"MT state at {ptr:#010x} kept changing mid-read "
                       f"({attempts} attempts)")
