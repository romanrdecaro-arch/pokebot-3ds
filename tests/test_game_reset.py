"""
Tests for soft-resetting in the middle of a hunt.

The dangerous part of a soft reset is not the button combo, it is the
READING. L+R+Start makes the game ask the 3DS to relaunch it, so Azahar
spends the next moment destroying the running process and building a
new one -- and its RPC server still holds a pointer to the old one. A
ReadMemory landing in that window dereferences memory being freed.
soft_reset.py carries four recorded 0xc0000005 access violations inside
azahar.exe from exactly that, all in one fault bucket.

So the properties worth testing are about silence: no reads in the
moment BEFORE the combo (a datagram already in flight cannot be called
back by any grace afterwards), and none for several seconds after it.
Pressing A through the boot logos is free and carries on throughout --
it is only the reading that waits.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot.modes import game_reset as gr  # noqa: E402


class FakeInput:
    def __init__(self):
        self.taps: list[str] = []
        self.resets: list[float] = []

    def soft_reset(self, hold_s=0.5):
        self.resets.append(time.monotonic())

    def tap(self, button, hold_s=0.05):
        self.taps.append(button)
        return None


class FakeCtx:
    """Real waits -- these tests are about when things happen."""

    def __init__(self):
        self.input = FakeInput()
        self._stop_evt = threading.Event()

    def should_stop(self):
        return self._stop_evt.is_set()

    def request_stop(self, reason=""):
        self._stop_evt.set()


FAST = gr.ResetPlan(quiet=0.05, grace=0.15, boot_timeout=3.0,
                    press_hold=0.001, press_gap=0.0, read_every=0.02)


def probe_recording(ctx, ready_after=0.0):
    """A save-loaded probe that records exactly when it read memory."""
    calls: list[float] = []
    started = time.monotonic()

    def loaded():
        calls.append(time.monotonic())
        return time.monotonic() - started >= ready_after

    return loaded, calls


# ----------------------------------------------------------------------
# The combo
# ----------------------------------------------------------------------
def test_the_reset_combo_is_sent():
    ctx = FakeCtx()
    loaded, _ = probe_recording(ctx)
    gr.soft_reset_and_wait(ctx, loaded, FAST)
    assert len(ctx.input.resets) == 1


def test_a_is_pressed_through_the_boot():
    """The logos, the title, CONTINUE and the save-data confirm are all
    just A -- and a wasted press costs 30 ms, so there is no reason to
    wait for them."""
    ctx = FakeCtx()
    loaded, _ = probe_recording(ctx, ready_after=0.4)
    gr.soft_reset_and_wait(ctx, loaded, FAST)
    assert ctx.input.taps.count("A") > 1


# ----------------------------------------------------------------------
# Silence around the teardown -- the crash-safety property
# ----------------------------------------------------------------------
def test_no_memory_is_read_in_the_quiet_window_before_the_combo():
    """A read already in flight cannot be called back afterwards."""
    ctx = FakeCtx()
    loaded, calls = probe_recording(ctx)
    started = time.monotonic()

    gr.soft_reset_and_wait(ctx, loaded, FAST)

    assert ctx.input.resets, "never reset"
    assert ctx.input.resets[0] - started >= FAST.quiet * 0.9, (
        "sent the combo without letting the last reads drain first")
    assert not [c for c in calls if c < ctx.input.resets[0]], (
        "read memory between being told to reset and resetting")


def test_no_memory_is_read_during_the_reload_grace():
    """The window where Azahar is destroying the old process."""
    ctx = FakeCtx()
    loaded, calls = probe_recording(ctx, ready_after=0.0)

    gr.soft_reset_and_wait(ctx, loaded, FAST)

    reset_at = ctx.input.resets[0]
    too_early = [c - reset_at for c in calls if c < reset_at + FAST.grace]
    assert not too_early, (
        f"read Azahar's memory {too_early} s after the reset combo, "
        f"inside the {FAST.grace}s reload grace")


def test_pressing_continues_through_the_grace():
    """Silence applies to reads, not to the controller."""
    ctx = FakeCtx()
    loaded, _ = probe_recording(ctx, ready_after=0.0)

    gr.soft_reset_and_wait(ctx, loaded, FAST)

    assert ctx.input.taps.count("A") >= 1


def test_the_probe_is_not_hammered_once_it_does_start():
    """A twelve-read party scan per iteration, hundreds of reads a
    second, aimed at the emulator at its most fragile moment."""
    ctx = FakeCtx()
    loaded, calls = probe_recording(ctx, ready_after=0.6)

    gr.soft_reset_and_wait(ctx, loaded, FAST)

    gaps = [b - a for a, b in zip(calls, calls[1:])]
    assert all(g >= FAST.read_every * 0.5 for g in gaps), \
        f"probed faster than read_every: {gaps}"


# ----------------------------------------------------------------------
# Outcome
# ----------------------------------------------------------------------
def test_it_reports_success_once_the_save_is_back():
    ctx = FakeCtx()
    loaded, _ = probe_recording(ctx, ready_after=0.0)
    assert gr.soft_reset_and_wait(ctx, loaded, FAST) is True


def test_it_reports_failure_if_the_save_never_comes_back():
    """Proof the combo actually landed. If it did not, the hunt is
    pressing A at an overworld it never left."""
    ctx = FakeCtx()
    plan = gr.ResetPlan(quiet=0.0, grace=0.02, boot_timeout=0.2,
                        press_hold=0.001, press_gap=0.0, read_every=0.02)

    assert gr.soft_reset_and_wait(ctx, lambda: False, plan) is False


def test_stopping_during_the_boot_ends_it():
    ctx = FakeCtx()
    plan = gr.ResetPlan(quiet=0.0, grace=0.0, boot_timeout=5.0,
                        press_hold=0.001, press_gap=0.0, read_every=0.01)

    def loaded():
        ctx.request_stop("user")
        return False

    assert gr.soft_reset_and_wait(ctx, loaded, plan) is False


def test_stopping_before_the_combo_never_resets():
    """A stop must not leave the game mid-relaunch."""
    ctx = FakeCtx()
    ctx.request_stop("user")

    assert gr.soft_reset_and_wait(ctx, lambda: True, FAST) is False
    assert ctx.input.resets == []


# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
def test_the_defaults_match_what_soft_reset_learned_the_hard_way():
    """These numbers were paid for in Azahar crashes; they should not
    drift apart from the mode that discovered them."""
    from pokebot.modes import soft_reset as sr

    d = gr.ResetPlan()
    assert d.quiet == sr._PRE_RESET_QUIET_S
    assert d.grace == sr._RELOAD_READ_GRACE_S
    assert d.read_every >= sr._MIN_RELOAD_READ_GAP_S


def test_config_values_are_read():
    plan = gr.ResetPlan.from_config(
        {"reset_grace": 6.0, "reset_boot_timeout": 90})
    assert plan.grace == 6.0
    assert plan.boot_timeout == 90


def test_a_junk_value_falls_back_rather_than_raising():
    assert gr.ResetPlan.from_config(
        {"reset_grace": "later"}).grace == gr.ResetPlan().grace


def test_the_grace_cannot_be_configured_away_entirely():
    """Zero grace is the crash. A user lowering it is tuning; a user
    zeroing it is removing the only thing standing between a read and
    a process being freed."""
    assert gr.ResetPlan.from_config({"reset_grace": 0}).grace > 0
