"""
Sun/Moon and Ultra Sun/Ultra Moon, Manual mode.

Three things had to change for Manual mode to work on Gen 7, and each
was broken before:

* the addresses: SM/USUM carried none of their own, so config.yaml's
  flat X/Y ones were used -- Kalos's memory map on an Alola game;
* the party search ignored the configured address and only ever swept
  Gen 6's application heap, while Gen 7's save block lives in the
  0x30000000 heap: 256 MB that must never be swept over 1 KB reads;
* the records: anything above #721 was rejected as junk, which is every
  Pokemon introduced in Alola.

The fake console holds real PK7 bytes at Gen 7's addresses and logs
every read, so the mode's own code runs against it unmodified.
"""
from __future__ import annotations

import logging
import struct
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot import games  # noqa: E402
from pokebot.modes import observe  # noqa: E402
from pokebot.parser import calc_checksum  # noqa: E402

GEN7 = ("SM-USA-1.2", "USUM-USA-1.2")
TID, SID = 0x1D1C, 0x0BAD
OT = "Roman"
SHINY_PID = (0x16B0 << 16) | 0x0001        # PSV == TSV for TID/SID above
PLAIN_PID = 0x9D1AE829

#: PKMN-NTR LookupTable.cs, verbatim.
NTR_WILD1 = 0x3254F4AC                     # all four games
NTR_WILD2 = 0x32663BF0                     # all four games
NTR_SOS = {"SM-USA-1.2": (0x3003969C, 0x3002F7B8),
           "USUM-USA-1.2": (0x30039888, 0x3002F9A0, 0x30030544)}


def pk7(*, key, species, pid=PLAIN_PID, ot="", party=False, level=50):
    """A plaintext PK7 with a correct checksum."""
    d = bytearray(260 if party else 232)
    struct.pack_into("<I", d, 0x00, key)
    struct.pack_into("<H", d, 0x08, species)
    struct.pack_into("<H", d, 0x0C, TID)
    struct.pack_into("<H", d, 0x0E, SID)
    struct.pack_into("<I", d, 0x18, pid)
    name = ot.encode("utf-16-le")
    d[0xB0:0xB0 + len(name)] = name
    if party:
        d[0xEC] = level
    struct.pack_into("<H", d, 0x06, calc_checksum(bytes(d)))
    return bytes(d)


def mystatus(ot=OT) -> bytes:
    """MyStatus7: TID16 @0x00, SID16 @0x02, OT @0x38 (PKHeX)."""
    d = bytearray(0xC0)
    struct.pack_into("<H", d, 0, TID)
    struct.pack_into("<H", d, 2, SID)
    name = ot.encode("utf-16-le")
    d[0x38:0x38 + len(name)] = name
    return bytes(d)


class Console:
    """Sparse Gen 7 memory: zeros wherever nothing was put."""

    def __init__(self):
        self.mem: dict[int, bytes] = {}
        self.later: list[tuple[float, int, bytes]] = []
        self.reads: list[tuple[int, int]] = []

    def put(self, addr, data):
        self.mem[addr] = bytes(data)

    def put_after(self, seconds, addr, data):
        self.later.append((time.monotonic() + seconds, addr, bytes(data)))

    def read(self, addr, n):
        now = time.monotonic()
        for item in [i for i in self.later if now >= i[0]]:
            self.later.remove(item)
            self.mem[item[1]] = item[2]
        self.reads.append((addr, n))
        out = bytearray(n)
        for a, d in self.mem.items():
            lo, hi = max(a, addr), min(a + len(d), addr + n)
            if lo < hi:
                out[lo - addr:hi - addr] = d[lo - a:hi - a]
        return bytes(out)


