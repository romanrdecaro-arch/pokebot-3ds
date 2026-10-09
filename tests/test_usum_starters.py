"""
The USUM starter soft reset, as the loop was laid out:

    spam A and tap Left until a starter is in hand (read at the
      nickname screen)
    if shiny, stop; if not shiny, reset
    immediately start spamming A again
    ...repeat

The fake game follows what Ultra Moon was seen to do when driven live
(2026-10-08), so each of these is a way this hunt was found to go wrong:

* the save stands in tall grass, and nothing happens until the player
  steps LEFT -- with the Circle Pad: the D-pad does not walk in USUM,
  which is why "it never did the left press";
* after the choice the starter is written to ``received_slot`` and the
  nickname question comes up; an A there answers it and opens the
  keyboard, where A types letters -- the "087" a real run named its
  Rowlet;
* a reset leaves RAM alone; the reload empties the slot, or with
  ``clears=False`` leaves last attempt's starter sitting there;
* every read is timestamped, so a read inside the relaunch window --
  what crashed Azahar -- is caught.

It holds real PK7 bytes at the Gen 7 addresses, so the mode's own
detection path runs against it unmodified.
"""
from __future__ import annotations

import re
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot import games  # noqa: E402
from pokebot.modes import soft_reset as sr  # noqa: E402
from pokebot.modes import usum_starters as us  # noqa: E402
from test_gen7 import OT, SHINY_PID, mystatus, pk7  # noqa: E402

clock = time.perf_counter

KEY = "USUM-USA-1.2"
OFF = games.GAMES[KEY].offsets
TB = games.LIVEHEX_REFERENCES[KEY]["trainer_block"]
SLOTS = {"received": OFF.received_slot, "live": OFF.party_live,
         "save": OFF.party_base}


class Starter:
    _n = 0

    def __init__(self, shiny=False, species=722):
        Starter._n += 1
        self.key = 0x7700_0000 + Starter._n
        self.shiny = shiny
        self.species = species
        pid = SHINY_PID if shiny else 0x9D1A0000 | (0x1000 + Starter._n)
        self.bytes = pk7(key=self.key, species=species, pid=pid, ot=OT,
                         party=True, level=5)


