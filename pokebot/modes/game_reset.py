"""
Soft-resetting in the middle of a hunt.

Some hunts cannot flee their way to the next attempt. A Rock Smash
hunt is the clearest case: the rock it is aimed at is *gone* once
smashed, and nothing brings it back but reloading the area — so the
reset IS the hunt loop, not an error path.

The dangerous part is not the button combo, it is the reading. L+R+
Start makes the game ask the 3DS to relaunch it, so Azahar spends the
next moment destroying the running process and building a new one,
while its RPC server still holds a pointer to the old one. A
ReadMemory landing in that window dereferences memory being freed;
soft_reset.py records four 0xc0000005 access violations inside
azahar.exe from exactly this, all in one fault bucket, at the same
fault offset, on both renderers.

So this module is mostly about silence, in two halves:

  * ``quiet`` seconds of no reads BEFORE the combo. The caller has
    just finished evaluating an encounter, which ends in a party scan,
    and Azahar services RPC on its own thread — a datagram sent a
    millisecond ago can still be in flight when the teardown starts,
    and no grace afterwards can call it back.
  * ``grace`` seconds of no reads AFTER it, while the relaunch runs.

Pressing A is free and carries on throughout: the boot logos, the
title, CONTINUE and the save-data confirm are all just A, and a wasted
press costs 30 ms. It is only the READING that waits.

The timing constants come from ``soft_reset`` rather than being
restated here. They were paid for in crashes, and two copies of a
number like that drift.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from .soft_reset import (_MIN_RELOAD_READ_GAP_S, _PRE_RESET_QUIET_S,
                         _PRESS_GAP_S, _PRESS_HOLD_S,
                         _RELOAD_READ_GRACE_S)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResetPlan:
    """How to reset and how long to stay quiet around it."""
    #: No memory reads for this long BEFORE the combo.
    quiet: float = _PRE_RESET_QUIET_S
    #: No memory reads for this long AFTER it.
    grace: float = _RELOAD_READ_GRACE_S
    #: Give up if the save never comes back. Generous: a cold Azahar
    #: rebuilding shader caches can take a while, and the cost of
    #: waiting is a slow attempt while the cost of giving up early is
    #: a hunt that thinks it reset when it did not.
    boot_timeout: float = 60.0
    press_hold: float = _PRESS_HOLD_S
    press_gap: float = _PRESS_GAP_S
    #: Floor on how often the "are we back yet?" probe reads memory.
    #: The probe is a party scan — a dozen round trips — so polling it
    #: flat out would aim hundreds of reads a second at the emulator
    #: at the one moment it can least survive them.
    read_every: float = _MIN_RELOAD_READ_GAP_S
    #: Shortest gap between two resets. 0 leaves the rate uncapped;
    #: raise it if Azahar starts falling over mid-relaunch.
    cooldown: float = 0.0

    @classmethod
    def from_config(cls, rcfg: dict | None) -> "ResetPlan":
        rcfg = rcfg or {}
        d = cls()

        def num(key: str, default: float) -> float:
            try:
                return float(rcfg.get(key, default))
            except (TypeError, ValueError):
                log.warning(f"  {key}={rcfg.get(key)!r} is not a number; "
                            f"using {default}")
                return default

        return cls(
            # Floors, not clamps to the default: lowering these is
            # tuning, zeroing one removes the only thing between a
            # read and a process being freed.
            quiet=max(0.05, num("reset_quiet", d.quiet)),
            grace=max(0.5, num("reset_grace", d.grace)),
            boot_timeout=max(1.0, num("reset_boot_timeout",
                                      d.boot_timeout)),
            press_hold=max(0.01, num("reset_press_hold", d.press_hold)),
            press_gap=max(0.0, num("reset_press_gap", d.press_gap)),
            read_every=max(_MIN_RELOAD_READ_GAP_S,
                           num("reset_read_every", d.read_every)),
            cooldown=max(0.0, num("reset_cooldown", d.cooldown)),
        )


def soft_reset_and_wait(ctx, loaded: Callable[[], bool],
                        plan: ResetPlan,
                        last_reset: list | None = None) -> bool:
    """L+R+Start, then press A until ``loaded()`` says the save is back.

    ``loaded`` is the caller's proof that the relaunch actually
    happened — for a hunt with a party, "the party is readable again".
    It is only ever called after the reload grace, and never faster
    than ``plan.read_every``.

    Returns True once the save is back, False if the bot was stopped or
    the save never returned within ``boot_timeout``. A False is worth
    acting on: it usually means the combo did not land at all, and the
    hunt is about to press A at an overworld it never left.
    """
    if ctx.should_stop():
        return False

    if plan.cooldown and last_reset and last_reset[0]:
        waited = time.monotonic() - last_reset[0]
        if waited < plan.cooldown:
            log.info(f"  waiting {plan.cooldown - waited:.1f}s before the "
                     f"next reset (reset_cooldown)")
            ctx._stop_evt.wait(plan.cooldown - waited)
            if ctx.should_stop():
                return False

    # Let the last reads drain before asking for the relaunch.
    if plan.quiet:
        ctx._stop_evt.wait(plan.quiet)
        if ctx.should_stop():
            return False

    if last_reset is not None:
        last_reset[:] = [time.monotonic()]
    ctx.input.soft_reset()
    try:
        from ..platform_utils import focus_azahar
        focus_azahar()
    except Exception:
        pass

    started = time.monotonic()
    deadline = started + plan.boot_timeout
    next_read = started + plan.grace
    while not ctx.should_stop() and time.monotonic() < deadline:
        now = time.monotonic()
        if now >= next_read:
            next_read = now + plan.read_every
            try:
                if loaded():
                    log.info(f"  save back after {now - started:.1f}s")
                    return True
            except Exception as exc:
                # Expected while the new process is still coming up.
                log.debug(f"  reset probe failed (still booting?): {exc}")
        ctx.input.tap("A", hold_s=plan.press_hold)
        if plan.press_gap:
            ctx._stop_evt.wait(plan.press_gap)

    if not ctx.should_stop():
        log.error(f"  the save did not come back within "
                  f"{plan.boot_timeout:.0f}s. Either L+R+Start never "
                  f"landed (is Azahar focused?) or the title is waiting "
                  f"on something A does not answer.")
    return False
