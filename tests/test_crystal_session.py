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


# ---------------------------------------------------------------------
# Regressions from the Celebi session of 2026-08-26.
#
# Two separate failures, both proven from timestamps rather than
# guessed at:
#
#   * The base was picked as 0x08a3bf1a at 15:26:39. A wild Celebi
#     battle then ran for ~15 seconds completely unseen, because a
#     stale copy's battle byte reads 0 forever. The 30-second liveness
#     re-compare switched the base to 0x08a2ffac at 15:27:11.255 and
#     the encounter was logged at 15:27:11.588 — 0.33s later.
#   * A sweep of the 896 MB catch-all range issued 177,164 RPC reads in
#     21 seconds, every one of them answered "no target process
#     selected", and Azahar wrote 100 MB of log and died.
# ---------------------------------------------------------------------

class TwoCopyRPC:
    """Two party-block copies; only ``battling_at`` is in a battle.

    Deliberately does NOT make either copy churn, so churn cannot break
    the tie. That is the real situation: the copies are equally
    plausible and the initial pick is close to a coin flip.
    """

    def __init__(self, base, data, battling_at):
        self.base, self.data = base, data
        self.battling_at = battling_at

    def read(self, addr, size):
        lo = addr - self.base
        if lo < 0 or lo + size > len(self.data):
            raise RuntimeError("unmapped")
        return bytes(self.data[lo:lo + size])


def _two_copies(mode_a=0, mode_b=crystal.BATTLE_WILD,
                species=251, level=30, gap=0x8000):
    """Two WRAM copies in one space; the second is mid-battle."""
    span = gap * 2 + 0x8000
    space = bytearray(span)
    for at, mode in ((0, mode_a), (gap, mode_b)):
        one = bytearray(build_space([make_mon(157, 100)], span=0x8000,
                                    wram_at=0, battle_mode=mode))
        one[crystal.GB_ENEMY_SPECIES - gen2.GB_WRAM_LO] = species
        one[crystal.GB_ENEMY_LEVEL - gen2.GB_WRAM_LO] = level
        space[at:at + len(one)] = one
    return bytes(space), gap


def test_battle_on_a_sibling_copy_is_seen_at_once() -> None:
    """The 15-second Celebi blind spot must not be possible again.

    With the base on the wrong copy, battle_mode() has to notice that a
    sibling is mid-battle and switch immediately — not wait out the
    30-second liveness timer.
    """
    space, gap = _two_copies()
    rpc = TwoCopyRPC(BASE, space, BASE + gap)
    session = crystal.CrystalSession(rpc, [], base=BASE)
    session._candidates = [BASE, BASE + gap]

    assert session.battle_mode() == crystal.BATTLE_WILD
    assert session.base == BASE + gap, "did not move to the battling copy"

    enemy = session.enemy()
    assert enemy is not None and enemy.species == 251 and enemy.level == 30


def test_a_lone_battle_byte_is_not_enough_to_switch() -> None:
    """A stale copy holds whatever was in WRAM when it was written.

    1 and 2 are common byte values, so the battle byte alone must not
    move the base — the opponent record has to corroborate it.
    """
    space, gap = _two_copies(species=0, level=200)   # implausible foe
    rpc = TwoCopyRPC(BASE, space, BASE + gap)
    session = crystal.CrystalSession(rpc, [], base=BASE)
    session._candidates = [BASE, BASE + gap]

    assert session.battle_mode() == crystal.BATTLE_NONE
    assert session.base == BASE, "switched on an uncorroborated byte"


def test_our_own_battle_needs_no_sibling_lookup() -> None:
    """When our base IS battling, the rivals are never consulted."""
    space, gap = _two_copies(mode_a=crystal.BATTLE_WILD, mode_b=0)
    rpc = TwoCopyRPC(BASE, space, BASE)
    session = crystal.CrystalSession(rpc, [], base=BASE)
    session._candidates = [BASE, BASE + gap]
    calls = []
    session._really_battling = lambda b: calls.append(b) or False

    assert session.battle_mode() == crystal.BATTLE_WILD
    assert calls == [], "asked the siblings despite seeing its own battle"


class BlankRPC:
    """An emulator with no target process: every read answers zeros."""

    def __init__(self):
        self.bytes_read = 0

    def read(self, addr, size):
        self.bytes_read += size
        return bytes(size)


def test_a_blank_range_is_abandoned_not_ground_through() -> None:
    """Detached emulator: 4 MB of proof, not 896 MB of it."""
    rpc = BlankRPC()
    hits = crystal.scan_range(rpc, 0x08000000, 0x40000000,
                              progress_every=0, max_req_per_s=0)
    assert hits == []
    assert rpc.bytes_read <= crystal._DEAD_BAIL_BYTES + crystal.CHUNK, (
        f"read {rpc.bytes_read / 1048576:.0f} MB of blank memory before "
        f"giving up")


def test_a_blank_stretch_mid_range_does_not_abort_the_scan() -> None:
    """Blank pages inside a real heap are ordinary and must be skipped.

    The bail-out exists for a range that is blank from its first byte;
    arming it anywhere else would make the scanner miss a party block
    that sits past a large empty region.
    """
    lead = 0x1000                                  # real data up front
    blank = crystal._DEAD_BAIL_BYTES + 0x10000     # then a big gap
    party = build_space([make_mon(251, 30)], span=0x8000)
    space = bytearray(lead + blank + len(party))
    space[:lead] = b"\xa5" * lead
    space[lead + blank:] = party
    rpc = FakeRPC(BASE, bytes(space))

    hits = crystal.scan_range(rpc, BASE, BASE + len(space),
                              progress_every=0, max_req_per_s=0)
    assert [h["wram_base"] for h in hits] == [BASE + lead + blank]