class Game:
    """A USUM save in Route 1's grass, on the game's own clock.

    After a reset the title boots for ``boot`` seconds. Then nothing
    happens until a CircleLeft step; after it, the ``presses``-th A
    makes the choice, and the starter is written ``delay`` later. The
    nickname question is up ``prompt`` after that.
    """

    def __init__(self, starters, *, presses=3, delay=0.03, boot=0.3,
                 prompt=0.25, where=("received",), clears=True,
                 trainer=OT):
        self.queue = list(starters)
        self.where = [SLOTS[w] for w in where]
        self.presses_needed = presses
        self.delay, self.boot, self.prompt = delay, boot, prompt
        self.clears = clears
        self.mem: dict[int, bytes] = {TB: mystatus(trainer)}
        self.booted_at = 0.0
        self.clear_at = None
        self.stepped = False
        self.presses = 0
        self.receive_at = None
        self.received = None
        self.prompt_at = None
        self.first_read_t = None
        self.inputs: list[tuple[float, str, float]] = []
        self.reads: list[tuple[float, int, int]] = []
        self.resets: list[float] = []
        self.arrivals: list[float] = []
        self.answered = 0
        #: Crash Azahar on this reset (1-based): its window is gone,
        #: and every tap returns "none" until it is relaunched.
        self.crash_on_reset = None
        self.crashed = False
        self.taps_into_nothing = 0

    def relaunch(self):
        """What a fresh Azahar on the ROM looks like: a cold boot."""
        now = clock()
        self.crashed = False
        self.booted_at = now + self.boot
        self.received = self.receive_at = self.prompt_at = None
        self.stepped = False
        self.presses = 0
        for addr in SLOTS.values():
            self.mem.pop(addr, None)

    def _tick(self, now):
        if self.clear_at is not None and now >= self.clear_at:
            self.clear_at = None
            for addr in SLOTS.values():
                self.mem.pop(addr, None)       # the save holds no party
        if (self.received is None and self.receive_at is not None
                and now >= self.receive_at and self.queue):
            self.received = self.queue.pop(0)
            for addr in self.where:
                self.mem[addr] = self.received.bytes
            self.prompt_at = now + self.prompt
            self.first_read_t = None
            self.arrivals.append(now)

    # -- controller -----------------------------------------------------
    def tap(self, button, hold_s=0.05):
        now = clock()
        if self.crashed:
            self.taps_into_nothing += 1
            return "none"
        self._tick(now)
        self.inputs.append((now, button, hold_s))
        booted = now >= self.booted_at
        if self.received is not None:
            if button == "A" and now >= self.prompt_at:
                self.answered += 1             # opens the keyboard
        elif booted and not self.stepped:
            # Standing in the grass. Only the Circle Pad walks.
            self.stepped = button == "CircleLeft"
        elif (booted and button == "A" and self.receive_at is None):
            self.presses += 1
            if self.presses >= self.presses_needed:
                self.receive_at = now + self.delay
        time.sleep(hold_s)
        return "postmessage"

    def hold(self, button):
        raise AssertionError(f"{button} was HELD -- it must be tapped")

    def needs_focus(self):
        return False

    def soft_reset(self, hold_s=0.5):
        now = clock()
        self.inputs.append((now, "RESET", hold_s))
        self.resets.append(now)
        if self.crash_on_reset == len(self.resets):
            self.crashed = True
        self.booted_at = now + self.boot
        self.received = self.receive_at = self.prompt_at = None
        self.stepped = False
        self.presses = 0
        if self.clears:
            self.clear_at = self.booted_at

    # -- memory (ctx.rpc) -----------------------------------------------
    def read(self, addr, n):
        now = clock()
        self._tick(now)
        self.reads.append((now, addr, n))
        if (self.received is not None and self.first_read_t is None
                and any(addr <= a and a + 232 <= addr + n
                        for a in self.where)):
            self.first_read_t = now
        out = bytearray(n)
        for a, d in self.mem.items():
            lo, hi = max(a, addr), min(a + len(d), addr + n)
            if lo < hi:
                out[lo - addr:hi - addr] = d[lo - a:hi - a]
        return bytes(out)

    def buttons_after(self, t):
        return [b for at, b, _ in self.inputs if at > t]


FAST = {"trainer_name": OT, "press_hold": 0.01, "usum_left_hold": 0.02,
        "usum_receive_timeout": 3.0, "usum_scan_gap": 0.02,
        "usum_sweep_every": 0.3, "pre_reset_quiet": 0.05,
        "reload_read_grace": 0.15}


class Ctx:
    def __init__(self, game, cfg=None, target=None, seconds=10.0):
        self.input = game
        self.rpc = game
        self.target = target
        self.game = games.GAMES[KEY]
        self.config = {"soft_reset": dict(FAST if cfg is None else cfg)}
        self._stop_evt = threading.Event()
        self.ran_away = False
        self._deadline = time.monotonic() + seconds
        self.events: list[tuple[str, dict]] = []
        events = self.events

        class Dash:
            @staticmethod
            def broadcast(kind, **kw):
                events.append((kind, kw))

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

    def judged(self):
        return [(kw["species"], kw["pid"]) for k, kw in self.events
                if k == "candidate"]


@pytest.fixture
def wired(monkeypatch):
    """Returns the species of every .pk7 the hunt saved."""
    saved = []
    monkeypatch.setattr(us, "_MIN_GRACE_S", 0.05)
    monkeypatch.setattr(us, "ensure_targets_dir", lambda: None)
    monkeypatch.setattr(us, "save_target_pk6",
                        lambda ctx, a, p, lbl: saved.append(p.species))
    monkeypatch.setattr(sr, "focus_azahar", lambda: None)
    # No test may look for, or relaunch, the real emulator.
    monkeypatch.setattr(us.Recovery, "for_hunt",
                        classmethod(lambda cls, ctx: None))

    import pokebot.modes.catch as catch_mod

    def no_catch(*a, **k):
        raise AssertionError("a catch sequence ran -- it must not")

    monkeypatch.setattr(catch_mod, "catch_wild", no_catch)
    return saved


