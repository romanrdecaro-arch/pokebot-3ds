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

    def __init__(self, gifts_in_order, presses_needed=5, boxed=80,
                 gift_outside_window=False):
        self.team = [Mon(1, 25), Mon(2, 4)]
        # True models a gift written somewhere the cached window does
        # not cover -- only a broad re-locating sweep finds it.
        self.gift_outside_window = gift_outside_window
        self.windowed_reads = 0
        self.broad_reads = 0
        # The PC boxes. A broad scan sees these too -- they are owned
        # PK6 in the same window -- which is the whole reason a broad
        # read cannot be used to count party slots.
        self.boxes = [Mon(1000 + i, 16) for i in range(boxed)]
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
    def party(self, contiguous=True, broad=False):
        """A cached/tight read sees only the save-block team.

        ``broad`` is a read that re-located first -- get_party with no
        cached window. It sees everything; a windowed one sees only
        what was near the party when the window was anchored, which
        was before the gift existed.

        A gift can land in a live buffer outside the window the cache
        was anchored on, so a contiguous read misses it -- and misses
        it silently, looking exactly like "not arrived yet". Modelled
        here so a read that trusts the cache fails the tests rather
        than passing them.
        """
        if contiguous:
            return list(self.team)
        out = list(self.team) + list(self.boxes)
        if self.held and (broad or not self.gift_outside_window):
            out.append(self.held)
        return out


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
        def fake_get_party(ctx, b, st, ot, contiguous=True):
            # Mirror get_party's caching: no window cached means it
            # relocates (and sees everything), then caches one.
            broad = getattr(ctx, "_party_win", None) is None
            if broad:
                game.broad_reads += 1
                ctx._party_win = (0x08C00000, 0x08C90000)
            else:
                game.windowed_reads += 1
            return game.party(contiguous, broad=broad)

        monkeypatch.setattr(gifts, "get_party", fake_get_party)
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


# ----------------------------------------------------------------------
# Boxes are not party slots
# ----------------------------------------------------------------------
def test_a_full_pc_box_is_not_a_full_party(wired):
    """Reported from a real run: 82 Pokemon "in the party".

    The broad read that finds an off-grid gift returns every owned PK6
    in the window -- the team AND the boxes. Counting party slots with
    it means anyone who has played the game for an hour is told their
    party is full, and the hunt stops before pressing anything.
    """
    game = wired(Game([Mon(99, shiny=True)], presses_needed=3, boxed=80))
    ctx = Ctx(game)

    run(ctx)

    assert "party full" not in (ctx.reasons() or []), (
        "counted boxed Pokemon as party members")
    assert "target_hit" in ctx.kinds()


def test_a_genuinely_full_party_is_still_refused(wired):
    """The other half: the check has to keep working."""
    game = Game([Mon(99, shiny=True)], boxed=80)
    game.team = [Mon(i, 25) for i in range(1, 7)]
    wired(game)
    ctx = Ctx(game)

    run(ctx)

    assert "party full" in (ctx.reasons() or [])
    assert game.taps == []


def test_the_gift_is_still_found_past_a_boxful(wired):
    """And the arrival check must still see a gift that lands off the
    save-block grid, which is why the broad read exists at all."""
    game = wired(Game([Mon(99, shiny=True)], presses_needed=3, boxed=80))
    ctx = Ctx(game)

    run(ctx)

    hit = [kw for k, kw in ctx.events if k == "target_hit"]
    assert hit and hit[0]["species"] == 131


