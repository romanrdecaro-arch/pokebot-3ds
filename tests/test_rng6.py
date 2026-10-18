"""
Gen 6 (X/Y) RNG prediction.

X/Y build every Pokemon from the game's main MT19937: pml's Random32 and
RandomBelow are hooked by the game to it (code.bin 0x0010D528 /
0x0010D4EC). The generator (FillRandomSpec, 0x0014F324) then draws EC,
PID (rerolled while not shiny), guaranteed-31 IV slots, IVs, ability,
nature and gender, in that order.

Two pieces of real data anchor this. A wild Graveler met in X on
2026-10-18 whose EC turned up as MT output #69 after a live snapshot,
with every later field where the generator puts it. And a pair of live
MT objects read 238 advances apart (tests/data/xy_mt_snapshots.bin),
which pins the object layout and what the index means.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot import rng6  # noqa: E402

SNAPSHOTS = REPO / "tests" / "data" / "xy_mt_snapshots.bin"
MT_BYTES = 4 + 4 * rng6.MT_N
PTR = 0x08C52848

# The live capture: MT outputs from the Graveler's EC onward.
GRAVELER_STREAM = [
    0x86877C8A,                          # EC
    0xA1A062F6, 0x0C8DF413, 0xD9F6B136,  # PID rolls 1-3 (Shiny Charm)
    0xA4269588, 0x2134BB5D, 0xC6AE2E60,  # IVs HP Atk Def
    0x4DDB36E7, 0x3BF7B88D, 0xE252579C,  # IVs SpA SpD Spe
    0x5B0E315E,                          # ability slot
    0x94BD326B,                          # nature
    0x38072FBA,                          # gender
    0x0214FA23, 0x7F633BD5,              # (unused tail)
]
X_TID, X_SID = 31182, 25810


def feed(values):
    it = iter(values)
    return lambda: next(it)


def r_for(choice: int, n: int) -> int:
    """A u32 that RandomBelow(n) maps to ``choice``."""
    r = (choice * 2**32 + n - 1) // n
    assert (n * r) >> 32 == choice
    return r


def block_of(mt) -> bytes:
    return struct.pack(f"<{rng6.MT_N + 1}I", mt.index, *mt.words)


def advanced(mt, n):
    s = mt.copy()
    for _ in range(n):
        s.next()
    return s


# --------------------------------------------------------------------------
# The MT itself
# --------------------------------------------------------------------------
def test_mt_matches_reference_mt19937():
    mt = rng6.GFMT.from_seed(5489)
    assert [mt.next() for _ in range(3)] == [3499211612, 581869302, 3890346734]


def test_mt_ten_thousandth_output_is_the_standard_value():
    # C++ [rand.predef]: the 10000th output of default-seeded mt19937.
    # Crosses 16 passes of the in-place regeneration.
    mt = advanced(rng6.GFMT.from_seed(5489), 9999)
    assert mt.next() == 4123659995


def test_mt_rebuilt_at_index_623_continues_the_sequence():
    ref = advanced(rng6.GFMT.from_seed(5489), 623)
    assert ref.index == 623
    rebuilt = rng6.GFMT.from_bytes(block_of(ref))
    assert [rebuilt.next() for _ in range(1300)] == [ref.next() for _ in range(1300)]


def test_mt_copy_is_independent():
    a = rng6.GFMT.from_seed(1)
    b = a.copy()
    a.next()
    assert b.index == 0
    assert b.next() == rng6.GFMT.from_seed(1).next()


def test_mt_equality_compares_index_and_words():
    a, b = rng6.GFMT.from_seed(7), rng6.GFMT.from_seed(7)
    assert a == b
    a.next()
    assert a != b
    b.next()
    assert a == b


@pytest.mark.parametrize("index", [-1, 624, 10**6])
def test_mt_rejects_bad_index(index):
    with pytest.raises(ValueError):
        rng6.GFMT(index, [0] * 624)


def test_mt_rejects_wrong_state_length():
    with pytest.raises(ValueError):
        rng6.GFMT(0, [0] * 623)
    with pytest.raises(ValueError):
        rng6.GFMT.from_bytes(b"\0" * (MT_BYTES - 4))


def test_advances_between():
    a = rng6.GFMT.from_seed(99)
    b = advanced(a, 1234)
    assert rng6.advances_between(a, b, limit=5000) == 1234
    assert rng6.advances_between(b, a, limit=5000) is None


def test_real_live_snapshots_are_linked_by_stepping():
    raw = SNAPSHOTS.read_bytes()
    n, = struct.unpack_from("<I", raw)
    a = rng6.GFMT.from_bytes(raw[4:4 + MT_BYTES])
    b = rng6.GFMT.from_bytes(raw[4 + MT_BYTES:])
    assert n > 0 and a != b
    assert advanced(a, n) == b
    assert rng6.advances_between(a, b, limit=10_000) == n


# --------------------------------------------------------------------------
# One generation
# --------------------------------------------------------------------------
def test_generate_reproduces_the_live_graveler():
    p = rng6.generate(feed(GRAVELER_STREAM),
                      rng6.GenSpec(pid_rolls=3, gender_ratio=127), X_TID, X_SID)
    assert p.ec == 0x86877C8A
    assert p.pid == 0xD9F6B136
    assert not p.shiny
    assert p.ivs == (20, 4, 24, 9, 7, 28)     # HP Atk Def SpA SpD Spe
    assert p.ability_slot == 0
    assert p.nature == 14                     # Naive
    assert p.gender == "F"
    assert p.draws == 13


def test_ivs_dict_matches_the_graveler_record_as_parsed():
    # pokebot.parser reported this dict for the live Graveler's PK6
    parsed = {"HP": 20, "Atk": 4, "Def": 24, "Spe": 28, "SpA": 9, "SpD": 7}
    p = rng6.generate(feed(GRAVELER_STREAM), rng6.GenSpec(pid_rolls=3), X_TID, X_SID)
    assert p.ivs_dict() == parsed


def test_reroll_stops_at_the_first_shiny_pid():
    shiny_pid = 0x12341234                    # PSV 0 for TID/SID 0
    stream = [0xAAAA0000, 0x0F0F5555, shiny_pid] + [0] * 6 + [0, 0, 0]
    p = rng6.generate(feed(stream), rng6.GenSpec(pid_rolls=3, gender_ratio=255), 0, 0)
    assert p.pid == shiny_pid and p.shiny
    assert p.draws == 1 + 2 + 6 + 1 + 1       # no third roll, no gender draw


def test_shiny_first_roll_skips_the_rest():
    stream = [0, 0x12341234] + [r_for(5, 32)] * 6 + [0, 0, 0]
    p = rng6.generate(feed(stream), rng6.GenSpec(pid_rolls=3), 0, 0)
    assert p.shiny and p.ivs == (5,) * 6
    assert p.draws == 2 + 6 + 3


def test_single_roll_keeps_the_first_pid():
    stream = [1, 0xDEADBEEF] + [0] * 9
    p = rng6.generate(feed(stream), rng6.GenSpec(pid_rolls=1), 0, 0)
    assert p.pid == 0xDEADBEEF


def test_guaranteed_perfect_ivs_redraw_taken_slots():
    slots = [r_for(0, 6), r_for(0, 6), r_for(5, 6), r_for(2, 6)]
    ivs = [r_for(v, 32) for v in (10, 11, 12)]      # slots 1, 3, 4
    stream = [0, 1] + slots + ivs + [0, 0, 0]
    p = rng6.generate(feed(stream), rng6.GenSpec(perfect_ivs=3), 0, 0)
    assert p.ivs == (31, 10, 31, 11, 12, 31)
    assert p.draws == 2 + 4 + 3 + 1 + 1 + 1


def test_six_perfect_ivs_draw_no_iv_values():
    slots = [r_for(s, 6) for s in (3, 3, 0, 1, 2, 4, 5)]
    stream = [0, 1] + slots + [0, 0, 0]
    p = rng6.generate(feed(stream), rng6.GenSpec(perfect_ivs=6), 0, 0)
    assert p.ivs == (31,) * 6
    assert p.draws == 2 + 7 + 3


def test_fixed_nature_and_ability_draw_nothing():
    stream = [0, 1] + [0] * 6 + [r_for(3, 252)]
    p = rng6.generate(feed(stream),
                      rng6.GenSpec(nature=7, fixed_ability=2, gender_ratio=127), 0, 0)
    assert (p.nature, p.ability_slot, p.gender) == (7, 2, "F")
    assert p.draws == 2 + 6 + 1


def test_hidden_ability_rolls_three_ways():
    stream = [0, 1] + [0] * 6 + [r_for(2, 3), 0]
    p = rng6.generate(feed(stream),
                      rng6.GenSpec(hidden_ability=True, gender_ratio=255), 0, 0)
    assert p.ability_slot == 2


@pytest.mark.parametrize("ratio,gender", [(0, "M"), (254, "F"), (255, "G")])
def test_fixed_gender_ratios_draw_nothing(ratio, gender):
    stream = [0, 1] + [0] * 6 + [0, 0]
    p = rng6.generate(feed(stream), rng6.GenSpec(gender_ratio=ratio), 0, 0)
    assert p.gender == gender
    assert p.draws == 10


def test_gender_threshold():
    # female when RandomBelow(252) + 1 < ratio
    base = [0, 1] + [0] * 6 + [0, 0]
    male = rng6.generate(feed(base + [r_for(126, 252)]),
                         rng6.GenSpec(gender_ratio=127), 0, 0)
    female = rng6.generate(feed(base + [r_for(125, 252)]),
                           rng6.GenSpec(gender_ratio=127), 0, 0)
    assert (male.gender, female.gender) == ("M", "F")


def test_random_below_scales_small_bounds_and_divides_large_ones():
    assert rng6.random_below(0xFFFFFFFF, 25) == 24
    assert rng6.random_below(0x80000000, 2) == 1
    assert rng6.random_below(100_005, 100_000) == 5
    with pytest.raises(ValueError):
        rng6.random_below(1, 0)


def test_gen6_shiny_threshold_is_sixteen():
    assert rng6.is_shiny(0x0000000F, 0, 0)
    assert not rng6.is_shiny(0x00000010, 0, 0)


def test_shiny_uses_trainer_id_and_secret_id():
    assert rng6.is_shiny((X_TID ^ X_SID) << 16, X_TID, X_SID)
    assert not rng6.is_shiny(0xD9F6B136, X_TID, X_SID)


@pytest.mark.parametrize("kwargs", [
    {"pid_rolls": 0}, {"pid_rolls": rng6.MAX_PID_ROLLS + 1},
    {"perfect_ivs": 7}, {"perfect_ivs": -1},
    {"gender_ratio": 256}, {"nature": 25}, {"fixed_ability": 3},
    {"fixed_ability": 0, "hidden_ability": True},
])
def test_genspec_rejects_out_of_range(kwargs):
    with pytest.raises(ValueError):
        rng6.GenSpec(**kwargs)


# --------------------------------------------------------------------------
# Looking ahead
# --------------------------------------------------------------------------
def test_scan_offset_n_means_generation_starts_n_advances_from_now():
    mt = rng6.GFMT.from_seed(0xC0FFEE)
    spec = rng6.GenSpec(pid_rolls=3)
    results = list(rng6.scan(mt, spec, X_TID, X_SID, 50))
    assert [a for a, _ in results] == list(range(50))
    for offset in (0, 1, 17, 49):
        ref = advanced(mt, offset)
        assert results[offset][1] == rng6.generate(ref.next, spec, X_TID, X_SID)


def test_scan_does_not_move_the_callers_state():
    mt = rng6.GFMT.from_seed(3)
    before = mt.copy()
    list(rng6.scan(mt, rng6.GenSpec(), 0, 0, 100))
    list(rng6.shiny_hits(mt, rng6.GenSpec(), 0, 0, 100))
    assert mt == before


@pytest.mark.parametrize("rolls", [1, 3])
def test_shiny_hits_is_exactly_the_shiny_part_of_scan(rolls):
    mt = rng6.GFMT.from_seed(0xBEEF)
    spec = rng6.GenSpec(pid_rolls=rolls, perfect_ivs=1)
    full = [(o, p) for o, p in rng6.scan(mt, spec, X_TID, X_SID, 30000) if p.shiny]
    assert full
    assert list(rng6.shiny_hits(mt, spec, X_TID, X_SID, 30000)) == full


def test_find_shiny_returns_the_first_shiny_offset():
    mt = rng6.GFMT.from_seed(0xBEEF)
    spec = rng6.GenSpec(pid_rolls=3)
    hit = rng6.find_shiny(mt, spec, X_TID, X_SID, 20000)
    assert hit is not None
    offset, pred = hit
    assert pred.shiny
    assert not any(p.shiny for _, p in rng6.scan(mt, spec, X_TID, X_SID, offset))


def test_find_shiny_gives_up_at_the_limit():
    mt = rng6.GFMT.from_seed(0xBEEF)
    first, _ = rng6.find_shiny(mt, rng6.GenSpec(), X_TID, X_SID, 100000)
    assert rng6.find_shiny(mt, rng6.GenSpec(), X_TID, X_SID, first) is None


# --------------------------------------------------------------------------
# Reading the live state
# --------------------------------------------------------------------------
class FakeRPC:
    """Serves MT objects at PTR: one block per full read (the last one
    repeats) and, for the 8-byte re-read that follows, either the given
    bytes or the start of the block just served."""

    def __init__(self, blocks, ptr=PTR, rereads=()):
        self.ptr = ptr
        self.blocks = list(blocks)
        self.rereads = list(rereads)
        self.full_reads = 0
        self._last = None

    def read_u32(self, addr):
        if addr == rng6.MAIN_MT_PTR_XY:
            return self.ptr
        raise AssertionError(f"unexpected read_u32 {addr:#x}")

    def read(self, addr, size):
        assert addr == self.ptr
        if size == MT_BYTES:
            self.full_reads += 1
            self._last = self.blocks.pop(0) if len(self.blocks) > 1 else self.blocks[0]
            return self._last
        assert size == 8
        return self.rereads.pop(0) if self.rereads else self._last[:8]


def test_read_returns_the_object_behind_the_pointer():
    mt = advanced(rng6.GFMT.from_seed(42), 1)
    assert rng6.read_main_mt(FakeRPC([block_of(mt)])) == mt


def test_read_parses_a_real_live_object():
    raw = SNAPSHOTS.read_bytes()
    block = raw[4:4 + MT_BYTES]
    assert rng6.read_main_mt(FakeRPC([block])) == rng6.GFMT.from_bytes(block)


def test_read_retries_a_torn_block():
    s = rng6.GFMT.from_seed(42)
    later = advanced(s, 5)
    torn = block_of(s)[:1024] + block_of(later)[1024:]
    rpc = FakeRPC([torn, block_of(later)], rereads=[block_of(later)[:8]])
    assert rng6.read_main_mt(rpc) == later
    assert rpc.full_reads == 2


def test_read_catches_a_full_cycle_during_the_read():
    s = advanced(rng6.GFMT.from_seed(42), 10)
    cycled = advanced(s, rng6.MT_N)
    assert cycled.index == s.index and cycled.words[0] != s.words[0]
    rpc = FakeRPC([block_of(s), block_of(cycled)], rereads=[block_of(cycled)[:8]])
    assert rng6.read_main_mt(rpc) == cycled


def test_read_gives_up_when_every_block_is_torn():
    s = rng6.GFMT.from_seed(1)
    moving = block_of(advanced(s, 1))[:8]
    rpc = FakeRPC([block_of(s)], rereads=[moving] * 10)
    with pytest.raises(rng6.RNGReadError, match="kept changing"):
        rng6.read_main_mt(rpc, attempts=5)


def test_read_out_of_range_index_means_the_wrong_object():
    bad = struct.pack("<I", 624) + block_of(rng6.GFMT.from_seed(1))[4:]
    rpc = FakeRPC([bad])
    with pytest.raises(rng6.RNGReadError, match="out of range"):
        rng6.read_main_mt(rpc)
    assert rpc.full_reads == 1


@pytest.mark.parametrize("ptr", [0, 0x10000000, PTR + 1, 0x07FFFFFC])
def test_read_rejects_a_pointer_outside_the_heap(ptr):
    with pytest.raises(rng6.RNGReadError, match="not a heap address"):
        rng6.read_main_mt(FakeRPC([block_of(rng6.GFMT.from_seed(1))], ptr=ptr))


def test_read_needs_at_least_one_attempt():
    with pytest.raises(ValueError):
        rng6.read_main_mt(FakeRPC([block_of(rng6.GFMT.from_seed(1))]), attempts=0)