def run(ctx):
    us.run(ctx)
    assert not ctx.ran_away, "the hunt never stopped on its own"


def shipped_soft_reset() -> dict:
    import yaml

    with open(REPO / "config.yaml", encoding="utf-8") as f:
        return dict(yaml.safe_load(f)["soft_reset"])


# ----------------------------------------------------------------------
# If shiny, stop -- all input, for good
# ----------------------------------------------------------------------
def test_a_shiny_starter_stops_the_hunt(wired):
    game = Game([Starter(shiny=True)])
    ctx = Ctx(game)
    run(ctx)

    assert "target_hit" in ctx.kinds()
    assert game.resets == [], "reset away a shiny starter"
    assert wired == [722], "the shiny's .pk7 was not saved"


def test_no_input_of_any_kind_once_the_shiny_has_been_read(wired):
    game = Game([Starter(), Starter(), Starter(shiny=True)])
    run(Ctx(game))

    assert game.first_read_t is not None
    after = game.buttons_after(game.first_read_t)
    assert after == [], f"sent {after} after the shiny was read"


def test_the_nickname_question_is_never_answered(wired):
    """The read happens at the nickname screen: an A there would open
    the keyboard, and the next ones would type the shiny's name."""
    game = Game([Starter() for _ in range(4)] + [Starter(shiny=True)],
                prompt=0.15)
    run(Ctx(game, seconds=20.0))
    assert len(game.resets) == 4
    assert game.answered == 0, f"{game.answered} A press(es) answered it"


# ----------------------------------------------------------------------
# Left: the Circle Pad, tapped, a little longer than A
# ----------------------------------------------------------------------
def test_the_dpad_never_starts_the_event(wired):
    """What a live run found: D-pad Left held 0.6 s did not move the
    player, so the starter never came."""
    game = Game([Starter(shiny=True)])
    ctx = Ctx(game, cfg={**FAST, "usum_left_button": "DpadLeft",
                         "usum_receive_timeout": 1.0})
    run(ctx)
    assert "target_hit" not in ctx.kinds()
    assert ctx.kinds().count("read_failure") == 3


def test_left_is_the_circle_pad_held_longer_than_a():
    plan = us.StarterPlan()
    assert plan.left_button == "CircleLeft"
    assert plan.left_hold >= 0.1 > plan.press_hold


def test_the_stream_alternates_a_and_left_through_the_whole_boot(wired):
    game = Game([Starter(), Starter(shiny=True)], boot=0.4)
    run(Ctx(game))

    r, arrival = game.resets[0], game.arrivals[1]
    stream = [(b, h) for t, b, h in game.inputs if r < t < arrival]
    assert len(stream) >= 15, f"only {len(stream)} presses through the boot"
    assert [b for b, _ in stream[:6]] == ["A", "CircleLeft"] * 3
    holds = {b: h for b, h in stream}
    assert holds["CircleLeft"] == FAST["usum_left_hold"]
    assert holds["A"] == FAST["press_hold"]


# ----------------------------------------------------------------------
# If not shiny, reset -- and immediately spam A and Left again
# ----------------------------------------------------------------------
def test_every_plain_starter_is_reset_and_judged_once(wired):
    starters = [Starter(), Starter(species=725), Starter(species=728),
                Starter(shiny=True)]
    game = Game(starters)
    ctx = Ctx(game)
    run(ctx)

    assert len(game.resets) == 3
    assert [sp for sp, _ in ctx.judged()] == [722, 725, 728, 722]


def test_presses_resume_straight_after_each_reset_with_the_shipped_config(
        wired):
    """config.yaml still ships the X/Y targets' post-reset wait and
    taps; this mode must not read them."""
    shipped = shipped_soft_reset()
    assert float(shipped["post_reset_wait"]) >= 10, "precondition"
    speed = {k: v for k, v in FAST.items() if k != "trainer_name"}
    game = Game([Starter(), Starter(), Starter(shiny=True)])
    run(Ctx(game, cfg={**shipped, **speed}))

    assert len(game.resets) == 2
    for r in game.resets:
        first = min(t for t, b, _ in game.inputs
                    if b in ("A", "CircleLeft") and t > r)
        assert first - r < 0.1, f"first press {first - r:.2f}s after reset"