def test_the_scanner_is_paced() -> None:
    """Unpaced, the scanner issued 8,273 RPC reads/second and Azahar died."""
    import time as _t
    rpc = FakeRPC(BASE, build_space([make_mon(251, 30)], span=0x40000))
    t0 = _t.monotonic()
    crystal.scan_range(rpc, BASE, BASE + 0x40000, progress_every=0,
                       max_req_per_s=200.0)
    elapsed = _t.monotonic() - t0
    requests = rpc.bytes_read / 1024
    assert requests / max(elapsed, 1e-9) <= 260, (
        f"paced at {requests / elapsed:.0f} req/s, asked for 200")


def test_the_scan_ranges_avoid_graphics_memory() -> None:
    """A Game Boy title has no business in the renderer's pages.

    The old catch-all (0x08000000-0x40000000) covered the linear heap
    and VRAM. Reading those over RPC produced 215,516 "unmapped
    ReadBlock" errors in 21 seconds for pages that can never hold GB
    WRAM.
    """
    from pokebot import games

    vram_and_linear = (0x14000000, 0x20000000)
    for start, end in games.GB_VC_SCAN_RANGES:
        assert end <= vram_and_linear[0] or start >= vram_and_linear[1], (
            f"range {start:#010x}-{end:#010x} overlaps graphics memory")

    # And the hot band must actually contain both bases seen in the wild.
    hot_start, hot_end = games.GB_VC_HOT_3DS
    for observed in (0x08a2ffac, 0x08a3bf1a):
        assert hot_start <= observed < hot_end


def test_an_unconfirmed_attach_is_reported_not_assumed() -> None:
    """Azahar can accept SetProcess and still have no target selected.

    It then answers every read with zeros instead of an error, so the
    caller cannot tell a blank page from a detached emulator — which is
    what sent the scanner through the whole heap.
    """
    from pokebot import citra_rpc

    class Detached(citra_rpc.CitraRPC):
        def __init__(self):                 # no socket
            self._attached_pid = None
            self._attached_title = None
            self.sent = []

        def _send_request(self, req_type, payload):
            self.sent.append(req_type)
            return b"\x00\x00\x00\x00"      # get_process -> 0, i.e. none

        def list_processes(self):
            return {7: (0x0004000000172800, "trl")}

    rpc = Detached()
    with pytest.raises(citra_rpc.RPCError, match="no target process"):
        rpc.attach_to_pokemon_game()


def test_a_confirmed_attach_succeeds() -> None:
    from pokebot import citra_rpc
    import struct as _s

    class Attached(citra_rpc.CitraRPC):
        def __init__(self):
            self._attached_pid = None
            self._attached_title = None

        def _send_request(self, req_type, payload):
            return _s.pack("<I", 7)         # get_process -> our pid

        def list_processes(self):
            return {7: (0x0004000000172800, "trl")}

    pid, tid, name = Attached().attach_to_pokemon_game()
    assert (pid, name) == (7, "Crystal")


def test_candidates_are_churned_over_one_shared_window() -> None:
    """Every candidate must be sampled BEFORE the sleep, not after it.

    Probing serially compares samples taken at different moments:
    candidate A gets watched over 0.0-0.4s and B over 0.4-0.8s. The game
    is not equally busy in both, so a quiet first window and a busy
    second one hands the verdict to whichever copy happened to be
    measured second, whatever its liveness. That coin flip is what put
    the base on a stale copy and cost a 15-second Celebi battle.

    Asserted on read ORDER rather than on timing, because the property
    that matters is structural: serial probing reads A,A,B,B and shared
    probing reads A,B,A,B.
    """
    A, B = 0x08a2ffac, 0x08a3bf1a
    order = []

    class Recording:
        def read(self, addr, size):
            order.append("A" if addr == A else "B")
            return bytes(size)

    crystal.churn_many(Recording(), [A, B], size=8, gap=0.01)
    assert order == ["A", "B", "A", "B"], (
        f"read order {order} — candidates were sampled in separate "
        f"windows, so their churn is not comparable")


def test_shared_window_costs_one_gap_not_one_per_candidate() -> None:
    """The probe blocks the poll loop, so its cost must not scale."""
    import time as _t

    class Flat:
        def read(self, addr, size):
            return bytes(size)

    bases = [0x1000 * i for i in range(1, 6)]     # five candidates
    t0 = _t.monotonic()
    crystal.churn_many(Flat(), bases, size=8, gap=0.3)
    elapsed = _t.monotonic() - t0
    assert elapsed < 0.3 * 2, (
        f"five candidates took {elapsed:.2f}s; serial probing would be 1.5s")


def test_churn_many_survives_an_unreadable_candidate() -> None:
    """A base that has gone away must score last, not raise."""

    class Half:
        def read(self, addr, size):
            if addr == 0x2000:
                raise RuntimeError("unmapped")
            return bytes(size)

    scores = crystal.churn_many(Half(), [0x1000, 0x2000], size=8, gap=0.01)
    assert scores[0x2000] == -1 and scores[0x1000] == 0
