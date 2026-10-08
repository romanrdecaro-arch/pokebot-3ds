"""
The ORAS static-encounter soft reset, as the loop was laid out:

    spam A until an encounter is detected
    if shiny, stop; if not shiny, reset
    immediately start spamming A until an encounter
    ...repeat

The fake game runs on its OWN clock (perf_counter) and holds real PK6
bytes, so the mode's actual detection path -- FoeWatch, scan_nonparty,
read_pk6_at, the record decoder -- runs against it unmodified. Only
the party read and the report plumbing are stubbed.

What it models, because each is a way this hunt goes wrong:

* a reset does NOT clear memory -- last attempt's wild is still in the
  slot afterwards, exactly where the cheap check looks;
* after a reset the title boots for a while, and A does nothing then;
* the battle MENU comes up a moment after the wild is written, and an
  A from then on is "Fight" -- the one press this hunt must never make;
* every read is timestamped, so a read inside the window in which
  reading crashed Azahar is caught.
"""
from __future__ import annotations

import logging
import struct
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot.modes import observe  # noqa: E402
from pokebot.modes import soft_reset as sr  # noqa: E402
from pokebot.modes import static_encounter as se  # noqa: E402
from pokebot.parser import calc_checksum  # noqa: E402

clock = time.perf_counter

FOE = 0x08800000
FOE_LEN = 0x20000
SLOT = FOE + 0x3F50            # where the live Omega Ruby run found Reshiram
TID = 0x1D1C                   # the player's; a wild in battle carries it

PARTY = [SimpleNamespace(encryption_key=0x0BADF00D, species=260)]


def record(*, key: int, pid: int, species: int = 643) -> bytes:
    """A plaintext 232-byte PK6 with a correct checksum."""
    d = bytearray(232)
    struct.pack_into("<I", d, 0x00, key)
    struct.pack_into("<H", d, 0x08, species)
    struct.pack_into("<H", d, 0x0C, TID)
    struct.pack_into("<I", d, 0x18, pid)
    struct.pack_into("<H", d, 0x06, calc_checksum(bytes(d)))
    return bytes(d)


class Mon:
    _n = 0

    def __init__(self, shiny: bool = False, species: int = 643):
        Mon._n += 1
        n = Mon._n
        self.key = 0x70000000 + n
        self.shiny = shiny
        # PSV == TSV for the shiny; far apart for the rest.
        pid = ((TID << 16) | (n & 0xF) if shiny
               else 0x9D1A0000 | (0x1000 + (n & 0xFFF)))
        self.bytes = record(key=self.key, pid=pid, species=species)


class Game:
    """A save in front of a legendary, on the game's own clock.

    After a reset the title boots for ``boot`` seconds. The
    ``presses``-th A after that starts the battle, and the wild is
    written to its slot ``delay`` later. The battle menu is up
    ``intro`` after that.
    """

    def __init__(self, encounters, *, presses=2, delay=0.03, boot=0.3,
                 intro=0.25, slots=None, at_battle=()):
        self.queue = list(encounters)
        self.slots = list(slots or ())
        self.at_battle = list(at_battle)      # (addr, bytes) per battle
        self.mem = bytearray(FOE_LEN)
        self.presses_needed = presses
        self.delay, self.boot, self.intro = delay, boot, intro
        self.booted_at = 0.0                  # starts in the overworld
        self.presses = 0
        self.spawn_at = None
        self.battle = None
        self.battle_slot = None
        self.menu_at = None
        self.first_read_t = None              # of the current battle
        self.inputs: list[tuple[float, str]] = []
        self.reads: list[tuple[float, int, int]] = []
        self.resets: list[float] = []
        self.spawns: list[float] = []
        self.fight = 0

    def put(self, addr, data):
        off = addr - FOE
        self.mem[off:off + len(data)] = data

    def _tick(self, now):
        if (self.battle is None and self.spawn_at is not None
                and now >= self.spawn_at and self.queue):
            self.battle = self.queue.pop(0)
            self.battle_slot = self.slots.pop(0) if self.slots else SLOT
            for addr, data in self.at_battle:
                self.put(addr, data)
            self.put(self.battle_slot, self.battle.bytes)
            self.menu_at = now + self.intro
            self.first_read_t = None
            self.spawns.append(now)

    # -- controller -----------------------------------------------------
    def tap(self, button, hold_s=0.05):
        now = clock()
        self._tick(now)
        self.inputs.append((now, button))
        if button == "A":
            if self.battle is not None:
                if now >= self.menu_at:
                    self.fight += 1
            elif self.spawn_at is None and now >= self.booted_at:
                self.presses += 1
                if self.presses >= self.presses_needed:
                    self.spawn_at = now + self.delay
        time.sleep(hold_s)
        return "postmessage"

    def tap_touch(self, *a, **k):
        self.inputs.append((clock(), "TOUCH"))
        return True

    def needs_focus(self):
        return False

    def soft_reset(self, hold_s=0.5):
        now = clock()
        self.inputs.append((now, "RESET"))
        self.resets.append(now)
        self.booted_at = now + self.boot
        self.battle = self.battle_slot = self.spawn_at = None
        self.menu_at = None
        self.presses = 0
        # Memory is deliberately left alone, as on the console.

    # -- memory (ctx.rpc) -----------------------------------------------
    def read(self, addr, n):
        now = clock()
        self._tick(now)
        self.reads.append((now, addr, n))
        if (self.battle is not None and self.first_read_t is None
                and addr <= self.battle_slot
                and self.battle_slot + 232 <= addr + n):
            self.first_read_t = now
        off = addr - FOE
        if 0 <= off and off + n <= FOE_LEN:
            return bytes(self.mem[off:off + n])
        return bytes(n)


