"""
Drives the REAL gift hunt against a simulated game.

The unit tests next door cover the pure helpers -- what counts as an
arrival, what counts as a target. These run ``gifts.run`` with the
actual press-and-detect and reset-and-confirm machinery underneath, a
fake party that a fake NPC adds to, and watch what comes out.

Which is where the two real hazards live.

Mashing A past the moment the gift lands walks into the nickname
keyboard, and nothing in this bot knows the way out of that. Only the
loop can show that the presses actually stop in time.

And if a reset does not take, last attempt's gift is still in the
party -- so the next attempt sees it as a fresh arrival, evaluates the
same Pokemon, and the hunt spins forever looking busy. Only the loop
can show that it notices.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot.modes import gifts  # noqa: E402
from pokebot.modes import soft_reset as sr  # noqa: E402


class Mon:
    def __init__(self, key, species=131, shiny=False):
        self.encryption_key = key
        self.species = species
        self.shiny = shiny
        self.pid = 0x1234 + key
        self.nickname = ""
        self.nature = "Adamant"
        self.nature_id = 3
        self.gender = "F"
        self.ivs = {"HP": 31, "Atk": 0, "Def": 31,
                    "SpA": 31, "SpD": 31, "Spe": 0}
        self.tsv, self.psv = 1234, 4321
        self.ability_id, self.ability_num = 11, 1
        self.moves = []
        self.party = {"level": 30}
        self.source_address = 0x08C79DA8 + key


class Game:
    """A save with a party, an NPC that hands over a Pokemon after a
    few A presses, and a reset that puts everything back."""

    def __init__(self, gifts_in_order, presses_needed=5):
        self.team = [Mon(1, 25), Mon(2, 4)]
        self.queue = list(gifts_in_order)
        self.presses_needed = presses_needed
        self.presses = 0
        self.held = None
        self.resets = 0
        self.taps: list[str] = []

    # --- the controller ---
    def tap(self, button, hold_s=0.05):
        # A press costs real time on the real driver, and the overshoot
        # this file cares about is detect_every / press_period -- which
        # is unbounded if a press is free. So the fake pays too.
        time.sleep(hold_s)
        self.taps.append(button)
        if button == "A":
            self.presses += 1
            if (self.held is None
                    and self.presses >= self.presses_needed
                    and self.queue):
                self.held = self.queue.pop(0)
        return "postmessage"

    def soft_reset(self, hold_s=0.5):
        self.resets += 1
        self.held = None                 # back to the save
        self.presses = 0

    def hold(self, button):
        raise AssertionError("a gift hunt holds no direction")

    def release(self, button):
        return True

    # --- what a party read sees ---
    def party(self, contiguous=True):
        """A cached/tight read sees only the save-block team.

        A gift can land in a live buffer outside the window the cache
        was anchored on, so a contiguous read misses it -- and misses
        it silently, looking exactly like "not arrived yet". Modelled
        here so a read that trusts the cache fails the tests rather
        than passing them.
        """
        if contiguous:
            return list(self.team)
        return list(self.team) + ([self.held] if self.held else [])


class Ctx:
    def __init__(self, game, target=None, seconds=10.0):
        self.input = game
        self.game_sim = game
        self.target = target
        self.rpc = object()
        self.config = {"soft_reset": {"trainer_name": "ASH"}}
        self._stop_evt = threading.Event()
        real = self._stop_evt.wait
        self._stop_evt.wait = lambda t=None: real(0)   # type: ignore
        self.events: list[tuple] = []
        self.ran_away = False
        self._deadline = time.monotonic() + seconds

        class Offsets:
            party_base = 0x08C79DA8
            party_stride = 484

        class G:
            offsets = Offsets()
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
        return [kw.get("reason") for k, kw in self.events
                if k == "read_failure"]


@pytest.fixture
def wired(monkeypatch):
    """Point the gift hunt at a simulated game instead of Azahar."""
    def install(game):
        monkeypatch.setattr(gifts, "ensure_targets_dir", lambda: None)
        monkeypatch.setattr(gifts, "_focus_if_needed", lambda ctx: None)
        monkeypatch.setattr(sr, "_focus_if_needed", lambda ctx: None)
        monkeypatch.setattr(gifts, "broadcast_party",
                            lambda ctx, p: set())
        monkeypatch.setattr(gifts, "get_party",
                            lambda ctx, b, s, ot, contiguous=True:
                            game.party(contiguous))
        monkeypatch.setattr(gifts, "save_target_pk6",
                            lambda ctx, addr, pkm, label: None)
        return game
    return install


FAST = {"trainer_name": "ASH", "press_hold": 0.01, "press_interval": 0.0,
        "detect_every": 0.01, "receive_timeout": 3.0,
        "reset_timeout": 3.0, "pre_reset_quiet": 0.0,
        "reload_read_grace": 0.0, "reset_cooldown": 0.0}


#: Every run opens with one reset before taking the baseline, so that
#: the baseline is the party AS SAVED. Counted separately here so the
#: tests below say how many MISSES they expect rather than carrying a
#: mystery +1.
OPENING_RESET = 1


def run(ctx):
    ctx.config = {"soft_reset": FAST}
    gifts.run(ctx)
    assert not ctx.ran_away, "the hunt never stopped on its own"


def misses(game):
    """Resets caused by a non-target gift."""
    return game.resets - OPENING_RESET


# ----------------------------------------------------------------------
# The happy paths
# ----------------------------------------------------------------------
def test_a_shiny_stops_the_hunt(wired):
    game = wired(Game([Mon(99, 131, shiny=True)]))
    ctx = Ctx(game)

    run(ctx)

    assert "target_hit" in ctx.kinds()
    assert misses(game) == 0, "reset away a shiny"
    assert game.held is not None, "the shiny is not in the party"


def test_a_plain_gift_resets_and_tries_again(wired):
    game = wired(Game([Mon(10), Mon(11), Mon(12, shiny=True)]))
    ctx = Ctx(game)

    run(ctx)

    assert misses(game) == 2, f"expected two misses, got {misses(game)}"
    assert "target_hit" in ctx.kinds()


def test_every_attempt_is_reported(wired):
    game = wired(Game([Mon(10), Mon(11, shiny=True)]))
    ctx = Ctx(game)

    run(ctx)

    attempts = [kw["count"] for k, kw in ctx.events
                if k == "soft_reset_attempt"]
    assert attempts == [1, 2]
    assert ctx.kinds().count("candidate") == 2


def test_a_configured_filter_can_stop_it_too(wired):
    class Target:
        rules = ["nature"]

        @staticmethod
        def matches(pkm):
            return pkm.species == 131

        @staticmethod
        def describe(pkm):
            return "Lapras"

    game = wired(Game([Mon(10, species=133), Mon(11, species=131)]))
    ctx = Ctx(game, target=Target())

    run(ctx)

    assert "target_hit" in ctx.kinds()
    assert misses(game) == 1


# ----------------------------------------------------------------------
# The nickname keyboard is one press past the gift landing
# ----------------------------------------------------------------------
def test_the_presses_stop_promptly_once_the_gift_lands(wired):
    """Overshoot is bounded by detect_every, not by luck.

    A is mashed at ~33/s and the nickname prompt comes right after the
    Pokemon is added, so the only thing standing between this hunt and
    a naming keyboard it cannot escape is how soon detection notices.
    """
    game = wired(Game([Mon(99, shiny=True)], presses_needed=5))
    ctx = Ctx(game)

    run(ctx)

    after = game.presses - 5
    assert after <= 15, (
        f"kept pressing A {after} times after the gift landed -- that "
        f"walks into the nickname keyboard")


def test_nothing_is_held_down(wired):
    """Game.hold raises: a latched direction outlives the bot."""
    game = wired(Game([Mon(99, shiny=True)]))
    run(Ctx(game))


def test_only_a_is_pressed(wired):
    game = wired(Game([Mon(99, shiny=True)]))
    ctx = Ctx(game)
    run(ctx)
    assert set(game.taps) == {"A"}


# ----------------------------------------------------------------------
# Refusing to start on a save that cannot work
# ----------------------------------------------------------------------
def test_a_full_party_is_refused_up_front(wired):
    """A gift given to a full party goes to a PC box, where no party
    read will ever see it."""
    game = Game([Mon(99, shiny=True)])
    game.team = [Mon(i, 25) for i in range(1, 7)]
    wired(game)
    ctx = Ctx(game)

    run(ctx)

    assert "party full" in (ctx.reasons() or [])
    assert game.taps == [], "pressed A at a hunt that cannot work"


def test_an_unreadable_party_is_refused_up_front(wired):
    game = Game([Mon(99)])
    game.team = []
    wired(game)
    ctx = Ctx(game)

    run(ctx)

    assert "no party at start" in (ctx.reasons() or [])
    assert game.taps == []


def test_a_gift_already_taken_is_cleared_by_the_opening_reset(wired):
    """There is no read that spots an already-taken gift.

    It looks exactly like one more party member, so a guard comparing
    against the baseline can never fire -- the baseline was read from
    that same party and has already absorbed it. The first version of
    this mode had exactly that guard, and this test is what showed it
    was dead code.

    So the fix is not a better guard, it is not needing one: reset
    before taking the baseline and the question stops existing.
    """
    game = Game([Mon(50, shiny=True)], presses_needed=3)
    game.held = Mon(99)                  # started with it already taken
    wired(game)
    ctx = Ctx(game)

    run(ctx)

    assert game.resets >= 1, "never reset before baselining"
    hit = [kw for k, kw in ctx.events if k == "target_hit"]
    assert hit, "the hunt never reached a shiny"
    assert hit[0]["species"] == 131
    # The stale one must NOT have been treated as this run's gift.
    assert ctx.kinds().count("candidate") == 1


def test_the_opening_reset_can_be_turned_off(wired):
    game = wired(Game([Mon(99, shiny=True)], presses_needed=3))
    ctx = Ctx(game)
    ctx.config = {"soft_reset": {**FAST, "initial_reset": False}}

    gifts.run(ctx)

    assert not ctx.ran_away
    assert game.resets == 0, "reset despite initial_reset: false"


# ----------------------------------------------------------------------
# A reset that does not take
# ----------------------------------------------------------------------
def test_a_reset_that_does_not_register_stops_the_hunt(wired):
    """Otherwise the same Pokemon is evaluated over and over while the
    hunt looks perfectly busy."""
    game = Game([Mon(10)])

    def dead_reset(hold_s=0.5):
        game.resets += 1          # combo goes out, nothing happens

    game.soft_reset = dead_reset
    wired(game)
    ctx = Ctx(game)

    run(ctx)

    assert "soft reset did not register" in (ctx.reasons() or [])
    assert ctx.kinds().count("candidate") == 1, \
        "evaluated the same gift more than once"


def test_a_gift_that_never_arrives_stops_the_hunt(wired):
    game = wired(Game([], presses_needed=5))
    ctx = Ctx(game)

    run(ctx)

    assert "no gift received from A presses" in (ctx.reasons() or [])


# ----------------------------------------------------------------------
# The team is not the gift
# ----------------------------------------------------------------------
def test_the_existing_team_never_reads_as_an_arrival(wired):
    """The whole difference from the starter hunt. If the team read as
    arrivals, attempt 1 would evaluate a party member and stop without
    ever pressing anything."""
    game = wired(Game([Mon(99, shiny=True)], presses_needed=5))
    ctx = Ctx(game)

    run(ctx)

    hit = [kw for k, kw in ctx.events if k == "target_hit"][0]
    assert hit["species"] == 131, \
        f"stopped on a party member, not the gift: {hit}"
    assert game.presses >= 5, "stopped before pressing anything"
