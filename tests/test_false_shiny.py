"""
The recurring "shiny" with Ice Body.

Seen 13 times in one Noibat run, always identical:

    species 65 · PID 0 · IVs all 0 · moves [0,0,0,0] · no nickname ·
    Hardy (nature 0) · ability 115 (Ice Body) · TSV 8 · PSV 0

That is not a Pokemon. It is a half-built scratch record in the foe
window -- Alakazam cannot even have Ice Body -- whose near-empty body
still sums to a matching checksum. And the shiny formula is
``(PSV ^ TSV) < 16``, so a PID of 0 makes PSV 0 and any record whose
TID ^ SID is under 16 reads as shiny. Junk with PID 0 and blank OT
fields is therefore a false-shiny generator, not a one-off.

The Noibat hunt reset over it, which was harmless. Random encounters
would have run the catch sequence at a Pokemon that does not exist.
So the fix is at the gate every mode shares.

A real Gen 6 Pokemon with PID 0 is a 1-in-4-billion event.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot.modes import observe  # noqa: E402
from pokebot.parser import calc_checksum, parse_pkm  # noqa: E402


def record(*, pid, species=65, ability=115, ability_num=0, nature=0,
           tid=8, sid=0, moves=(0, 0, 0, 0)) -> bytes:
    """A plaintext 232-byte PK6 with a correct checksum."""
    d = bytearray(232)
    struct.pack_into("<I", d, 0x00, 0x1234ABCD)      # encryption key
    struct.pack_into("<H", d, 0x08, species)
    struct.pack_into("<H", d, 0x0C, tid)
    struct.pack_into("<H", d, 0x0E, sid)
    struct.pack_into("<B", d, 0x14, ability)
    struct.pack_into("<B", d, 0x15, ability_num)
    struct.pack_into("<I", d, 0x18, pid)
    struct.pack_into("<B", d, 0x1C, nature)
    for i, mv in enumerate(moves):
        struct.pack_into("<H", d, 0x5A + 2 * i, mv)
    struct.pack_into("<H", d, 0x06, calc_checksum(bytes(d)))
    return bytes(d)


def test_the_junk_record_really_does_read_as_shiny():
    """Pins WHY this happened, so a future change to the shiny formula
    does not quietly make this test meaningless."""
    pkm = parse_pkm(record(pid=0))
    assert pkm.psv == 0
    assert pkm.shiny, "precondition: PID 0 with TID^SID < 16 is 'shiny'"


def test_the_observed_junk_record_is_rejected():
    assert observe._parse_valid(record(pid=0)) is None, (
        "the species-65 / Ice Body / PID-0 record still passes as a "
        "Pokemon, and still reads as shiny")


def test_pid_zero_is_the_only_reason_it_is_rejected():
    """The same bytes with a real PID must still be accepted, or the
    fix is rejecting something broader than it claims to."""
    assert observe._parse_valid(record(pid=0x9D1AE829)) is not None


def test_the_move_bearing_variant_is_rejected_too():
    """A second variant in the log had moves [70, 0, 0, 0]."""
    assert observe._parse_valid(record(pid=0, moves=(70, 0, 0, 0))) is None


def test_the_rapidash_variant_is_rejected_too():
    """And a third: species 78, ability 0, PID 0."""
    assert observe._parse_valid(record(pid=0, species=78, ability=0)) is None


def test_a_real_shiny_is_still_accepted():
    """The point of the hunt. PSV must equal TSV within 16."""
    tid, sid = 0x1D1C, 0x0000                # TSV 0x1D1C
    pid = (0x1D1C << 16) | 0x0001            # PSV 0x1D1D
    pkm = observe._parse_valid(
        record(pid=pid, tid=tid, sid=sid, species=714, ability=119,
               ability_num=1, nature=10, moves=(48, 0, 0, 0)))
    assert pkm is not None
    assert pkm.shiny


def test_the_foe_window_scan_skips_it(monkeypatch):
    """End to end through the scanner every encounter mode uses."""
    junk = record(pid=0)
    real = record(pid=0x9D1AE829, species=714, ability=119,
                  ability_num=1, moves=(48, 0, 0, 0))
    buf = bytearray(0x1000)
    buf[0x100:0x100 + 232] = junk
    real_b = bytearray(real)
    struct.pack_into("<I", real_b, 0x00, 0x55667788)   # distinct key
    struct.pack_into("<H", real_b, 0x06, calc_checksum(bytes(real_b)))
    buf[0x800:0x800 + 232] = real_b

    found = observe._all_valid(bytes(buf), 0x08800000)
    species = [p.species for _, p in found]
    assert 65 not in species, "the scanner still yields the junk record"
    assert 714 in species, "the scanner lost the real one"
