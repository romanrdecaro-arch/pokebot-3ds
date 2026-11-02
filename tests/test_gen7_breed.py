"""
Ultra Sun / Ultra Moon breeding by egg seed (pokebot/modes/gen7_breed.py).

The fake Nursery acts as the game is described by PKMN-NTR's breeding
bot and 3DSRNGTool: A starts a talk, two presses move her text along,
then at her question A takes the egg (the seed moves past everything it
drew) and B declines it (the seed moves one step). Not yet seen live.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot import egg7 as e  # noqa: E402
from pokebot.modes import gen7_breed as gb  # noqa: E402
from pokebot.parser import calc_checksum  # noqa: E402
from test_gen7_encounter import FakeDashboard, InstantEvent  # noqa: E402
from test_xy_starter_screen import Memory  # noqa: E402

BASE = 0x3307B010
BAG = 0x33011934
TB = 0x33012818
LIVE = 0x33F7FA44
SEED = (305419896, 2271560481, 3735928559, 3237998016)
MASUDA = e.Parents(0, 0, True, False, True, False)


def pk7(*, key, species, pid, gender="M", item=0, lang=2, party=False,
        egg=False, nature=0, ivs=(0,) * 6) -> bytes:
    """``ivs`` in HP, Atk, Def, SpA, SpD, Spe order."""
    d = bytearray(260 if party else 232)
    struct.pack_into("<I", d, 0x00, key)
    struct.pack_into("<H", d, 0x08, species)
    struct.pack_into("<H", d, 0x0A, item)
    struct.pack_into("<I", d, 0x18, pid)
    d[0x1C] = nature
    d[0x1D] = {"M": 0, "F": 2, "G": 4}[gender]
    d[0xE3] = lang
    hp, atk, de, spa, spd, spe = ivs                # the PK7 keeps Spe 4th
    word = hp | atk << 5 | de << 10 | spe << 15 | spa << 20 | spd << 25
    struct.pack_into("<I", d, 0x74, word | egg << 30)
    if party:
        d[0xEC] = 1
    struct.pack_into("<H", d, 0x06, calc_checksum(bytes(d)))
    return bytes(d)


def tsv_with_shiny_within(parents, lo, hi, game=None):
    """A TSV whose first shiny egg (by ``parents``) is lo..hi declines
    away; with ``game``, one the game's own parents would NOT draw shiny."""
    for tsv in range(4096):
        n = e.eggs_until_shiny(SEED, parents, tsv, limit=hi + 1)
        if n is not None and n >= lo and (game is None or not e.generate(
                e.Stream(SEED), n, game, tsv).shiny):
            return tsv, n
    raise AssertionError("no such TSV")


class World(Memory):
    """The Nursery, the trainer block and the live party, plus the lady."""

    def __init__(self, ctx, tsv, game_parents=MASUDA, langs=(2, 1),
                 occupied=True, answers=True, party_full=False,
                 b_takes_it=False, bag=()):
        super().__init__()
        for i, (item, count) in enumerate(bag):     # InventoryItem7 words
            self.put(BAG + 4 * i, struct.pack("<I", item | count << 10))
        self.ctx, self.tsv, self.game = ctx, tsv, game_parents
        self.answers, self.talk, self.b_takes_it = answers, None, b_takes_it
        self.declined = self.accepted = self.flag_writes = 0
        if occupied:
            self.put(BASE, b"\x01")
            self.put(BASE + 0xE9, b"\x01")
        # Raichu M + Pikachu F: not one species, so no ball draw (MASUDA)
        self.put(BASE + 0x001, pk7(key=11, species=26, pid=0x1111, lang=langs[0]))
        self.put(BASE + 0x0EA, pk7(key=12, species=25, pid=0x2222, gender="F",
                                   lang=langs[1]))
        self.put(BASE + 0x1DC, struct.pack("<4I", *SEED))
        tid = tsv << 4
        self.put(TB, struct.pack("<HH", tid, 0))
        self.put(TB + 0x38, "Roman".encode("utf-16-le"))
        for slot in range(6 if party_full else 1):
            self.put(LIVE + slot * 484, pk7(key=100 + slot, species=19,
                                            pid=0x3333, party=True))

    def seed(self):
        return struct.unpack("<4I", self.read(BASE + 0x1DC, 16))

    def write(self, addr, data):
        if addr == BASE + 0x1D8 and data == b"\x01":
            self.flag_writes += 1
        self.put(addr, data)

    def press(self, button):
        if self.talk is None:
            if button == "A" and self.answers:
                self.talk = 0 if self.read(BASE + 0x1D8, 1) == b"\x01" else "chat"
            return
        if self.talk == "chat" or self.talk >= 3:   # her last line closes
            self.talk = None
            return
        self.talk += 1
        if self.talk < 3:
            return
        seed = self.seed()                          # her question
        if button == "A" or self.b_takes_it:
            egg = e.generate(e.Stream(seed), 0, self.game, self.tsv)
            seed = e.advance(seed, egg.used)
            self.accepted += 1
            for slot in range(6):
                if self.read(LIVE + slot * 484, 4) == b"\0\0\0\0":
                    self.put(LIVE + slot * 484, pk7(
                        key=egg.ec, species=25, pid=egg.pid, party=True,
                        egg=True, nature=egg.nature, ivs=egg.ivs))
                    break
        else:
            seed = e.advance(seed, 1)
            self.declined += 1
        self.put(BASE + 0x1DC, struct.pack("<4I", *seed))
        self.put(BASE + 0x1D8, b"\x00")