class Ctx:
    def __init__(self, console, key="USUM-USA-1.2", trainer=OT,
                 seconds=2.0):
        self.rpc = console
        self.game = games.GAMES[key]
        self.config = {"soft_reset": {"trainer_name": trainer}}
        self.target = None
        self._stop_evt = threading.Event()
        self._deadline = time.monotonic() + seconds
        self.events: list[tuple[str, dict]] = []
        events = self.events

        class Dash:
            @staticmethod
            def broadcast(kind, **kw):
                events.append((kind, kw))

        self.dashboard = Dash()

    def should_stop(self):
        return self._stop_evt.is_set() or time.monotonic() > self._deadline

    def request_stop(self, reason=""):
        self._stop_evt.set()

    def encounters(self):
        return [kw for k, kw in self.events if k == "encounter"]

    def party_species(self):
        parties = [kw["slots"] for k, kw in self.events if k == "party"]
        return [s["species"] for s in parties[-1]] if parties else []


@pytest.fixture
def no_files(monkeypatch):
    """A shiny here would otherwise write a real .pk6 into targets/."""
    import pokebot.pk6_export as pk6_export

    saved = []
    monkeypatch.setattr(pk6_export, "ensure_targets_dir", lambda: None)
    monkeypatch.setattr(pk6_export, "save_target_pk6",
                        lambda ctx, a, p, lbl: saved.append(p.species))
    return saved


def trainer_block(key):
    return games.LIVEHEX_REFERENCES[key]["trainer_block"]


def save_with_party(key, species=(722, 725, 807), shift=0):
    """A console holding the trainer block and the save-block party."""
    c = Console()
    c.put(trainer_block(key) + shift, mystatus())
    base = games.GAMES[key].offsets.party_base + shift
    for i, sp in enumerate(species):
        c.put(base + i * 260, pk7(key=0x5000 + i, species=sp, ot=OT,
                                  party=True))
    return c


def allowed(key):
    """Every byte Manual mode has any business reading on Gen 7."""
    o = games.GAMES[key].offsets
    span = observe._PARTY_HINT_SPAN
    regions = [(o.party_base - span, o.party_base + span),
               (trainer_block(key), trainer_block(key) + 0xC0)]
    regions += [(b, b + n) for b, n in observe.foe_windows(o)]
    return regions


def stray_reads(console, key):
    regions = allowed(key)
    return [(hex(a), n) for a, n in console.reads
            if not any(lo <= a and a + n <= hi for lo, hi in regions)]


# ----------------------------------------------------------------------
# Records: Alola's species are real on Gen 7, junk on Gen 6
# ----------------------------------------------------------------------
def test_alola_species_are_valid_on_gen7_and_rejected_on_gen6():
    rowlet = pk7(key=0x1234, species=722)
    zeraora = pk7(key=0x1235, species=807)
    beyond = pk7(key=0x1236, species=808)
    assert observe._parse_valid(rowlet, 807) is not None
    assert observe._parse_valid(zeraora, 807) is not None
    assert observe._parse_valid(beyond, 807) is None
    assert observe._parse_valid(rowlet) is None, (
        "the Gen 6 default no longer rejects #722")


def test_the_species_bound_follows_the_game():
    class G7:
        game = games.GAMES["USUM-USA-1.2"]

    class G6:
        game = games.GAMES["Y-USA"]

    assert observe._species_max(G7()) == 807
    assert observe._species_max(G6()) == 721
    assert observe._species_max(object()) == 721


# ----------------------------------------------------------------------
# The party: a window at the save block, never a sweep
# ----------------------------------------------------------------------
@pytest.mark.parametrize("key", GEN7)
def test_the_party_is_found_at_the_save_block(key):
    c = save_with_party(key)
    ctx = Ctx(c, key)
    party = observe.get_party(ctx, ctx.game.offsets.party_base, 260, OT)

    assert [p.species for p in party] == [722, 725, 807]
    assert stray_reads(c, key) == []


@pytest.mark.parametrize("shift", [-0x10, 0x40, -0x1000])
def test_the_party_is_found_when_azahar_shifts_it_a_little(shift):
    """X/Y's trainer block reads 0x10 low on Azahar."""
    key = "USUM-USA-1.2"
    c = save_with_party(key, shift=shift)
    ctx = Ctx(c, key)
    party = observe.get_party(ctx, ctx.game.offsets.party_base, 260, OT)
    assert [p.species for p in party] == [722, 725, 807]


