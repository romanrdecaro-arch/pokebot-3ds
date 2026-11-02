"""
Gen 7 egg prediction (Sun / Moon / Ultra Sun / Ultra Moon).

Ported from 3DSRNGTool (wwwwwwzx/3DSRNGTool @ae5d176): RNG/TinyMT.cs,
Core/EggRNG.cs and Gen7/Egg7.cs.

The Nursery keeps a 128-bit TinyMT state, the egg seed, and the next egg
is drawn from it when the Nursery lady hands one over. Declining the egg
moves the state exactly one step; accepting it moves the state past
every number the egg drew, plus two. With the Masuda method and / or the
Shiny Charm the PID is drawn from this state too (6 / 2 / 8 tries, kept
at the first shiny one), so whether the egg on offer is shiny is known
before she is answered. Without either, the PID comes from the main RNG
at the moment the egg is accepted, and the seed cannot tell.
"""
from __future__ import annotations

from dataclasses import dataclass

_M32 = 0xFFFFFFFF
_MAT1, _MAT2, _TMAT = 0x8F7011EE, 0xFC78FF1F, 0x3793FDFF

DITTO = 132
#: 3DSRNGTool's parent item index: 1 Everstone, 2 Destiny Knot, 3-8 the
#: Power item for HP, Atk, Def, SpA, SpD, Spe (Weight, Bracer, Belt,
#: Lens, Band, Anklet); anything else is 0.
_ITEM_INDEX = {229: 1, 280: 2, 294: 3, 289: 4, 290: 5, 291: 6, 292: 7, 293: 8}
#: Parents whose egg always has one gender (or none), so no number is
#: drawn for it: breedable species whose line starts at a male-only,
#: female-only or genderless Pokemon (PokeAPI pokemon_species.csv
#: gender_rate -1 / 0 / 8; the Undiscovered egg group left out). Gallade,
#: Vespiquen, Froslass, Salazzle, Mothim and Wormadam are not here: their
#: eggs are Ralts, Combee, Snorunt, Salandit and Burmy. The Nidoran and
#: Volbeat / Illumise lines draw for theirs too.
FIXED_GENDER = frozenset({
    81, 82, 100, 101, 106, 107, 113, 115, 120, 121, 124, 128, 137, 233,
    237, 241, 242, 337, 338, 343, 344, 374, 375, 376, 436, 437, 462, 474,
    479, 489, 490, 538, 539, 548, 549, 599, 600, 601, 615, 622, 623, 627,
    628, 629, 630, 669, 670, 671, 703, 761, 762, 763, 774, 781,
})


def next_state(s: tuple) -> tuple:
    """One TinyMT32 step of the state (s0, s1, s2, s3)."""
    x = (s[0] & 0x7FFFFFFF) ^ s[1] ^ s[2]
    x = (x ^ (x << 1)) & _M32
    y = s[3] ^ (s[3] >> 1) ^ x
    s2 = (x ^ (y << 10)) & _M32
    if y & 1:
        return (s[1], s[2] ^ _MAT1, s2 ^ _MAT2, y)
    return (s[1], s[2], s2, y)


def temper(s: tuple) -> int:
    t1 = (s[0] + (s[2] >> 8)) & _M32
    t0 = s[3] ^ t1
    return t0 ^ _TMAT if t1 & 1 else t0


def advance(s: tuple, n: int) -> tuple:
    for _ in range(n):
        s = next_state(s)
    return s


class Stream:
    """The numbers TinyMT gives after ``state``, kept as they are made:
    [n] is the first one drawn by the egg n declines from now."""

    def __init__(self, state):
        self.state, self.out = tuple(state), []

    def __getitem__(self, i: int) -> int:
        while len(self.out) <= i:
            self.state = next_state(self.state)
            self.out.append(temper(self.state))
        return self.out[i]


@dataclass(frozen=True)
class Parents:
    """What the egg draw depends on. ``male`` / ``female`` are 3DSRNGTool's
    roles: a Ditto takes the one its partner does not."""
    male_item: int
    female_item: int
    random_gender: bool
    same_species: bool      # the ball then comes from either parent: a draw
    masuda: bool
    shiny_charm: bool

    @property
    def pid_rolls(self) -> int:
        return 2 * self.shiny_charm + 6 * self.masuda

    @classmethod
    def from_nursery(cls, a, b, shiny_charm: bool) -> "Parents":
        """From the two Nursery records (ParsedPokemon)."""
        if a.species == DITTO:
            a, b = b, a                     # a is never the Ditto now
        if b.species == DITTO:
            mother = a
            male, female = (b, a) if a.gender == "F" else (a, b)
        else:
            male, female = (a, b) if b.gender == "F" else (b, a)
            mother = female
        return cls(male_item=_ITEM_INDEX.get(male.held_item, 0),
                   female_item=_ITEM_INDEX.get(female.held_item, 0),
                   random_gender=mother.species not in FIXED_GENDER,
                   same_species=a.species == b.species,
                   masuda=a.ot_language != b.ot_language,
                   shiny_charm=shiny_charm)


@dataclass(frozen=True)
class Egg:
    nature: int
    ec: int
    pid: int | None         # None without Masuda / Shiny Charm
    shiny: bool
    used: int               # steps accepting it moves the seed


def generate(rand: Stream, start: int, p: Parents, tsv: int) -> Egg:
    """The egg that ``start`` declines from now would bring (Egg7.Generate,
    in the same draw order). ``tsv`` is (TID ^ SID) >> 4."""
    i = start

    def r() -> int:
        nonlocal i
        i += 1
        return rand[i - 1]

    if p.random_gender:
        r()
    nature = r() % 25
    if p.male_item == p.female_item == 1:       # whose Everstone
        r()
    r()                                         # ability
    mp, fp = p.male_item - 3, p.female_item - 3  # >= 0: a Power item's stat
    fixed: set = set()
    if mp >= 0 and fp >= 0:
        fixed.add(mp if r() & 1 == 0 else fp)
    elif max(mp, fp) >= 0:
        fixed.add(max(mp, fp))
    while len(fixed) < (5 if 2 in (p.male_item, p.female_item) else 3):
        stat = r() % 6
        while stat in fixed:
            stat = r() % 6
        fixed.add(stat)
        r()                                     # from which parent
    for _ in range(6):                          # IVs
        r()
    ec = r()
    pid, shiny = None, False
    for _ in range(p.pid_rolls):
        pid = r()
        if ((pid >> 16) ^ (pid & 0xFFFF)) >> 4 == tsv:
            shiny = True
            break
    if p.same_species:                          # whose ball
        r()
    return Egg(nature, ec, pid, shiny, i - start + 2)   # + adopt, clear


def eggs_until_shiny(state, p: Parents, tsv: int,
                     limit: int = 20000) -> int | None:
    """Eggs to decline before the first shiny one (0: the egg on offer
    now), or None if there is none within ``limit``."""
    rand = Stream(state)
    for n in range(limit):
        if generate(rand, n, p, tsv).shiny:
            return n
    return None