def test_nothing_is_read_while_azahar_relaunches(wired):
    game = Game([Starter(), Starter(), Starter(shiny=True)])
    run(Ctx(game))

    quiet, grace, eps = FAST["pre_reset_quiet"], FAST["reload_read_grace"], 0.03
    assert len(game.resets) == 2
    for r in game.resets:
        bad = [t - r for t, _, _ in game.reads
               if r - quiet + eps < t < r + grace - eps]
        assert not bad, f"read at {bad[0]:+.3f}s from a reset"


def test_a_starter_left_in_memory_is_not_judged_again(wired):
    """A reset leaves RAM alone; if the reload does not clear the slot
    either, last attempt's starter sits exactly where the check reads."""
    starters = [Starter(), Starter(), Starter(shiny=True)]
    game = Game(starters, clears=False, boot=0.4)
    ctx = Ctx(game)
    run(ctx)

    grace = FAST["reload_read_grace"]
    looked = [t for t, a, n in game.reads
              if a == SLOTS["received"] and n == 232
              and game.resets[0] + grace < t < game.arrivals[1]]
    assert looked, "precondition: the stale slot was read after the reset"
    assert len(ctx.judged()) == 3
    assert len(game.resets) == 2


def test_the_live_party_copy_is_watched_too(wired):
    game = Game([Starter(), Starter(shiny=True)], where=("live",))
    ctx = Ctx(game)
    run(ctx)
    assert len(game.resets) == 1
    assert "target_hit" in ctx.kinds() and wired == [722]


def test_once_the_slot_is_known_a_check_is_one_read(wired):
    game = Game([Starter() for _ in range(3)] + [Starter(shiny=True)])
    run(Ctx(game))

    slot_reads = sum(1 for _, a, n in game.reads if n == 232)
    scans = sum(1 for _, a, n in game.reads if n > 232 and a != TB)
    assert slot_reads > 2 * scans, f"{slot_reads} slot reads, {scans} scans"


def test_reads_stay_inside_the_watched_windows(wired):
    """Gen 7's heap is 256 MB; nothing may wander into it -- and the
    save-block party, which did not change live, is not read at all."""
    game = Game([Starter(), Starter(shiny=True)])
    run(Ctx(game))

    allowed = us.watch_windows(OFF) + [(TB, TB + 0xC0)]
    stray = [(hex(a), n) for _, a, n in game.reads
             if not any(lo <= a and a + n <= hi for lo, hi in allowed)]
    assert stray == []


def test_a_record_without_the_players_ot_is_never_the_starter():
    """The choice's three on-screen Pokemon, or any other record that
    lands in the slot, are not yours until one is chosen."""
    game = Game([])
    ctx = Ctx(game)
    watch = us.StarterWatch(ctx, us.watch_windows(OFF), OT, set(),
                            us.StarterPlan.from_config(FAST))
    watch.hot = SLOTS["received"]
    for ot in ("", "Hala"):
        game.mem[SLOTS["received"]] = pk7(key=0xCAFE, species=725, ot=ot,
                                          party=True, level=5)
        watch._next_scan = 0.0
        assert watch.check(time.monotonic()) is None, f"OT {ot!r} counted"


# ----------------------------------------------------------------------
# Azahar crashing in its own relaunch (~1 in 650-900 resets)
# ----------------------------------------------------------------------
class FakeRecovery:
    def __init__(self, game, ok=True, dialog_on_miss=False):
        self.game, self.ok, self.revives = game, ok, 0
        self.dialog_on_miss = dialog_on_miss
        self.dismissed = 0

    def dismiss_dialogs(self):
        if self.dialog_on_miss and not self.dismissed:
            self.dismissed += 1
            return ["Update Available"]
        return []

    def revive(self, ctx):
        self.revives += 1
        if self.ok:
            self.game.relaunch()
        return self.ok