@pytest.mark.parametrize("key", GEN7)
def test_box_one_is_outside_the_party_window(key):
    """It sits ~0x3000 above the party, with the player's OT on every
    record; inside the window it would read as more party members."""
    box1 = games.LIVEHEX_REFERENCES[key]["box1_slot1"]
    o = games.GAMES[key].offsets
    assert box1 >= o.party_base + observe._PARTY_HINT_SPAN


def test_gen6_keeps_its_own_party_ranges():
    assert observe._party_ranges(0x08CE1CF8) == observe._PARTY_SCAN_RANGES
    assert observe._party_ranges(0x08C814AC) == observe._PARTY_SCAN_RANGES
    assert observe._party_ranges(0) == observe._PARTY_SCAN_RANGES


# ----------------------------------------------------------------------
# Manual mode, end to end
# ----------------------------------------------------------------------
@pytest.mark.parametrize("key", GEN7)
def test_manual_mode_reports_the_wild_and_the_sos_ally(key, no_files):
    c = save_with_party(key, species=(722,))
    # The player's own battle copy, and a leftover from before the run.
    c.put(NTR_WILD1 + 0x400, pk7(key=0x5000, species=722, ot=OT))
    c.put(NTR_WILD2, pk7(key=0x9999, species=10))
    # Then a wild battle, and an SOS ally -- shiny -- answering a call.
    c.put_after(0.3, NTR_WILD1, pk7(key=0xA001, species=731))
    c.put_after(0.7, NTR_SOS[key][0],
                pk7(key=0xA002, species=731, pid=SHINY_PID))
    ctx = Ctx(c, key)
    observe.run(ctx)

    got = [(e["species"], e["shiny"], e["address"])
           for e in ctx.encounters()]
    assert got == [(731, False, f"{NTR_WILD1:#010x}"),
                   (731, True, f"{NTR_SOS[key][0]:#010x}")]
    assert ctx.party_species() == [722]
    assert no_files == [731], "the shiny ally's .pk6 was not saved"


@pytest.mark.parametrize("key", GEN7)
def test_manual_mode_never_reads_outside_its_windows(key, no_files):
    c = save_with_party(key)
    c.put_after(0.3, NTR_WILD1, pk7(key=0xA001, species=731))
    observe.run(Ctx(c, key, seconds=1.2))

    assert c.reads, "nothing was read at all"
    assert stray_reads(c, key) == []


def test_one_wild_seen_in_two_windows_is_reported_once(no_files):
    """WildOffset1 and WildOffset2 can both hold the opponent."""
    key = "USUM-USA-1.2"
    c = save_with_party(key, species=(722,))
    wild = pk7(key=0xA001, species=731)
    c.put_after(0.3, NTR_WILD1, wild)
    c.put_after(0.3, NTR_WILD2, wild)
    ctx = Ctx(c, key, seconds=1.2)
    observe.run(ctx)
    assert [e["species"] for e in ctx.encounters()] == [731]


def test_a_record_with_the_players_ot_is_never_a_wild(no_files):
    """A wild has no OT until it is caught. One carrying the player's is
    theirs -- here a copy whose key the party read does not know."""
    key = "USUM-USA-1.2"
    c = save_with_party(key, species=(722,))
    c.put_after(0.3, NTR_WILD1, pk7(key=0xB0B0, species=725, ot=OT))
    ctx = Ctx(c, key, seconds=1.2)
    observe.run(ctx)
    assert ctx.encounters() == []


def test_the_game_names_its_trainer_when_config_has_it_wrong(
        no_files, caplog):
    key = "USUM-USA-1.2"
    c = save_with_party(key)
    ctx = Ctx(c, key, trainer="Ash", seconds=0.8)
    with caplog.at_level(logging.INFO, logger=observe.__name__):
        observe.run(ctx)

    assert ctx.party_species() == [722, 725, 807]
    assert "trainer block" in caplog.text and "'Roman'" in caplog.text
    assert "ID No." in caplog.text


