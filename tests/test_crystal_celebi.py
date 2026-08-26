"""
Tests for the Celebi soft-reset hunt.

Two behaviours carry the whole mode:

  * On a shiny it must STOP and send no further input. The player asked
    to fight it themselves, and a stray A press on the "Wild CELEBI
    appeared!" screen is a fight they did not choose to start.
  * On a non-shiny it must soft-reset WITHOUT re-scanning the heap.
    A hunt is thousands of resets, and a scan per reset is what took
    the emulator down before.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot import crystal, gen2                          # noqa: E402
from pokebot.modes import MODES, crystal_celebi as cc      # noqa: E402
from test_gen2 import make_mon                             # noqa: E402
from test_crystal_session import build_space               # noqa: E402


# --------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------

class FakeInput:
    """Records every button the mode sends, in order."""

    def __init__(self):
        self.events = []

    def tap(self, button, hold_s=0.05):
        self.events.append(button)

    def gb_soft_reset(self, hold_s=0.6):
        self.events.append("RESET")

    def soft_reset(self, hold_s=0.5):          # the 3DS combo
        self.events.append("3DS-RESET")


class FakeDashboard:
    def __init__(self):
        self.sent = []

    def broadcast(self, kind, **payload):
        self.sent.append((kind, payload))

    def kinds(self):
        return [k for k, _ in self.sent]

    def of(self, kind):
        return [p for k, p in self.sent if k == kind]


class FakeCtx:
    def __init__(self, config=None):
        self.rpc = object()
        self.config = config or {}
        self.input = FakeInput()
        self.dashboard = FakeDashboard()
        self._stop_evt = threading.Event()
        self.stop_reason = None

    def should_stop(self):
        return self._stop_evt.is_set()

    def request_stop(self, reason=""):
        self.stop_reason = reason
        self._stop_evt.set()


def _party_block() -> bytes:
    """A genuinely valid party block, built by the tested helpers."""
    space = build_space([make_mon(157, 100)], span=0x8000)
    at = gen2.PARTY_COUNT_FROM_WRAM
    return space[at:at + crystal.SIGNATURE_LEN]


class ScriptedSession:
    """A CrystalSession stand-in driven by a list of scripted attempts.

    Each entry is the opponent for one attempt; ``None`` means no
    battle ever starts. ``advance()`` is wired to the reset, so the
    script only moves on when the mode actually resets.
    """

    #: Polls spent on the overworld before the battle starts. The real
    #: game is always on the overworld first, and the mode requires
    #: seeing it — a battle byte read before then is title-screen
    #: leftovers, not a battle.
    OVERWORLD_POLLS = 2

    def __init__(self, attempts, base=0x08a2ffac, region=None):
        self.attempts = list(attempts)
        self.base = base
        self.index = 0
        self.polls = 0
        self.party_reads = 0
        self.region = region or bytes(_ENEMY_SPAN)
        self.in_battle = crystal.BATTLE_WILD

    def ensure_base(self):
        return self.base

    def battle_mode(self):
        if self._current() is None:
            return crystal.BATTLE_NONE
        self.polls += 1
        if self.polls <= self.OVERWORLD_POLLS:
            return crystal.BATTLE_NONE
        return self.in_battle

    def enemy(self):
        return self._current()

    def _read(self, gb_addr, size):
        if gb_addr == gen2.GB_PARTY_COUNT:
            self.party_reads += 1
            return _party_block()
        if gb_addr == cc._ENEMY_REGION_LO:
            return self.region
        return bytes(size)

    def _current(self):
        if self.index >= len(self.attempts):
            return None
        return self.attempts[self.index]

    def advance(self):
        self.index += 1
        self.polls = 0          # back to the overworld after a reset


_ENEMY_SPAN = cc._ENEMY_REGION_HI - cc._ENEMY_REGION_LO

#: DV words. Shiny needs Def/Spe/Spc == 10 and Atk in the shiny set.
SHINY_WORD = (10 << 12) | (10 << 8) | (10 << 4) | 10
PLAIN_WORD = (10 << 12) | (3 << 8) | (14 << 4) | 2


def _enemy(word=PLAIN_WORD, species=251, level=30, max_hp=None):
    dvs = gen2.parse_dvs(word)
    if max_hp is None:
        base = gen2.BASE_HP.get(species)
        max_hp = gen2.max_hp(base, dvs["HP"], level) if base else 40
    return crystal.EnemyReading(
        species=species, level=level, dvs=dvs,
        shiny=gen2.is_shiny(dvs), confirmed=False, max_hp=max_hp,
        dv_check=gen2.verify_dv_reading(species, level, dvs, max_hp))


def _region_with(word: int) -> bytes:
    """The opponent region with a DV word at the real offset."""
    buf = bytearray(_ENEMY_SPAN)
    at = crystal.GB_ENEMY_DVS - cc._ENEMY_REGION_LO
    buf[at:at + 2] = word.to_bytes(2, "big")
    return bytes(buf)


def _run(ctx, session, **cfg):
    """Run the mode against a scripted session, resets driving the script."""
    base_cfg = {"press_interval": 0.0, "encounter_timeout": 2.0,
                "boot_timeout": 2.0}
    base_cfg.update(cfg)
    ctx.config = {"celebi_hunt": base_cfg}

    real_reset = ctx.input.gb_soft_reset

    def reset_and_advance(hold_s=0.6):
        real_reset(hold_s)
        session.advance()

    ctx.input.gb_soft_reset = reset_and_advance
    cc.run(ctx)
    return ctx


def _patched(monkeypatch, session):
    monkeypatch.setattr(cc, "CrystalSession", lambda *a, **k: session)
    monkeypatch.setattr(cc, "_OPPONENT_SETTLE_S", 0.05)


# --------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------

def test_mode_is_registered_and_offered() -> None:
    from pokebot.games import methods_for
    assert "crystal_celebi" in MODES
    labels = {m.mode for m in methods_for("CRYSTAL-USA")}
    assert "crystal_celebi" in labels


# --------------------------------------------------------------------
# The shiny path — the one that must not press anything
# --------------------------------------------------------------------

def test_a_shiny_stops_the_bot_and_sends_no_further_input(monkeypatch):
    session = ScriptedSession([_enemy(SHINY_WORD)],
                              region=_region_with(SHINY_WORD))
    _patched(monkeypatch, session)
    ctx = _run(FakeCtx(), session)

    assert ctx.stop_reason and "shiny" in ctx.stop_reason
    assert "target_hit" in ctx.dashboard.kinds()
    # Nothing after the encounter: no reset, no stray A.
    assert "RESET" not in ctx.input.events, "reset past a shiny"
    assert "3DS-RESET" not in ctx.input.events


def test_a_shiny_is_reported_as_shiny_in_the_encounter_row(monkeypatch):
    session = ScriptedSession([_enemy(SHINY_WORD)],
                              region=_region_with(SHINY_WORD))
    _patched(monkeypatch, session)
    ctx = _run(FakeCtx(), session)
    rows = ctx.dashboard.of("encounter")
    assert rows and rows[-1]["shiny"] is True
    assert rows[-1]["species"] == 251


# --------------------------------------------------------------------
# The reset path
# --------------------------------------------------------------------

def test_a_plain_celebi_is_reset_and_the_hunt_continues(monkeypatch):
    session = ScriptedSession([_enemy(), _enemy(), _enemy(SHINY_WORD)],
                              region=_region_with(PLAIN_WORD))
    _patched(monkeypatch, session)

    # The third attempt is the shiny; give it a shiny region too.
    real_advance = session.advance

    def advance():
        real_advance()
        if session.index == 2:
            session.region = _region_with(SHINY_WORD)

    session.advance = advance
    ctx = _run(FakeCtx(), session)

    assert ctx.input.events.count("RESET") == 2, ctx.input.events
    assert ctx.stop_reason and "shiny" in ctx.stop_reason
    assert len(ctx.dashboard.of("encounter")) == 3


def test_the_reset_is_the_game_boy_combo_not_the_3ds_one(monkeypatch):
    """A+B+Start+Select resets the emulated Game Boy and leaves the
    located WRAM base intact. L+R+Start is a 3DS-level action."""
    from pokebot.input_driver import InputDriver

    combos = []
    monkeypatch.setattr(
        InputDriver, "hold_combo",
        lambda self, buttons, hold_s=0.5, what="": combos.append(tuple(buttons)))
    drv = InputDriver.__new__(InputDriver)
    drv.gb_soft_reset()
    assert combos == [("A", "B", "Start", "Select")]


def test_waiting_for_the_reload_never_goes_through_ensure_base(monkeypatch):
    """A hunt is thousands of resets, and one heap scan each is what
    killed the emulator.

    While the game sits on the title screen there is no party to find,
    so ensure_base would read that as a lost base and start scanning.
    _wait_for_reload therefore has to read the party block directly.
    Asserted by counting ensure_base calls: the mode locates the base
    ONCE at startup and must never ask again, however many resets it
    does.
    """
    calls = []
    session = ScriptedSession([_enemy(), _enemy(), _enemy(SHINY_WORD)],
                              region=_region_with(PLAIN_WORD))
    session.ensure_base = lambda: calls.append(1) or session.base
    _patched(monkeypatch, session)

    real_advance = session.advance

    def advance():
        real_advance()
        if session.index == 2:
            session.region = _region_with(SHINY_WORD)

    session.advance = advance
    ctx = _run(FakeCtx(), session)

    assert ctx.input.events.count("RESET") == 2
    assert len(calls) == 1, (
        f"ensure_base called {len(calls)} times across 2 resets — each one "
        f"can trigger a full heap scan")
    assert session.party_reads > 0, "never actually waited for the reload"


# --------------------------------------------------------------------
# Refusing to run on an untrustworthy reading
# --------------------------------------------------------------------

def test_a_dv_reading_that_fails_its_max_hp_check_halts_the_hunt(monkeypatch):
    """A wrong DV offset would silently reset past every shiny.

    Celebi at Lv30 with HP DV 4 must have 102 max HP. If the game
    disagrees, the reading is wrong and the hunt is worthless — stop
    rather than spend a thousand resets on a broken check.
    """
    bad = _enemy(max_hp=115)               # DVs say 102
    assert bad.dv_check is False
    session = ScriptedSession([bad], region=_region_with(PLAIN_WORD))
    _patched(monkeypatch, session)
    ctx = _run(FakeCtx(), session)

    assert ctx.stop_reason and "max-HP" in ctx.stop_reason
    assert "RESET" not in ctx.input.events, "reset on an untrusted reading"
    assert "read_failure" in ctx.dashboard.kinds()


def test_a_correct_reading_passes_its_own_check() -> None:
    """The Celebi actually read on 2026-08-26: Lv30, DVs 4/10/3/14/2."""
    good = _enemy()
    assert good.max_hp == 102
    assert good.dv_check is True


def test_a_species_without_a_trusted_base_hp_is_not_judged() -> None:
    """Unknown base HP means the question cannot be answered — and an
    unanswerable check must not read as a failure and halt a hunt."""
    oddish = _enemy(species=43, level=5, max_hp=20)
    assert oddish.dv_check is None


# --------------------------------------------------------------------
# Giving up honestly
# --------------------------------------------------------------------

def test_no_encounter_within_the_timeout_stops_with_a_reason(monkeypatch):
    """Input is delivered indirectly and may not arrive at all. Spinning
    silently forever would hide that; the mode must say so."""
    session = ScriptedSession([None])
    _patched(monkeypatch, session)
    ctx = _run(FakeCtx(), session, encounter_timeout=0.3)

    assert ctx.stop_reason and "no encounter" in ctx.stop_reason
    assert "A" in ctx.input.events, "did not even try pressing A"
    assert "read_failure" in ctx.dashboard.kinds()


def test_a_trainer_battle_stops_rather_than_resetting_blind(monkeypatch):
    session = ScriptedSession([_enemy()])
    session.in_battle = crystal.BATTLE_TRAINER
    _patched(monkeypatch, session)
    ctx = _run(FakeCtx(), session)

    assert ctx.stop_reason and "trainer" in ctx.stop_reason
    assert "RESET" not in ctx.input.events


def test_a_missing_wram_base_stops_before_pressing_anything(monkeypatch):
    session = ScriptedSession([_enemy()])
    session.ensure_base = lambda: None
    _patched(monkeypatch, session)
    ctx = _run(FakeCtx(), session)

    assert ctx.stop_reason == "crystal wram not found"
    assert ctx.input.events == [], "pressed buttons with no base located"


def test_a_stale_battle_byte_after_a_reset_is_not_an_encounter(monkeypatch):
    """Coming out of a soft reset the game sits on its title screen.

    The battle byte there holds whatever survived the reset, not a real
    battle state. Acting on it would read a garbage opponent and reset
    straight past the attempt that never happened, so the overworld has
    to be seen before a battle counts.
    """
    session = ScriptedSession([_enemy()], region=_region_with(PLAIN_WORD))
    # Never returns to the overworld: the byte is stuck non-zero.
    session.battle_mode = lambda: crystal.BATTLE_WILD
    _patched(monkeypatch, session)
    ctx = _run(FakeCtx(), session, encounter_timeout=0.3)

    assert ctx.stop_reason and "no encounter" in ctx.stop_reason
    assert "RESET" not in ctx.input.events
    assert not ctx.dashboard.of("encounter"), (
        "logged an encounter from a stale battle byte")
