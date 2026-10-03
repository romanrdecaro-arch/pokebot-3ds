"""
Drives the REAL Noibat hunt against a simulated cave.

The unit tests next door check ``is_hit`` in isolation. These run
``noibat.run`` with a fake foe window and watch what it actually does,
because the behaviour that matters here is a sequence, not a predicate:
**a shiny of the wrong species has to be saved and then reset over.**

Both halves matter and they pull opposite ways. Stopping on the wrong
shiny wastes the user's run. Resetting over it without exporting first
destroys the only record it existed. A test for either one alone would
pass on a version that got the other badly wrong.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot.modes import noibat  # noqa: E402

FOE = 0x08800000
SLOT = FOE + 0x3ECC
NOIBAT = 714
ZUBAT = 41


class Mon:
    def __init__(self, species, shiny=False, key=None):
        self.species = species
        self.shiny = shiny
        self.encryption_key = key if key is not None else id(self) & 0xFFFF
        self.pid = 0x2000 + (self.encryption_key & 0xFFF)
        self.nickname = ""
        self.nature = "Timid"
        self.nature_id = 10
        self.gender = "M"
        self.ivs = {"HP": 31, "Atk": 0, "Def": 31,
                    "SpA": 31, "SpD": 31, "Spe": 31}
        self.tsv, self.psv = 7452, 1
        self.ability_id, self.ability_num = 1, 1
        self.moves = []
        self.party = {"level": 25}
        self.exp = 8000
        self.source_address = SLOT


class Cave:
    """A save beside a shaking spot, and a queue of what it yields."""

    def __init__(self, encounters, tiles=3):
        self.queue = list(encounters)
        self.tiles = tiles          # holds needed before the spot fires
        self.held = None
        self.held_ticks = 0
        self.wild = None
        self.resets = 0
        self.releases = 0
        self.taps: list[str] = []
        self.touches: list[tuple] = []

    # --- controller ---
    def diagnose(self):
        return {"dry_run": False, "driver": "fake"}

    def hold(self, button):
        self.held = button
        return True

    def release(self, button):
        self.releases += 1
        self.held = None
        return True

    def tap(self, button, hold_s=0.05):
        self.taps.append(button)
        return "postmessage"

    def tap_touch(self, x, y, hold_s=0.08):
        self.touches.append((x, y))
        return True

    def soft_reset(self, hold_s=0.5):
        self.resets += 1
        self.wild = None
        self.held_ticks = 0

    # --- what the foe window holds ---
    def step(self):
        """Called on every poll: walking eventually hits the spot."""
        if self.held and self.wild is None:
            self.held_ticks += 1
            if self.held_ticks >= self.tiles and self.queue:
                self.wild = self.queue.pop(0)

    def foe(self):
        self.step()
        return [(SLOT, self.wild)] if self.wild else []


class Ctx:
    def __init__(self, cave, target=None, seconds=15.0, cfg=None):
        self.input = cave
        self.cave = cave
        self.target = target
        self.rpc = object()
        self.config = {"noibat": cfg or dict(FAST)}
        self._stop_evt = threading.Event()
        real = self._stop_evt.wait
        self._stop_evt.wait = lambda t=None: real(0)   # type: ignore
        self.events: list[tuple] = []
        self.ran_away = False
        self._deadline = time.monotonic() + seconds

        class Off:
            foe_base = FOE
            foe_scan_len = 0x20000
            party_base = 0x08C79DA8
            party_stride = 484

        class G:
            offsets = Off()
            key = "XY"

        self.game = G()

        class Dash:
            @staticmethod
            def broadcast(kind, **kw):
                self.events.append((kind, kw))

        self.dashboard = Dash()

    def should_stop(self):
        if self._stop_evt.is_set():
            return True
        if time.monotonic() > self._deadline:
            self.ran_away = True
            return True
        return False

    def request_stop(self, reason=""):
        self._stop_evt.set()

    def kinds(self):
        return [k for k, _ in self.events]

    def reasons(self):
        return [kw.get("reason", "") for k, kw in self.events]


FAST = {"direction": "up", "species": NOIBAT,
        "encounter_timeout": 1.5, "poll_gap": 0.001, "settle": 0.0}


@pytest.fixture
def wired(monkeypatch):
    saved: list = []

    def install(cave, catch_ok=True):
        monkeypatch.setattr(noibat, "ensure_targets_dir", lambda: None)
        monkeypatch.setattr(noibat, "broadcast_party",
                            lambda ctx, p: set())
        monkeypatch.setattr(noibat, "get_party",
                            lambda ctx, b, s, ot: [Mon(25, key=9001)])
        monkeypatch.setattr(noibat, "scan_nonparty",
                            lambda ctx, b, n, keys: cave.foe())
        monkeypatch.setattr(noibat, "read_pk6_at",
                            lambda ctx, addr: (cave.wild if addr == SLOT
                                               else None))

        def fake_save(ctx, addr, pkm, label):
            saved.append((label, pkm.species, pkm.shiny))
            return f"targets/{label}.pk6"

        monkeypatch.setattr(noibat, "save_target_pk6", fake_save)
        monkeypatch.setattr(
            noibat, "soft_reset_and_wait",
            lambda ctx, loaded, plan, last=None: (
                cave.soft_reset() or True))

        class Result:
            caught = catch_ok
            detail = "ball landed" if catch_ok else "no confirmation"
            party_full = False

        import pokebot.modes.catch as catch_mod
        monkeypatch.setattr(catch_mod, "catch_wild",
                            lambda *a, **k: Result())
        return cave

    install.saved = saved        # type: ignore[attr-defined]
    return install


def run(ctx):
    noibat.run(ctx)
    assert not ctx.ran_away, "the hunt never stopped on its own"


# ----------------------------------------------------------------------
# The rule, end to end
# ----------------------------------------------------------------------
def test_a_shiny_noibat_stops_and_is_caught(wired):
    cave = wired(Cave([Mon(NOIBAT, shiny=True)]))
    ctx = Ctx(cave)

    run(ctx)

    assert "target_caught" in ctx.kinds()
    assert cave.resets == 0, "reset away a shiny Noibat"


def test_a_plain_noibat_is_reset(wired):
    cave = wired(Cave([Mon(NOIBAT), Mon(NOIBAT, shiny=True)]))
    ctx = Ctx(cave)

    run(ctx)

    assert cave.resets == 1
    assert "target_caught" in ctx.kinds()


def test_a_shiny_of_another_species_is_reset_over(wired):
    """The ask, stated in capitals: only shiny Noibat is wanted.

    Every other hunt in this bot would stop here, and stopping would
    be the safe-looking mistake -- it is not what was asked for, and
    it would end the run on the wrong Pokemon.
    """
    cave = wired(Cave([Mon(ZUBAT, shiny=True),
                       Mon(NOIBAT, shiny=True)]))
    ctx = Ctx(cave)

    run(ctx)

    assert cave.resets == 1, "did not reset over the shiny Zubat"
    caught = [kw for k, kw in ctx.events if k == "target_caught"]
    assert caught and caught[0]["species"] == NOIBAT


def test_the_discarded_shiny_is_exported_first(wired):
    """Resetting over it is irreversible; the .pk6 is the only record
    it ever existed."""
    cave = wired(Cave([Mon(ZUBAT, shiny=True),
                       Mon(NOIBAT, shiny=True)]))
    ctx = Ctx(cave)

    run(ctx)

    labels = [lbl for lbl, _sp, _sh in wired.saved]
    species = [sp for _lbl, sp, _sh in wired.saved]
    assert ZUBAT in species, (
        f"reset over a shiny without saving it: {wired.saved}")
    assert any("not-noibat" in lbl for lbl in labels)


def test_ordinary_misses_are_not_exported(wired):
    """targets/ would fill with thousands of identical Zubat."""
    cave = wired(Cave([Mon(ZUBAT), Mon(ZUBAT), Mon(NOIBAT, shiny=True)]))
    ctx = Ctx(cave)

    run(ctx)

    assert all(sh for _lbl, _sp, sh in wired.saved), (
        f"exported a non-shiny miss: {wired.saved}")


def test_the_evolution_is_not_mistaken_for_it(wired):
    """Noivern is 715 and sits right next to Noibat in the dex."""
    cave = wired(Cave([Mon(715, shiny=True), Mon(NOIBAT, shiny=True)]))
    ctx = Ctx(cave)

    run(ctx)

    assert cave.resets == 1
    caught = [kw for k, kw in ctx.events if k == "target_caught"]
    assert caught[0]["species"] == NOIBAT


# ----------------------------------------------------------------------
# Holding the direction
# ----------------------------------------------------------------------
def test_the_chosen_direction_is_held(wired):
    cave = wired(Cave([Mon(NOIBAT, shiny=True)]))
    ctx = Ctx(cave, cfg={**FAST, "direction": "left"})

    held = []
    real_hold = cave.hold

    def hold(button):
        held.append(button)
        return real_hold(button)

    cave.hold = hold                            # type: ignore
    run(ctx)

    assert held == ["DpadLeft"]


def test_the_direction_is_released_on_the_way_out(wired):
    """A latched direction outlives the bot: the player takes back a
    game that is still walking into a wall."""
    cave = wired(Cave([Mon(NOIBAT, shiny=True)]))
    ctx = Ctx(cave)

    run(ctx)

    assert cave.releases >= 1
    assert cave.held is None


def test_the_direction_is_released_even_when_stopped_mid_walk(wired):
    """Stop the hunt WHILE it is walking, not before it starts.

    The first version of this stopped during the baseline scan, so the
    walk never began -- nothing was held, nothing needed releasing,
    and the test passed without touching the code it names.
    """
    cave = wired(Cave([], tiles=99))
    ctx = Ctx(cave, seconds=6.0)
    real_foe = cave.foe

    def foe():
        out = real_foe()
        if cave.held:                 # only once the walk is underway
            ctx.request_stop("user")
        return out

    cave.foe = foe                              # type: ignore
    noibat.run(ctx)

    assert cave.releases >= 1, "stopped mid-walk without releasing"
    assert cave.held is None, "left a direction held after stopping"


def test_nothing_is_tapped_while_walking(wired):
    """Only the hold. A stray A would talk to whatever is in front."""
    cave = wired(Cave([Mon(NOIBAT)], tiles=3))
    ctx = Ctx(cave, seconds=6.0)

    def stop_after_one(ctx=ctx):
        ctx.request_stop("one attempt")

    real_reset = cave.soft_reset

    def reset(hold_s=0.5):
        real_reset(hold_s)
        stop_after_one()

    cave.soft_reset = reset                     # type: ignore
    noibat.run(ctx)

    assert cave.taps == [], f"pressed {cave.taps} while walking"


# ----------------------------------------------------------------------
# Not finding anything
# ----------------------------------------------------------------------
def test_a_walk_that_finds_nothing_resets_and_tries_again(wired):
    cave = wired(Cave([Mon(NOIBAT, shiny=True)], tiles=10**9))
    ctx = Ctx(cave, seconds=12.0)

    # First attempt walks into nothing; the spot works after the reset.
    def reset(hold_s=0.5):
        cave.resets += 1
        cave.wild = None
        cave.held_ticks = 0
        cave.tiles = 2

    cave.soft_reset = reset                     # type: ignore
    run(ctx)

    assert cave.resets >= 1
    assert "no encounter while walking" in " ".join(ctx.reasons())
    assert "target_caught" in ctx.kinds()


def test_a_stale_wild_is_not_read_as_this_attempt(wired):
    """The foe buffer keeps the last wild. Without a baseline, attempt
    one evaluates a Pokemon nobody just met -- and if that stale one
    happens to be a shiny Noibat the hunt stops having done nothing."""
    cave = Cave([Mon(NOIBAT)], tiles=2)
    cave.wild = Mon(NOIBAT, shiny=True, key=4242)   # left over
    wired(cave)
    ctx = Ctx(cave, seconds=8.0)

    def reset(hold_s=0.5):
        cave.resets += 1
        cave.wild = None
        cave.held_ticks = 0
        ctx.request_stop("one attempt is enough")

    cave.soft_reset = reset                     # type: ignore
    noibat.run(ctx)

    assert "target_caught" not in ctx.kinds(), (
        "stopped on a wild that was already in the buffer at startup")
