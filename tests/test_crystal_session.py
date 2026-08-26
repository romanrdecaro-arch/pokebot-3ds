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
    """A fake address space.

    ``live_at`` marks which base behaves like RUNNING memory: a byte
    there changes on every read, which is how locate_wram tells live
    WRAM apart from a static save buffer.
    """

    def __init__(self, base: int, data: bytes, live_at: int | None = None):
        self.base, self.data = base, data
        self.reads = 0
        self.bytes_read = 0
        self.live_at = base if live_at is None else live_at
        self._tick = 0

    def read(self, addr: int, size: int) -> bytes:
        self.reads += 1
        self.bytes_read += size
        lo = addr - self.base
        if lo < 0 or lo >= len(self.data):
            raise RuntimeError("unmapped")
        chunk = self.data[lo:lo + size]
        if len(chunk) < size:
            raise RuntimeError("short read")
        if addr == self.live_at:
            self._tick += 1
            out = bytearray(chunk)
            for i in range(0, min(64, len(out))):
                out[i] = (out[i] + self._tick) & 0xFF
            return bytes(out)
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


def test_located_base_is_cached_and_reused(monkeypatch) -> None:
    """A cache hit must cost far less than sweeping the heap."""
    # The real neighbourhood is 4 MB, which would swallow this whole
    # synthetic space and make the comparison meaningless.
    monkeypatch.setattr(crystal, "_NEARBY", 0x4000)
    monkeypatch.setattr(crystal, "_NEIGHBOURHOOD", 0x4000)
    far = 0x20000                       # several chunks in
    space = build_space([make_mon(251, 30)], wram_at=far)
    rpc = FakeRPC(BASE, space, live_at=BASE + far)
    assert crystal.locate_wram(rpc, [(BASE, BASE + 0x30000)]) == BASE + far
    scan_reads = rpc.reads
    scan_rpc_bytes = rpc.bytes_read
    assert scan_reads > 2, "scenario too easy to prove anything"

    rpc2 = FakeRPC(BASE, space, live_at=BASE + far)
    assert crystal.locate_wram(rpc2, [(BASE, BASE + 0x30000)]) == BASE + far
    # A cached lookup still probes the cached base's NEIGHBOURHOOD, so
    # it can tell the live region from a save buffer sitting beside it —
    # which an absolute liveness threshold provably cannot. It must
    # still cost far less than sweeping the heap.
    assert rpc2.bytes_read < scan_rpc_bytes, (
        "cached run cost as much as a full sweep")


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


def test_live_base_is_preferred_over_a_save_buffer() -> None:
    """The bug this exists to prevent.

    Several regions hold a valid party block — the live WRAM and the
    game's save/backup buffers. They are identical to the party
    signature, so taking the first match can lock onto a buffer that
    reads a correct party forever while battle state stays permanently
    zero, and no encounter is ever detected.
    """
    mons = [make_mon(251, 30)]
    space = bytearray(0x40000)
    buffer_at, live_at = 0x1000, 0x21000
    for at in (buffer_at, live_at):
        blk = build_space(mons, span=0x40000, wram_at=at)
        space[at:at + 0x30000] = blk[at:at + 0x30000]

    rpc = FakeRPC(BASE, bytes(space), live_at=BASE + live_at)
    assert crystal.locate_wram(rpc, [(BASE, BASE + 0x40000)],
                               use_cache=False) == BASE + live_at


def test_churn_distinguishes_live_from_static() -> None:
    space = build_space([make_mon()], wram_at=0)
    live = FakeRPC(BASE, space, live_at=BASE)
    static = FakeRPC(BASE, space, live_at=BASE + 0x99999)   # never hit
    assert crystal.churn(live, BASE, size=0x400, gap=0.0) > 0
    assert crystal.churn(static, BASE, size=0x400, gap=0.0) == 0
    assert crystal.is_live_base(live, BASE) is True
    assert crystal.is_live_base(static, BASE) is False


def test_stale_cache_pointing_at_a_buffer_is_rejected() -> None:
    """A cached base that parses but does not tick must be re-scanned."""
    space = build_space([make_mon()], wram_at=0)
    crystal._save_cached_base(BASE)
    static = FakeRPC(BASE, space, live_at=BASE + 0x99999)
    assert not crystal.is_live_base(static, BASE)


def test_session_switches_off_a_dead_buffer_to_the_live_base() -> None:
    """A base can keep parsing while being a dead save buffer.

    The session must notice it has stopped ticking and move to the
    real WRAM, or it reads a correct party forever while battle state
    stays zero and no encounter is ever detected.
    """
    mons = [make_mon(251, 30)]
    space = bytearray(0x40000)
    buffer_at, live_at = 0x1000, 0x21000
    for at in (buffer_at, live_at):
        blk = build_space(mons, span=0x40000, wram_at=at)
        space[at:at + 0x30000] = blk[at:at + 0x30000]

    rpc = FakeRPC(BASE, bytes(space), live_at=BASE + live_at)
    s = crystal.CrystalSession(rpc, [(BASE, BASE + 0x40000)],
                               base=BASE + buffer_at)
    s._next_live_check = 0.0          # force the liveness check now
    assert s.ensure_base() == BASE + live_at
    assert s.base == BASE + live_at


def test_liveness_is_not_rechecked_every_call() -> None:
    """The probe costs two reads and a sleep; it must be on a timer."""
    space = build_space([make_mon()], wram_at=0)
    rpc = FakeRPC(BASE, space, live_at=BASE)
    s = crystal.CrystalSession(rpc, [(BASE, BASE + 0x30000)], base=BASE)
    s.ensure_base()
    after_first = rpc.reads
    for _ in range(5):
        s.ensure_base()
    assert rpc.reads - after_first <= 6, "liveness probed on every call"


def test_locate_does_not_sweep_the_whole_heap_for_missing_candidates() -> None:
    """Regression: asking for 8 candidates read the entire range.

    scan_range only stops early once it has `stop_after` hits, so
    requesting 8 when only a few exist meant sweeping every byte of the
    ~900 MB heap on EVERY attempt — roughly 85s of sustained RPC
    traffic, which starved the detection loop and destabilised the
    emulator. It must stop at the first hit and then look only nearby.
    """
    mons = [make_mon(251, 30)]
    span = 0x60000
    live_at = 0x1000
    space = bytearray(span)
    blk = build_space(mons, span=span, wram_at=live_at)
    space[live_at:live_at + 0x30000] = blk[live_at:live_at + 0x30000]

    rpc = FakeRPC(BASE, bytes(space), live_at=BASE + live_at)
    crystal._save_cached_base(0)          # no useful cache
    base = crystal.locate_wram(rpc, [(BASE, BASE + span)], use_cache=False)
    assert base == BASE + live_at

    # The party sits near the start, so stopping at the first hit plus
    # a bounded neighbourhood probe must read far less than the whole
    # range. A sweep would read every byte of it.
    assert rpc.bytes_read < span, (
        f"read {rpc.bytes_read} bytes across a {span}-byte range; the "
        f"scan is not stopping at the first hit")


def test_liveness_only_rejects_a_completely_static_base() -> None:
    """A quiet game must not trigger endless full re-scans.

    Each rejection costs a heap sweep, so the check is deliberately
    generous: only a base that never ticks at all is refused.
    """
    space = build_space([make_mon()], wram_at=0)
    live = FakeRPC(BASE, space, live_at=BASE)
    assert crystal.is_live_base(live, BASE) is True
    static = FakeRPC(BASE, space, live_at=BASE + 0x99999)
    assert crystal.is_live_base(static, BASE) is False