class FakeInput:
    def __init__(self):
        self.events, self.world = [], None

    def tap(self, button, hold_s=0.05):
        self.events.append(button)
        self.world.press(button)


class Ctx:
    class game:
        key, generation = "USUM-USA-1.2", 7

        class offsets:
            party_live = LIVE

    def __init__(self, config=None):
        self.input, self.dashboard = FakeInput(), FakeDashboard()
        self.config, self._stop_evt, self.stop_reason = config or {}, InstantEvent(), None

    def should_stop(self):
        return self._stop_evt.is_set()

    def request_stop(self, reason=""):
        self.stop_reason = reason
        self._stop_evt.set()


def run(monkeypatch, tsv, config=None, **world):
    ctx = Ctx(config)
    ctx.rpc = ctx.input.world = World(ctx, tsv, **world)
    monkeypatch.setattr(gb, "_focus_if_needed", lambda ctx: None)
    gb.run(ctx)
    return ctx, ctx.rpc


def test_declines_until_the_shiny_egg_then_takes_it_and_stops(monkeypatch,
                                                               caplog):
    tsv, n = tsv_with_shiny_within(MASUDA, 3, 12)
    ctx, w = run(monkeypatch, tsv, bag=ITEMS)
    assert ctx.stop_reason == "shiny egg"
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    assert (w.declined, w.accepted) == (n, 1)
    assert w.flag_writes == n + 1                   # no walking: the flag
    got = w.read(LIVE + 484, 260)
    pid = struct.unpack_from("<I", got, 0x18)[0]
    assert ((pid >> 16) ^ (pid & 0xFFFF)) >> 4 == tsv
    assert ctx.dashboard.sent[-1][0] == "target_hit"
    assert "party slot 2" in ctx.dashboard.sent[-1][1]["reason"]
    left = [k["eggs_left"] for t, k in ctx.dashboard.sent if t == "status"]
    assert left == list(range(n, -1, -1))           # exact, counting down


def test_a_full_party_sends_it_to_the_pc_and_the_seed_vouches(monkeypatch):
    tsv, n = tsv_with_shiny_within(MASUDA, 1, 12)
    ctx, w = run(monkeypatch, tsv, party_full=True)
    assert ctx.stop_reason == "shiny egg" and w.accepted == 1
    assert "PC" in ctx.dashboard.sent[-1][1]["reason"]


def test_a_shiny_charm_set_but_not_owned_is_caught(monkeypatch):
    charm = e.Parents(0, 0, True, False, True, True)
    tsv, n = tsv_with_shiny_within(charm, 0, 60, game=MASUDA)
    ctx, w = run(monkeypatch, tsv, config={"breeding": {"shiny_charm": True}})
    assert ctx.stop_reason == "egg not shiny"
    assert w.accepted == 1
    assert "target_hit" not in [t for t, _ in ctx.dashboard.sent]


def test_a_decline_that_takes_the_egg_stops_at_once(monkeypatch):
    tsv, _ = tsv_with_shiny_within(MASUDA, 3, 12)
    ctx, w = run(monkeypatch, tsv, b_takes_it=True)
    assert ctx.stop_reason == "decline failed"
    assert (w.declined, w.accepted) == (0, 1)


ITEMS = [(1, 30), (4, 99), (17, 5), (28, 1), (50, 3), (213, 1)]
CHARM_ONLY = e.Parents(0, 0, True, False, False, True)


def test_a_shiny_charm_in_the_bag_is_found_and_used(monkeypatch):
    tsv, n = tsv_with_shiny_within(CHARM_ONLY, 0, 3000)
    ctx, w = run(monkeypatch, tsv, langs=(2, 2), game_parents=CHARM_ONLY,
                 bag=ITEMS + [(gb._SHINY_CHARM, 1)])
    assert ctx.stop_reason == "shiny egg"
    assert (w.declined, w.accepted) == (n, 1)


def test_a_bag_without_the_charm_means_none(monkeypatch):
    ctx, w = run(monkeypatch, 5, langs=(2, 2), bag=ITEMS)
    assert ctx.stop_reason == "no Masuda / Shiny Charm"


def test_a_bag_that_does_not_read_as_one_is_not_trusted():
    w = World(Ctx(), 5, bag=ITEMS + [(gb._SHINY_CHARM, 1), (1000, 1)])
    assert gb.bag_has(w, BAG, gb._SHINY_CHARM) is None   # no item 1000
    assert gb.bag_has(World(Ctx(), 5), BAG, gb._SHINY_CHARM) is None  # empty


def test_without_masuda_or_the_charm_nothing_is_touched(monkeypatch):
    ctx, w = run(monkeypatch, 5, langs=(2, 2))
    assert ctx.stop_reason == "no Masuda / Shiny Charm"
    assert ctx.input.events == [] and w.flag_writes == 0


def test_an_empty_nursery_writes_nothing(monkeypatch):
    ctx, w = run(monkeypatch, 5, occupied=False)
    assert ctx.stop_reason == "nursery not found"
    assert ctx.input.events == [] and w.flag_writes == 0


def test_a_lady_who_never_answers_stops_it(monkeypatch):
    monkeypatch.setattr(gb, "_ANSWER_TIMEOUT", 0.01)
    tsv, _ = tsv_with_shiny_within(MASUDA, 3, 12)
    ctx, w = run(monkeypatch, tsv, answers=False)
    assert ctx.stop_reason == "nursery lady not answering"
    assert w.declined == w.accepted == 0