def with_recovery(monkeypatch, recovery):
    monkeypatch.setattr(us.Recovery, "for_hunt",
                        classmethod(lambda cls, ctx: recovery))


def test_a_crash_is_survived_and_the_hunt_goes_on(wired, monkeypatch):
    game = Game([Starter(), Starter(), Starter(shiny=True)])
    game.crash_on_reset = 1
    rec = FakeRecovery(game)
    with_recovery(monkeypatch, rec)
    ctx = Ctx(game)
    run(ctx)

    assert rec.revives == 1
    assert "target_hit" in ctx.kinds(), "the hunt did not carry on"
    assert game.taps_into_nothing == 1, (
        f"{game.taps_into_nothing} taps into a dead emulator; the first "
        f"one is the probe that notices")


def test_the_relaunch_is_the_reset(wired, monkeypatch):
    """A cold boot already puts the game back at the save; sending
    L+R+Start on top of it would only cost another relaunch."""
    game = Game([Starter(), Starter(shiny=True)])
    game.crash_on_reset = 1
    with_recovery(monkeypatch, FakeRecovery(game))
    run(Ctx(game))
    assert len(game.resets) == 1, "reset again straight after relaunching"


def test_with_no_recovery_a_crash_stops_the_hunt_at_once(wired):
    """Not three 180 s timeouts of pressing into nothing."""
    game = Game([Starter(), Starter(shiny=True)])
    game.crash_on_reset = 1
    ctx = Ctx(game, cfg={**FAST, "usum_receive_timeout": 60.0})
    t0 = time.monotonic()
    run(ctx)

    assert time.monotonic() - t0 < 5.0
    assert ("read_failure", "Azahar crashed") in [
        (k, kw.get("reason")) for k, kw in ctx.events]
    assert game.taps_into_nothing == 1


def test_a_dialog_found_on_a_miss_is_answered_not_counted(wired,
                                                          monkeypatch):
    """A blocked window looks exactly like "no starter came". A miss
    that turned out to be a dialog must not count towards stopping:
    here a real miss follows it, and two counted misses would end the
    hunt before the shiny on the third attempt."""
    game = Game([Starter(shiny=True)], presses=10 ** 9)
    rec = FakeRecovery(game, dialog_on_miss=True)
    with_recovery(monkeypatch, rec)
    real = game.soft_reset

    def soft_reset(hold_s=0.5):
        real(hold_s)
        if len(game.resets) == 2:
            game.presses_needed = 2           # the third attempt works

    game.soft_reset = soft_reset                   # type: ignore
    ctx = Ctx(game, cfg={**FAST, "usum_receive_timeout": 1.0,
                         "usum_max_misses": 2})
    run(ctx)
    assert rec.dismissed == 1
    assert "target_hit" in ctx.kinds(), "the dialog's miss was counted"


def test_a_relaunch_that_fails_stops_the_hunt(wired, monkeypatch):
    game = Game([Starter(), Starter(shiny=True)])
    game.crash_on_reset = 1
    rec = FakeRecovery(game, ok=False)
    with_recovery(monkeypatch, rec)
    ctx = Ctx(game)
    run(ctx)
    assert rec.revives == 1 and "target_hit" not in ctx.kinds()


# ----------------------------------------------------------------------
# Start-up, misses
# ----------------------------------------------------------------------
def test_a_shiny_already_in_hand_at_start_stops_before_any_press(wired):
    """It is the game's real state: judge it, and touch nothing."""
    shiny = Starter(shiny=True)
    game = Game([])
    game.mem[SLOTS["received"]] = shiny.bytes
    ctx = Ctx(game)
    run(ctx)

    assert "target_hit" in ctx.kinds()
    assert game.inputs == [], "pressed something at a shiny"


def test_a_plain_starter_already_in_hand_at_start_is_judged_then_reset(
        wired):
    game = Game([Starter(shiny=True)])
    game.mem[SLOTS["received"]] = Starter().bytes
    ctx = Ctx(game)
    run(ctx)
    assert len(ctx.judged()) == 2 and len(game.resets) == 1


