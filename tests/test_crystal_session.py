"""
Tests for the live Crystal session layer.

The WRAM base is discovered, not known, so the risky behaviours are
around caching it: a stale base must be detected and re-scanned rather
than read as garbage, and a validated one must be reused.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from pokebot import crystal, gen2  # noqa: E402
from test_gen2 import make_mon      # noqa: E402


class FakeRPC:
    def __init__(self, base: int, data: bytes):
        self.base, self.data = base, data
        self.reads = 0

    def read(self, addr: int, size: int) -> bytes:
        self.reads += 1
        lo = addr - self.base
        if lo < 0 or lo >= len(self.data):
            raise RuntimeError("unmapped")
        chunk = self.data[lo:lo + size]
        if len(chunk) < size:
            raise RuntimeError("short read")
        return chunk


def build_space(mons, span=0x30000, wram_at=0, battle_mode=0):
    """An address space with WRAM planted at ``wram_at``.

    ``wram_at`` matters for the caching tests: with WRAM at offset 0
    the very first chunk read already contains the party, so a scan and
    a cache hit both cost one read and the comparison proves nothing.
    """
    space = bytearray(span)
    party_at = wram_at + gen2.PARTY_COUNT_FROM_WRAM
    space[party_at] = len(mons)
    for i in range(6):
        space[party_at + 1 + i] = (mons[i][gen2.OFF_SPECIES]
                                   if i < len(mons) else 0xFF)
    space[party_at + 7] = 0xFF
    for i, rec in enumerate(mons):
        s = party_at + 8 + i * gen2.PARTY_STRUCT_SIZE
        space[s:s + len(rec)] = rec
    space[wram_at + crystal.GB_BATTLE_MODE - gen2.GB_WRAM_LO] = battle_mode
    return bytes(space)


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(crystal, "CACHE_PATH", tmp_path / "wram.json")


BASE = 0x30000000


def test_locate_finds_the_wram_base() -> None:
    rpc = FakeRPC(BASE, build_space([make_mon(251, 30)]))
    assert crystal.locate_wram(rpc, [(BASE, BASE + 0x30000)]) == BASE


def test_located_base_is_cached_and_reused() -> None:
    """A cache hit must verify in one read, not re-walk the heap."""
    far = 0x20000                       # several chunks in
    space = build_space([make_mon(251, 30)], wram_at=far)
    rpc = FakeRPC(BASE, space)
    assert crystal.locate_wram(rpc, [(BASE, BASE + 0x30000)]) == BASE + far
    scan_reads = rpc.reads
    assert scan_reads > 2, "scenario too easy to prove anything"

    rpc2 = FakeRPC(BASE, space)
    assert crystal.locate_wram(rpc2, [(BASE, BASE + 0x30000)]) == BASE + far
    assert rpc2.reads < scan_reads, "cached run re-scanned the whole heap"
    assert rpc2.reads <= 2, "a cached base should verify in one read"


def test_stale_cached_base_is_rescanned_not_trusted() -> None:
    """The base can move between launches; a wrong one must not stick."""
    crystal._save_cached_base(0x0BADBEEF)
    rpc = FakeRPC(BASE, build_space([make_mon(251, 30)]))
    assert crystal.locate_wram(rpc, [(BASE, BASE + 0x30000)]) == BASE


def test_verify_base_rejects_a_wrong_address() -> None:
    rpc = FakeRPC(BASE, build_space([make_mon(251, 30)]))
    assert crystal.verify_base(rpc, BASE)
    assert not crystal.verify_base(rpc, BASE + 0x1000)


def test_session_reads_the_party() -> None:
    rpc = FakeRPC(BASE, build_space([make_mon(251, 30, dv_word=0xAAAA),
                                     make_mon(155, 12)]))
    s = crystal.CrystalSession(rpc, [(BASE, BASE + 0x30000)], base=BASE)
    party = s.party()
    assert [p.species for p in party] == [251, 155]
    assert party[0].shiny is True
    assert party[1].shiny is False


def test_session_relocates_when_the_base_goes_stale() -> None:
    rpc = FakeRPC(BASE, build_space([make_mon(251, 30)]))
    s = crystal.CrystalSession(rpc, [(BASE, BASE + 0x30000)],
                               base=0x0BADBEEF)
    assert [p.species for p in s.party()] == [251]
    assert s.base == BASE


def test_battle_mode_is_read() -> None:
    for mode in (crystal.BATTLE_NONE, crystal.BATTLE_WILD,
                 crystal.BATTLE_TRAINER):
        rpc = FakeRPC(BASE, build_space([make_mon()], battle_mode=mode))
        s = crystal.CrystalSession(rpc, [(BASE, BASE + 0x30000)], base=BASE)
        assert s.battle_mode() == mode


def test_no_enemy_reading_outside_a_battle() -> None:
    rpc = FakeRPC(BASE, build_space([make_mon()], battle_mode=0))
    s = crystal.CrystalSession(rpc, [(BASE, BASE + 0x30000)], base=BASE)
    assert s.enemy() is None


def test_enemy_reading_is_marked_unconfirmed() -> None:
    """The in-battle struct's DV offsets are not verified, so nothing
    downstream should be able to mistake this for solid data."""
    rpc = FakeRPC(BASE, build_space([make_mon()], battle_mode=1))
    s = crystal.CrystalSession(rpc, [(BASE, BASE + 0x30000)], base=BASE)
    enemy = s.enemy()
    assert enemy is not None
    assert enemy.confirmed is False


def test_unreadable_memory_yields_an_empty_party_not_a_crash() -> None:
    class DeadRPC:
        def read(self, addr, size):
            raise RuntimeError("emulator closed")

    s = crystal.CrystalSession(DeadRPC(), [(BASE, BASE + 0x1000)], base=BASE)
    assert s.party() == []


def test_watcher_formats_a_shiny_clearly() -> None:
    import crystal_watch
    shiny = gen2.parse_pokemon(bytes(make_mon(251, 30, dv_word=0xAAAA)))
    assert "SHINY" in crystal_watch.describe(shiny)
    plain = gen2.parse_pokemon(bytes(make_mon(251, 30, dv_word=0x1234)))
    assert "SHINY" not in crystal_watch.describe(plain)


def test_watcher_reports_shiny_presence() -> None:
    import crystal_watch
    shiny = gen2.parse_pokemon(bytes(make_mon(251, 30, dv_word=0xAAAA)))
    plain = gen2.parse_pokemon(bytes(make_mon(155, 12, dv_word=0x1234)))
    assert crystal_watch.report([shiny], None) is True
    assert crystal_watch.report([plain], None) is False
    assert crystal_watch.report([], None) is False


def test_scan_prefilter_still_finds_blocks_it_must() -> None:
    """The 0xFF-terminator prefilter is an optimisation, not a filter.

    Scanning every byte position in Python could not finish over a
    ~900 MB heap, so the scan jumps between 0xFF bytes instead. That
    must not change WHICH blocks are found.
    """
    far = 0x21000
    space = build_space([make_mon(251, 30, dv_word=0xAAAA)], wram_at=far)
    rpc = FakeRPC(BASE, space)
    hits = crystal.scan_range(rpc, BASE, BASE + 0x30000)
    assert [h["wram_base"] for h in hits] == [BASE + far]
    assert hits[0]["party"][0].shiny


def test_scan_prefilter_handles_a_party_count_of_six() -> None:
    """Six slots means no 0xFF inside the species list itself."""
    mons = [make_mon(150 + i, 20 + i) for i in range(6)]
    space = build_space(mons, wram_at=0x1000)
    rpc = FakeRPC(BASE, space)
    hits = crystal.scan_range(rpc, BASE, BASE + 0x30000)
    assert len(hits) == 1
    assert [p.species for p in hits[0]["party"]] == [150 + i for i in range(6)]