#: Only what makes the test fast. Everything else is the mode's default.
FAST = {"trainer_name": "Roman", "static_press_hold": 0.01,
        "static_timeout": 3.0, "static_settle": 0.02,
        "pre_reset_quiet": 0.05, "reload_read_grace": 0.15}


class Ctx:
    def __init__(self, game, cfg=None, target=None, seconds=10.0):
        self.input = game
        self.rpc = game
        self.target = target
        self.config = {"soft_reset": dict(FAST if cfg is None else cfg)}
        self._stop_evt = threading.Event()
        self.events: list[tuple] = []
        self.ran_away = False
        self._deadline = time.monotonic() + seconds

        class Off:
            foe_base = FOE
            foe_scan_len = FOE_LEN
            party_base = 0x08C814AC
            party_stride = 260

        class G:
            offsets = Off()
            key = "OR-USA"

        self.game = G()
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


def shipped_soft_reset() -> dict:
    import yaml
    with open(REPO / "config.yaml", encoding="utf-8") as f:
        return dict(yaml.safe_load(f)["soft_reset"])


class Reported(list):
    """Every Pokemon the hunt reported, in order -- plus how many times
    it read the party."""
    party_reads: int = 0


@pytest.fixture
def wired(monkeypatch):
    reported = Reported()
    # The test graces are shorter than the real floor; a test below
    # checks the floor itself without this.
    monkeypatch.setattr(se, "_MIN_GRACE_S", 0.05)
    monkeypatch.setattr(se, "ensure_targets_dir", lambda: None)
    monkeypatch.setattr(sr, "focus_azahar", lambda: None)

    def get_party(*a, **k):
        reported.party_reads += 1
        return list(PARTY)

    monkeypatch.setattr(se, "get_party", get_party)
    monkeypatch.setattr(se, "broadcast_party",
                        lambda ctx, p: {m.encryption_key for m in p})
    monkeypatch.setattr(se, "_report_encounter",
                        lambda ctx, pkm, addr, n, via: reported.append(pkm))

    import pokebot.modes.catch as catch_mod

    def no_catch(*a, **k):
        raise AssertionError("the catch sequence ran -- it must not")

    monkeypatch.setattr(catch_mod, "catch_wild", no_catch)
    return reported


def run(ctx):
    se.run(ctx)
    assert not ctx.ran_away, "the hunt never stopped on its own"


def keys(reported):
    return [p.encryption_key for p in reported]


def test_the_fake_records_decode_as_intended():
    """Pins the fixtures. If these drift, everything below is vacuous."""
    s, p = Mon(shiny=True), Mon()
    ds, dp = observe._decode(s.bytes), observe._decode(p.bytes)
    assert ds is not None and ds.shiny and ds.encryption_key == s.key
    assert dp is not None and not dp.shiny and dp.encryption_key == p.key
    assert not (ds.ot_name or ""), "a wild has no OT"


# ----------------------------------------------------------------------
# If shiny, stop -- all input, for good
# ----------------------------------------------------------------------
def test_a_shiny_stops_the_hunt(wired):
    game = Game([Mon(shiny=True)])
    ctx = Ctx(game)
    run(ctx)

    assert "target_hit" in ctx.kinds()
    assert game.resets == [], "reset away a shiny"


def test_no_input_of_any_kind_once_the_shiny_has_been_read(wired):
    """"Stop all actions": not an A, not a reset, not a touch."""
    game = Game([Mon(), Mon(), Mon(shiny=True)])
    run(Ctx(game))

    assert game.first_read_t is not None
    after = [b for t, b in game.inputs if t > game.first_read_t]
    assert after == [], f"sent {after} after the shiny was read"


def test_there_is_no_catch_sequence(wired):
    """catch_wild raises in this fixture; reaching it fails the run."""
    game = Game([Mon(), Mon(shiny=True)])
    run(Ctx(game))
    assert {b for _, b in game.inputs} <= {"A", "RESET"}