def test_no_starter_resets_and_tries_again(wired):
    game = Game([Starter(shiny=True)], presses=10 ** 9)
    ctx = Ctx(game, cfg={**FAST, "usum_receive_timeout": 1.0})
    real = game.soft_reset

    def soft_reset(hold_s=0.5):
        real(hold_s)
        game.presses_needed = 2          # works after the reset

    game.soft_reset = soft_reset                   # type: ignore
    run(ctx)

    assert len(game.resets) == 1
    assert "read_failure" in ctx.kinds() and "target_hit" in ctx.kinds()


def test_three_misses_in_a_row_stop_the_hunt(wired):
    game = Game([Starter(shiny=True)], presses=10 ** 9)
    ctx = Ctx(game, cfg={**FAST, "usum_receive_timeout": 1.0})
    run(ctx)

    assert ctx.kinds().count("read_failure") == 3
    assert len(game.resets) == 2, "should stop on the third miss, not reset"


def test_the_games_own_trainer_name_wins_over_config(wired):
    """The starter is matched by OT. A config name that differs from the
    game's would make every starter invisible."""
    game = Game([Starter(), Starter(shiny=True)])
    run(Ctx(game, cfg={**FAST, "trainer_name": "Ash"}))
    assert len(game.resets) == 1


def test_the_shiny_is_saved_as_a_pk7(tmp_path, monkeypatch):
    import pokebot.pk6_export as pk6_export

    monkeypatch.setattr(us, "_MIN_GRACE_S", 0.05)
    monkeypatch.setattr(sr, "focus_azahar", lambda: None)
    monkeypatch.setattr(us, "ensure_targets_dir", lambda: None)
    monkeypatch.setattr(pk6_export, "TARGETS_DIR", tmp_path)
    monkeypatch.setattr(pk6_export, "ensure_targets_dir", lambda: tmp_path)
    shiny = Starter(shiny=True)
    run(Ctx(Game([shiny])))

    files = list(tmp_path.iterdir())
    assert [f.suffix for f in files] == [".pk7"]
    assert files[0].read_bytes() == shiny.bytes[:232]


# ----------------------------------------------------------------------
# Plan, registry, launcher
# ----------------------------------------------------------------------
def test_the_plan_takes_the_launchers_press_speed():
    """--press-speed writes soft_reset.press_hold."""
    plan = us.StarterPlan.from_config({"press_hold": 0.02})
    assert plan.press_hold == 0.02


def test_the_relaunch_silences_come_from_the_shared_keys_with_floors():
    shipped = shipped_soft_reset()
    plan = us.StarterPlan.from_config(shipped)
    assert plan.reload_grace == float(shipped["reload_read_grace"])
    assert plan.pre_reset_quiet == float(shipped["pre_reset_quiet"])
    zeroed = us.StarterPlan.from_config({"pre_reset_quiet": 0,
                                         "reload_read_grace": 0})
    assert zeroed.pre_reset_quiet >= 0.05 and zeroed.reload_grace >= 0.5


def test_left_every_sets_the_pattern():
    assert [us.StarterPlan(left_every=2).button(i) for i in range(6)] == [
        "A", "A", "CircleLeft", "A", "A", "CircleLeft"]
    assert {us.StarterPlan(left_every=0).button(i) for i in range(6)} == {
        "A"}


def test_the_received_slot_is_the_one_found_live():
    assert OFF.received_slot == 0x329C2C74
    assert games.GAMES["SM-USA-1.2"].offsets.received_slot == 0


def test_usum_offers_the_starter_hunt_and_sm_does_not():
    assert [m.mode for m in games.methods_for(KEY)] == [
        "observe", "usum_starters"]
    assert [m.mode for m in games.methods_for("SM-USA-1.2")] == ["observe"]


def test_the_mode_is_registered():
    from pokebot.modes import MODES

    assert MODES["usum_starters"] is us.run


def test_the_launcher_passes_the_trainer_name_and_press_speed():
    src = (REPO / "launcher.py").read_text(encoding="utf-8")
    m = re.search(r"if method\.mode in \(([^)]*)\):\s*\n\s*tn = "
                  r"self\._trainer_var", src)
    assert m and "usum_starters" in re.findall(r'"(\w+)"', m.group(1))
