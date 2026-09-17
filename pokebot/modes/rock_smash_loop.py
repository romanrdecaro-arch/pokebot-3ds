"""
The Rock Smash loop.

    press A -> watch the foe window -> stop pressing the instant
    something lands there -> the caller evaluates it -> smash again.

Rock Smash is the one idle action where the button that MAKES an
encounter is also the button that FIGHTS it. A opens the "would you
like to use Rock Smash?" prompt, A answers Yes, A clears the result
text -- and the moment a wild appears, that same A press means
"attack with move 1", pointed at the shiny the hunt exists to catch.

So this loop is built around when to STOP pressing. Detection runs
between every press and once more before the first one, because the
outer hunt only calls in here when its own (much slower) scan found
nothing, and the cheap watcher can see a battle that scan missed.

B is the mirror of that trap. In the Yes/No prompt B answers "No", so
B is never pressed while smashing -- only in ``restart``, where any
prompt still on screen is stale by definition.

Rock Smash does not guarantee an encounter; most rocks give nothing at
all. A quiet stretch is therefore normal rather than a fault, which is
why the caller's stall watchdog is set short (30 s) for this mode and
recovers by clearing the screen rather than by fleeing a battle that
was never there.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)

ENCOUNTER = "encounter"
NO_ENCOUNTER = "no-encounter"
STOPPED = "stopped"

#: Internal only: the foe read failed, so whether a battle is up is
#: unknown. Never treated as "no battle" -- that is the reading that
#: would press A into a shiny.
_UNSURE = "unsure"


@dataclass(frozen=True)
class SmashPlan:
    """How to work the rock."""
    smash_button: str = "A"
    clear_button: str = "B"

    # A presses per attempt. One opens the prompt, one answers Yes,
    # the rest carry through the smash animation and its text -- with
    # enough headroom that a press eaten by a transition does not
    # strand the attempt half-way.
    taps: int = 6
    # How long to watch the foe window after each press before pressing
    # again. Also the ceiling on how late the loop can notice a battle,
    # since the next press is what it is racing.
    settle: float = 0.4
    # How often to look inside that window. The hot path is ONE
    # 232-byte read (see foe_watch), not the 128-round-trip window
    # scan, so this can be -- and needs to be -- small.
    poll_gap: float = 0.02

    # Recovery: B presses to clear a stale prompt or text box, then a
    # breath before smashing again.
    clear_taps: int = 4
    clear_gap: float = 0.25
    restart_gap: float = 0.4

    @classmethod
    def from_config(cls, rcfg: dict | None) -> "SmashPlan":
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
            smash_button=str(rcfg.get("smash_button", d.smash_button)),
            clear_button=str(rcfg.get("smash_clear_button", d.clear_button)),
            taps=max(1, int(num("smash_taps", d.taps))),
            settle=num("smash_settle", d.settle),
            poll_gap=max(0.005, num("smash_poll_gap", d.poll_gap)),
            clear_taps=max(0, int(num("smash_clear_taps", d.clear_taps))),
            clear_gap=num("smash_clear_gap", d.clear_gap),
            restart_gap=num("smash_restart_gap", d.restart_gap),
        )


def _watch(ctx, detect: Callable[[], bool], seconds: float,
           plan: SmashPlan) -> str:
    """Poll the foe window for up to ``seconds``.

    ``seconds = 0`` still checks exactly once. Returns ENCOUNTER,
    STOPPED, NO_ENCOUNTER, or _UNSURE when the last read in the
    span failed -- the caller must not treat that as "all clear".
    """
    deadline = time.monotonic() + max(0.0, seconds)
    first = True
    failed = False
    while first or time.monotonic() < deadline:
        if ctx.should_stop():
            return STOPPED
        # Check BEFORE sleeping: the press that starts a battle is the
        # one immediately behind us, so the first look is the one that
        # matters most.
        if not first:
            ctx._stop_evt.wait(plan.poll_gap)
        first = False
        try:
            if detect():
                return ENCOUNTER
            failed = False
        except Exception as exc:
            # A failed read is not a reason to abandon the attempt,
            # but it is also not evidence that nothing is there.
            log.debug(f"  rock smash: foe-window check failed: {exc}")
            failed = True
    return _UNSURE if failed else NO_ENCOUNTER


def clear_text(ctx, plan: SmashPlan) -> None:
    """B through whatever is on screen so the next A press lands."""
    for _ in range(plan.clear_taps):
        if ctx.should_stop():
            return
        ctx.input.tap(plan.clear_button, hold_s=0.05)
        ctx._stop_evt.wait(plan.clear_gap)


def smash_once(ctx, detect: Callable[[], bool], plan: SmashPlan) -> str:
    """One smash attempt. Returns ENCOUNTER, NO_ENCOUNTER or STOPPED.

    ``detect`` reports whether a wild record the caller has not seen
    before is now in the foe window. It is checked before every press
    and continuously between them; the first True ends the attempt
    with no further input, leaving the battle untouched for the caller
    to evaluate.
    """
    if ctx.should_stop():
        return STOPPED

    for _ in range(plan.taps):
        # Look before pressing. On the first pass this is what keeps A
        # off a battle the outer scan missed; on later passes it is
        # what keeps A off the battle the previous press just started.
        before = _watch(ctx, detect, 0.0, plan)
        if before in (ENCOUNTER, STOPPED):
            return before
        if before == _UNSURE:
            # Unknown, so do not press blind -- wait out a poll gap and
            # ask again. A genuinely dead RPC is the caller's watchdog
            # to notice, not something to paper over with A presses.
            ctx._stop_evt.wait(plan.poll_gap)
            continue
        if ctx.should_stop():
            return STOPPED

        ctx.input.tap(plan.smash_button, hold_s=0.05)
        after = _watch(ctx, detect, plan.settle, plan)
        if after in (ENCOUNTER, STOPPED):
            if after == ENCOUNTER:
                log.info("  rock smash: encounter -- presses stopped")
            return after

    log.info(f"  rock smash: nothing from {plan.taps} press(es) - "
             f"smashing again")
    return NO_ENCOUNTER


def restart(ctx, plan: SmashPlan) -> None:
    """Get unwedged: clear everything on screen, twice over.

    Called when nothing has been encountered for the stall timeout.
    The usual cause is a prompt nobody answered or text nobody cleared,
    and B fixes both -- unlike the RUN touch the walking hunt recovers
    with, which in the overworld lands on the PSS and which has no
    battle to run from here anyway.
    """
    log.warning("  rock smash: nothing for a while - clearing the screen "
                "and starting over")
    clear_text(ctx, plan)
    clear_text(ctx, plan)
    ctx._stop_evt.wait(plan.restart_gap)