# ----------------------------------------------------------------------
# What a poll is allowed to cost
# ----------------------------------------------------------------------
def test_the_poll_does_not_force_a_full_relocation_scan(monkeypatch):
    """get_party caches a tight window around the party and re-scans
    only that. Clearing the cache sends the next read through the full
    15 MB relocation scan instead -- thousands of 1 KB RPC round trips.

    Once per attempt is fine. Every detect_every while mashing A is
    not: the poll would take longer than the interval it is polling
    on, and the A presses stop while it runs.
    """
    game = Game([Mon(99, shiny=True)], presses_needed=40, boxed=80)
    cleared = {"n": 0}

    class Ctx2(Ctx):
        pass

    ctx = Ctx2(game)

    class Win:
        def __get__(self, obj, cls=None):
            return None

    monkeypatch.setattr(gifts, "ensure_targets_dir", lambda: None)
    monkeypatch.setattr(gifts, "_focus_if_needed", lambda c: None)
    monkeypatch.setattr(sr, "_focus_if_needed", lambda c: None)
    monkeypatch.setattr(gifts, "broadcast_party", lambda c, p: set())
    monkeypatch.setattr(gifts, "save_target_pk6",
                        lambda c, a, pk, lbl: None)

    reads = {"n": 0}

    def counting_get_party(c, b, st, ot, contiguous=True):
        reads["n"] += 1
        return game.party(contiguous)

    monkeypatch.setattr(gifts, "get_party", counting_get_party)

    # Count how often the cached window is dropped.
    class Tracker(dict):
        pass

    orig_setattr = Ctx2.__setattr__

    def tracking_setattr(self, name, value):
        if name == "_party_win" and value is None:
            cleared["n"] += 1
        orig_setattr(self, name, value)

    Ctx2.__setattr__ = tracking_setattr
    try:
        ctx._party_win = (0, 1)          # pretend a window is cached
        cleared["n"] = 0
        run(ctx)
    finally:
        Ctx2.__setattr__ = orig_setattr

    polls = reads["n"]
    assert polls > 10, "the hunt barely read anything; test is vacuous"
    assert cleared["n"] <= 6, (
        f"cleared the party-window cache {cleared['n']} times across "
        f"{polls} reads -- the poll is forcing a relocation scan")


# ----------------------------------------------------------------------
# A gift the cached window cannot see
# ----------------------------------------------------------------------
def test_a_gift_outside_the_cached_window_is_still_found(wired):
    """Reported from a real run: it pressed A and evaluated nothing.

    get_party caches a tight window around the owned cluster -- which
    was anchored BEFORE the gift existed. Polling only that window is
    cheap and blind: a gift written outside it is missed for the whole
    attempt, and the mode then reports that nothing ever arrived,
    which is indistinguishable from a save in the wrong place.

    So the poll stays cheap but a broad sweep runs every sweep_every
    polls, which bounds the blind spot instead of letting it last.
    """
    game = wired(Game([Mon(99, shiny=True)], presses_needed=3,
                      boxed=80, gift_outside_window=True))
    ctx = Ctx(game, seconds=20.0)

    run(ctx)

    assert "target_hit" in ctx.kinds(), (
        "never saw a gift that only a relocating scan can find")
    assert game.broad_reads > 1, "never swept"


def test_the_sweep_is_not_run_on_every_poll(wired):
    """The other half. Every poll relocating means every poll pays for
    a 15 MB scan, and the A presses stop while it runs."""
    game = wired(Game([Mon(99, shiny=True)], presses_needed=60,
                      boxed=80))
    ctx = Ctx(game, seconds=20.0)

    run(ctx)

    assert game.windowed_reads > game.broad_reads, (
        f"{game.broad_reads} relocating reads vs "
        f"{game.windowed_reads} cheap ones -- the poll is paying for a "
        f"full scan almost every time")


def test_a_timeout_reports_what_it_actually_saw(wired):
    """"Nothing arrived" has several very different causes, and the
    numbers tell them apart instantly where the sentence cannot."""
    import logging

    game = wired(Game([], presses_needed=5, boxed=80))
    ctx = Ctx(game)

    records = []
    handler = logging.Handler()
    handler.emit = lambda r: records.append(r.getMessage())
    log = logging.getLogger("pokebot.modes.gifts")
    log.addHandler(handler)
    try:
        run(ctx)
    finally:
        log.removeHandler(handler)

    blob = " ".join(records)
    assert "owned PK6" in blob, "did not report what the scan saw"
    assert "OT" in blob, "did not mention the OT trap"