def test_an_unreadable_trainer_block_says_so(no_files, caplog):
    """If Azahar keeps Gen 7's memory elsewhere, this is the first line
    that shows it."""
    ctx = Ctx(Console(), "USUM-USA-1.2", seconds=0.5)
    with caplog.at_level(logging.WARNING, logger=observe.__name__):
        observe.run(ctx)
    assert "holds no readable name" in caplog.text
    assert "no party found yet" in caplog.text


def test_gen6_never_reads_a_trainer_block(no_files):
    class Off:
        party_base = 0
        party_stride = 484
        foe_base = 0x08800000
        foe_scan_len = 0x1000

    class G:
        offsets = Off()
        generation = 6
        key = "Y-USA"

    ctx = Ctx(Console(), "USUM-USA-1.2", seconds=0.3)
    ctx.game = G()
    assert observe.gen7_trainer_ot(ctx) is None
    assert ctx.rpc.reads == []


def test_a_gen7_shiny_is_saved_as_pk7(tmp_path, monkeypatch):
    """Same bytes as a .pk6, but PKHeX takes the format from the
    extension -- as .pk6 an Alola species opens as invalid Gen 6."""
    import pokebot.pk6_export as pk6_export

    monkeypatch.setattr(pk6_export, "TARGETS_DIR", tmp_path)
    monkeypatch.setattr(pk6_export, "ensure_targets_dir", lambda: tmp_path)
    key = "USUM-USA-1.2"
    addr = NTR_SOS[key][0]
    rec = pk7(key=0xA002, species=731, pid=SHINY_PID)
    c = Console()
    c.put(addr, rec)
    ctx = Ctx(c, key)
    pkm = observe.read_pk6_at(ctx, addr)
    assert pkm is not None and pkm.shiny

    path = pk6_export.save_target_pk6(ctx, addr, pkm, "shiny")
    assert path is not None and path.suffix == ".pk7"
    assert path.read_bytes() == rec


def test_gen6_targets_stay_pk6():
    import pokebot.pk6_export as pk6_export

    class G6:
        game = games.GAMES["Y-USA"]

    assert pk6_export.file_ext(G6()) == "pk6"
    assert pk6_export.file_ext(object()) == "pk6"


# ----------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------
@pytest.mark.parametrize("key", GEN7)
def test_gen7_offers_manual_mode_only(key):
    assert [m.mode for m in games.methods_for(key)] == ["observe"]


def test_gen6_menus_are_unchanged():
    assert [m.mode for m in games.methods_for("OR-USA")] == [
        "static_encounter"]
    xy = [m.mode for m in games.methods_for("Y-USA")]
    assert "observe" in xy and "soft_reset" in xy and "noibat" in xy


@pytest.mark.parametrize("key", GEN7)
def test_the_party_address_is_the_trainer_block_plus_0xcc(key):
    o = games.GAMES[key].offsets
    assert o.party_base == trainer_block(key) + 0xCC
    assert o.party_stride == 260


def test_usum_party_is_pkmn_ntrs_published_alternate():
    assert games.GAMES["USUM-USA-1.2"].offsets.party_base == 0x330128E4


@pytest.mark.parametrize("key", GEN7)
def test_every_pkmn_ntr_wild_address_is_inside_a_window(key):
    windows = observe.foe_windows(games.GAMES[key].offsets)

    def inside(addr):
        return any(b <= addr and addr + 232 <= b + n for b, n in windows)

    for addr in (NTR_WILD1, NTR_WILD2, *NTR_SOS[key]):
        assert inside(addr), f"{addr:#010x} is in no window"


@pytest.mark.parametrize("key", GEN7)
def test_the_shipped_config_cannot_put_xy_addresses_on_gen7(key):
    import yaml

    with open(REPO / "config.yaml", encoding="utf-8") as f:
        shipped = yaml.safe_load(f)["offsets"]
    eff = games.resolve_offsets(key, shipped)
    o = games.GAMES[key].offsets
    assert eff["party_base"] == o.party_base
    assert eff["foe_base"] == o.foe_base