def test_the_shiny_is_saved_to_targets_exactly_once(wired, monkeypatch):
    """Through the real reporter. The previous version saved it, then
    saved it again."""
    import pokebot.pk6_export as pk6_export

    saved = []
    monkeypatch.setattr(se, "_report_encounter", observe._report_encounter)
    monkeypatch.setattr(pk6_export, "save_target_pk6",
                        lambda ctx, a, p, lbl: saved.append(p.species))
    game = Game([Mon(shiny=True, species=381)])
    run(Ctx(game))
    assert saved == [381]


def test_a_target_filter_match_stops_too(wired):
    class Want:
        rules = [object()]

        def matches(self, p):
            return p.species == 384

        def describe(self, p):
            return "Rayquaza"

    game = Game([Mon(), Mon(species=384)])
    run(Ctx(game, target=Want()))
    assert len(game.resets) == 1


# ----------------------------------------------------------------------
# If not shiny, reset -- and immediately spam A again
# ----------------------------------------------------------------------
def test_every_plain_encounter_is_reset_and_reported_once(wired):
    mons = [Mon(), Mon(), Mon(), Mon(shiny=True)]
    game = Game(mons)
    run(Ctx(game))

    assert len(game.resets) == 3
    assert keys(wired) == [m.key for m in mons]


def test_a_is_pressed_straight_after_each_reset_with_the_shipped_config(
        wired):
    """The bug report: 19 s of near-idle after every reset. config.yaml
    still ships the X/Y targets' post-reset wait and taps; this mode
    must not read them."""
    shipped = shipped_soft_reset()
    assert float(shipped["post_reset_wait"]) >= 10, "precondition"
    assert int(shipped["post_reset_taps"]) >= 5, "precondition"
    speed = {k: v for k, v in FAST.items() if k != "trainer_name"}
    game = Game([Mon(), Mon(), Mon(shiny=True)])
    run(Ctx(game, cfg={**shipped, **speed}))

    assert len(game.resets) == 2
    for r in game.resets:
        first = min(t for t, b in game.inputs if b == "A" and t > r)
        assert first - r < 0.1, f"first A {first - r:.2f}s after a reset"


def test_a_is_spammed_through_the_whole_boot(wired):
    """Not a few taps: a continuous stream, from the reset to the
    battle, through the stretch where nothing may be read."""
    game = Game([Mon(), Mon(shiny=True)], boot=0.4)
    run(Ctx(game))

    r, battle = game.resets[0], game.spawns[1]
    a = [t for t, b in game.inputs if b == "A" and r < t < battle]
    gaps = [y - x for x, y in zip(a, a[1:])]
    assert len(a) >= 15, f"only {len(a)} presses through the boot"
    assert max(gaps) < 0.1, f"a {max(gaps):.2f}s gap in the presses"


def test_nothing_is_read_while_azahar_relaunches(wired):
    """No read in the quiet before L+R+Start, none in the grace after
    it. A read there is what crashed Azahar."""
    game = Game([Mon(), Mon(), Mon(shiny=True)])
    run(Ctx(game))

    quiet, grace, eps = FAST["pre_reset_quiet"], FAST["reload_read_grace"], 0.03
    assert len(game.resets) == 2
    for r in game.resets:
        bad = [t - r for t, _, _ in game.reads
               if r - quiet + eps < t < r + grace - eps]
        assert not bad, f"read at {bad[0]:+.3f}s from a reset"


def test_the_last_wild_left_in_memory_is_not_the_next_encounter(wired):
    """A reset leaves RAM alone: while the title boots, the old wild is
    still in the slot the cheap check reads."""
    mons = [Mon(), Mon(), Mon(shiny=True)]
    game = Game(mons, boot=0.4)
    run(Ctx(game))

    grace = FAST["reload_read_grace"]
    looked = [t for t, a, n in game.reads
              if a == SLOT and n == 232
              and game.resets[0] + grace < t < game.spawns[1]]
    assert looked, "precondition: the stale slot was read during the boot"
    assert keys(wired) == [m.key for m in mons]


def test_a_leftover_shiny_from_before_the_hunt_is_ignored(wired):
    stale = Mon(shiny=True)
    game = Game([Mon(), Mon(shiny=True)])
    game.put(SLOT, stale.bytes)
    run(Ctx(game))

    assert stale.key not in keys(wired), "stopped on the leftover shiny"
    assert len(game.resets) == 1


