"""
The fishing cast loop.

    cast (Y) -> watch the foe window -> hook (A) the moment something
    lands there -> the caller evaluates it -> recast.

Detection drives the hook rather than timing. The bot cannot see the
"Oh! A bite!" cue, but it can see the wild record the game generates,
and reacting to that is the difference between a loop that hooks every
bite and one that spams A hoping to land inside a window it cannot
observe.

B is pressed during RECOVERY -- after a cast that produced nothing, and
whenever the loop has to get itself unstuck -- because that is where
text boxes pile up and block the next cast. It is deliberately NOT
pressed while the rod is out: there is no text to clear then, and B
during a cast reels the line in, which would stop the loop fishing at
all.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)

HOOKED = "hooked"
NO_BITE = "no-bite"
STOPPED = "stopped"


@dataclass(frozen=True)
class FishPlan:
    """How to work the rod."""
    cast_button: str = "Y"       # the registered key item
    hook_button: str = "A"
    clear_button: str = "B"

    # How long to wait for something to appear in the foe window after
    # a cast. A cast that produces nothing costs this much, so it is
    # the main lever on casts per minute.
    bite_timeout: float = 4.0
    # How often to look. Each poll is a scan of the foe window, which
    # is a few hundred 1 KB reads, so this trades RPC load against how
    # quickly the hook follows the bite.
    poll_gap: float = 0.25
    hook_taps: int = 1
    hook_gap: float = 0.15

    # Recovery: B presses to clear "Not even a nibble..." and anything
    # else on screen, then a breath before recasting.
    clear_taps: int = 4
    clear_gap: float = 0.25
    recast_gap: float = 0.4

    # No encounter for this long means the loop is wedged -- a cast
    # that never registered, or text nobody cleared. Start over rather
    # than casting into a menu forever. 0 disables.
    stuck_timeout: float = 60.0

    @classmethod
    def from_config(cls, rcfg: dict | None) -> "FishPlan":
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
            cast_button=str(rcfg.get("fish_cast_button", d.cast_button)),
            hook_button=str(rcfg.get("fish_hook_button", d.hook_button)),
            clear_button=str(rcfg.get("fish_clear_button", d.clear_button)),
            bite_timeout=num("fish_cast_settle", d.bite_timeout),
            poll_gap=max(0.05, num("fish_poll_gap", d.poll_gap)),
            hook_taps=max(1, int(num("fish_hook_taps", d.hook_taps))),
            hook_gap=num("fish_hook_gap", d.hook_gap),
            clear_taps=max(0, int(num("fish_clear_taps", d.clear_taps))),
            clear_gap=num("fish_clear_gap", d.clear_gap),
            recast_gap=num("fish_recast_gap", d.recast_gap),
            stuck_timeout=max(0.0, num("stuck_timeout", d.stuck_timeout)),
        )


def clear_text(ctx, plan: FishPlan) -> None:
    """B through whatever is on screen so the next cast can land."""
    for _ in range(plan.clear_taps):
        if ctx.should_stop():
            return
        ctx.input.tap(plan.clear_button, hold_s=0.05)
        ctx._stop_evt.wait(plan.clear_gap)


def cast_once(ctx, detect: Callable[[], bool], plan: FishPlan) -> str:
    """One cast. Returns HOOKED, NO_BITE or STOPPED.

    ``detect`` reports whether a wild record the caller has not seen
    before is now in the foe window. It is called every ``poll_gap``
    until ``bite_timeout``; the first True is the bite, and the hook
    goes in immediately after it.
    """
    if ctx.should_stop():
        return STOPPED

    ctx.input.tap(plan.cast_button, hold_s=0.05)
    log.info(f"  fishing: cast ({plan.cast_button}), watching for a bite "
             f"for up to {plan.bite_timeout:.1f}s")

    deadline = time.monotonic() + plan.bite_timeout
    while time.monotonic() < deadline:
        if ctx.should_stop():
            return STOPPED
        ctx._stop_evt.wait(plan.poll_gap)
        try:
            bite = bool(detect())
        except Exception as exc:
            # A failed read is not a reason to abandon the cast.
            log.debug(f"  fishing: foe-window check failed: {exc}")
            continue
        if bite:
            log.info(f"  fishing: bite -> {plan.hook_button} to hook it")
            for _ in range(plan.hook_taps):
                if ctx.should_stop():
                    return STOPPED
                ctx.input.tap(plan.hook_button, hold_s=0.05)
                ctx._stop_evt.wait(plan.hook_gap)
            return HOOKED

    # Nothing bit. The game is sitting on "Not even a nibble..." and
    # the next cast will be eaten by that text box unless it is
    # cleared first.
    log.info(f"  fishing: nothing in {plan.bite_timeout:.1f}s - "
             f"clearing and recasting")
    clear_text(ctx, plan)
    ctx._stop_evt.wait(plan.recast_gap)
    return NO_BITE


def restart(ctx, plan: FishPlan) -> None:
    """Get unwedged: clear everything on screen, twice over.

    Called when nothing has been caught for ``stuck_timeout``. The
    usual cause is a cast that never registered or a text box nobody
    answered, and both come free of the same B presses -- which are
    safe in the overworld, unlike the RUN touch the walking hunt uses
    to recover.
    """
    log.warning("  fishing: nothing for a while - clearing the screen "
                "and starting the cast loop over")
    clear_text(ctx, plan)
    clear_text(ctx, plan)
    ctx._stop_evt.wait(plan.recast_gap)