def test_the_players_own_battle_copy_is_never_the_wild(wired):
    """Written when each battle starts, above the wild slot -- and
    shiny, so mistaking it for the wild would stop the hunt on it."""
    own = record(key=PARTY[0].encryption_key, pid=(TID << 16) | 3,
                 species=260)
    game = Game([Mon(), Mon(shiny=True)], at_battle=[(FOE + 0x8000, own)])
    run(Ctx(game))

    assert PARTY[0].encryption_key not in keys(wired)
    assert len(game.resets) == 1


def test_the_party_is_read_once_not_every_attempt(wired):
    """It cannot change across resets, and each read idled A."""
    game = Game([Mon(), Mon(), Mon(shiny=True)])
    run(Ctx(game))
    assert wired.party_reads == 1


def test_no_encounter_resets_and_tries_again(wired):
    game = Game([Mon(shiny=True)], presses=10 ** 9)
    ctx = Ctx(game, cfg={**FAST, "static_timeout": 1.0})
    real = game.soft_reset

    def soft_reset(hold_s=0.5):
        real(hold_s)
        game.presses_needed = 1          # works after the reset

    game.soft_reset = soft_reset                   # type: ignore
    run(ctx)

    assert len(game.resets) == 1
    assert "read_failure" in ctx.kinds()
    assert "target_hit" in ctx.kinds()


# ----------------------------------------------------------------------
# Spamming must not run into the battle
# ----------------------------------------------------------------------
def test_no_a_press_ever_reaches_the_battle_menu(wired):
    game = Game([Mon() for _ in range(6)] + [Mon(shiny=True)], intro=0.15)
    run(Ctx(game, seconds=20.0))

    assert len(game.resets) == 6
    assert game.fight == 0, f"{game.fight} A press(es) chose Fight"


def test_a_wild_that_turns_up_somewhere_new_is_still_found(wired):
    """The cheap check only reads where the last wild was. The periodic
    sweep is what finds one elsewhere -- and in time."""
    game = Game([Mon(), Mon(shiny=True)], slots=[SLOT, SLOT + 0x1000],
                intro=0.3)
    run(Ctx(game, cfg={**FAST, "static_full_every": 5}))

    assert len(game.resets) == 1
    assert game.fight == 0


def test_once_the_slot_is_known_a_check_is_one_read(wired):
    """The press rate rests on it: on the console a sweep is ~128 RPC
    round trips, a slot read is one."""
    game = Game([Mon() for _ in range(3)] + [Mon(shiny=True)])
    run(Ctx(game))

    slot_reads = sum(1 for _, a, n in game.reads if n == 232)
    sweeps = sum(1 for _, a, n in game.reads if a == FOE and n > 232)
    assert slot_reads > 2 * sweeps, (
        f"{slot_reads} slot reads against {sweeps} full sweeps")


def test_a_battle_already_up_when_reading_resumes_is_flagged(wired, caplog):
    """The blind presses are safe only while the boot outlasts the
    grace. When it does not, say so."""
    game = Game([Mon(), Mon(shiny=True)], boot=0.0, presses=1)
    with caplog.at_level(logging.WARNING, logger=se.__name__):
        run(Ctx(game))
    assert "ALREADY UP" in caplog.text


def test_a_normal_boot_is_not_flagged(wired, caplog):
    game = Game([Mon(), Mon(shiny=True)])
    with caplog.at_level(logging.WARNING, logger=se.__name__):
        run(Ctx(game))
    assert len(game.resets) == 1
    assert "ALREADY UP" not in caplog.text


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
def test_the_plan_does_not_read_the_starter_hunts_keys():
    """A default can never beat a key that is set, so sharing the
    starter hunt's press_hold would hand this mode its value."""
    shipped = shipped_soft_reset()
    plan = se.StaticPlan.from_config({**shipped, "press_hold": 0.2})
    assert plan.press_hold == float(shipped.get(
        "static_press_hold", se.StaticPlan().press_hold))
    assert plan.press_hold != 0.2


def test_the_relaunch_silences_come_from_the_shared_keys():
    shipped = shipped_soft_reset()
    plan = se.StaticPlan.from_config(shipped)
    assert plan.reload_grace == float(shipped["reload_read_grace"])
    assert plan.pre_reset_quiet == float(shipped["pre_reset_quiet"])


def test_the_relaunch_silences_cannot_be_configured_away():
    plan = se.StaticPlan.from_config({"pre_reset_quiet": 0,
                                      "reload_read_grace": 0})
    assert plan.pre_reset_quiet >= 0.05
    assert plan.reload_grace >= 0.5


# ----------------------------------------------------------------------
# Registration
# ----------------------------------------------------------------------
def test_the_mode_is_registered():
    from pokebot.modes import MODES

    assert "static_encounter" in MODES


def test_the_launcher_passes_the_trainer_name():
    """The party's own keys are excluded from wild detection by OT."""
    src = (REPO / "launcher.py").read_text(encoding="utf-8")
    assert '("soft_reset", "gifts", "static_encounter")' in src
