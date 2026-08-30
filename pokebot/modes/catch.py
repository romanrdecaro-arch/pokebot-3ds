"""
Catching a wild target instead of running from it.

The bot already knows a shiny is on screen before the player could;
this turns that into a caught Pokémon. The sequence is the same three
touches a human makes -- BAG, POKE BALLS, first ball in the pocket --
followed by clearing the "Gotcha!" text and the nickname prompt.

Touch points are given as fractions of the 3DS *bottom screen*, not of
the Azahar window, and converted through the same layout geometry the
RUN button uses. That keeps them correct at any window size and in any
screen layout, which hardcoded window fractions would not be.

A ball that is not a Master Ball can fail, and a failed throw hands the
turn back with the command menu on screen again -- so the whole
sequence retries until the catch is confirmed in the party, the
attempts run out, or the caller asks to stop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger(__name__)

# Bottom-screen-local fractions, measured from Pokemon X/Y's wild
# battle UI. (0,0) is the top-left of the touch screen.
DEFAULT_BAG = (0.135, 0.86)      # BAG, bottom-left of the command menu
DEFAULT_BALLS = (0.743, 0.205)   # POKE BALLS, top-right bag pocket
DEFAULT_BALL = (0.245, 0.117)    # first item slot in the pocket

# The 3DS asks "Would you like to give a nickname?" after a catch, and
# B answers no. Tapping it also clears the Gotcha! / Pokedex / "sent to
# Box" text, so one button clears every screen between the throw and
# the overworld.
DISMISS_BUTTON = "B"


@dataclass(frozen=True)
class CatchPlan:
    """Where to touch and how long to wait between touches."""
    bag: tuple = DEFAULT_BAG
    balls: tuple = DEFAULT_BALLS
    ball: tuple = DEFAULT_BALL
    layout: str = "side_by_side"
    overrides: dict = field(default_factory=dict)

    intro_taps: int = 4          # B presses to clear the appearance text
    intro_gap: float = 0.35
    menu_settle: float = 1.5     # command menu draw
    bag_settle: float = 1.2      # bag pocket list draw
    pocket_settle: float = 1.2   # item list draw
    throw_settle: float = 6.0    # ball throw + shake + Gotcha!
    # The throw is followed by a long chain of text boxes -- shakes,
    # "Gotcha!", the Pokedex entry, the nickname prompt, "sent to Box".
    # Hammering B through all of them is far quicker than waiting each
    # one out, and B answers "no" to the nickname prompt on the way.
    post_throw_taps: int = 25
    post_throw_gap: float = 0.12

    attempts: int = 5
    confirm_window: float = 12.0  # per attempt, watching the party
    confirm_gap: float = 1.0

    @classmethod
    def from_config(cls, rcfg: Optional[dict]) -> "CatchPlan":
        """Build from a ``random_encounters:`` section."""
        rcfg = rcfg or {}

        def point(key: str, default: tuple) -> tuple:
            raw = rcfg.get(key)
            if not raw:
                return default
            try:
                return (float(raw[0]), float(raw[1]))
            except (TypeError, ValueError, IndexError):
                log.warning(f"  {key}={raw!r} is not an [x, y] pair; "
                            f"using the default {default}")
                return default

        def num(key: str, default: float) -> float:
            try:
                return float(rcfg.get(key, default))
            except (TypeError, ValueError):
                return default

        overrides = {}
        for name, key in (("bag", "bag_touch"), ("balls", "balls_touch"),
                          ("ball", "ball_touch")):
            raw = rcfg.get(key)
            if raw:
                try:
                    overrides[name] = (float(raw[0]), float(raw[1]))
                except (TypeError, ValueError, IndexError):
                    log.warning(f"  {key}={raw!r} ignored (need [x, y])")

        return cls(
            bag=point("bag_local", DEFAULT_BAG),
            balls=point("balls_local", DEFAULT_BALLS),
            ball=point("ball_local", DEFAULT_BALL),
            layout=str(rcfg.get("screen_layout", "side_by_side")).lower(),
            overrides=overrides,
            intro_taps=int(num("catch_intro_taps", 4)),
            menu_settle=num("catch_menu_settle", 1.5),
            bag_settle=num("catch_bag_settle", 1.2),
            pocket_settle=num("catch_pocket_settle", 1.2),
            throw_settle=num("catch_throw_settle", 6.0),
            attempts=max(1, int(num("catch_attempts", 5))),
            confirm_window=num("catch_confirm_window", 12.0),
            confirm_gap=num("catch_confirm_gap", 1.0),
            post_throw_taps=max(0, int(num("catch_post_throw_taps", 25))),
            post_throw_gap=num("catch_post_throw_gap", 0.12),
        )


@dataclass(frozen=True)
class CatchResult:
    caught: bool
    detail: str
    attempts: int = 0
    touches_sent: int = 0
    touches_failed: int = 0

    @property
    def ok(self) -> bool:
        """Did the sequence actually reach the game?

        A confirmed catch is obviously fine, but so is an unconfirmed
        one where every touch landed -- a full party sends the catch
        to a box, where the party scan cannot see it.
        """
        return self.caught or (self.touches_sent > 0
                               and self.touches_failed == 0)


def window_fraction(layout: str, local: tuple,
                    override: Optional[tuple] = None):
    """Bottom-screen-local (x, y) -> whole-window fractions.

    Falls back to the local values unchanged if the window cannot be
    measured; they are at least in the right half of the screen for a
    side-by-side layout, which beats refusing to touch at all.
    """
    if override:
        return float(override[0]), float(override[1]), "override"
    try:
        from ..platform_utils import find_azahar_hwnd, get_client_size
        from ..platform_utils import bottom_screen_fraction
        hwnd = find_azahar_hwnd()
        size = get_client_size(hwnd) if hwnd else None
        if size:
            fx, fy = bottom_screen_fraction(
                size[0], size[1], layout, local[0], local[1])
            return fx, fy, f"{layout} {size[0]}x{size[1]}"
    except Exception as exc:
        log.warning(f"  touch geometry failed: {exc}")
    return float(local[0]), float(local[1]), "fallback"


def _wait(ctx, seconds: float) -> bool:
    """Sleep unless asked to stop. True if still running."""
    ctx._stop_evt.wait(seconds)
    return not ctx.should_stop()


def _dismiss(ctx, times: int, gap: float) -> None:
    for _ in range(times):
        if ctx.should_stop():
            return
        ctx.input.tap(DISMISS_BUTTON, hold_s=0.05)
        ctx._stop_evt.wait(gap)


class _Toucher:
    """Sends touches and remembers whether they landed."""

    def __init__(self, ctx, plan: CatchPlan):
        self.ctx = ctx
        self.plan = plan
        self.sent = 0
        self.failed = 0

    def at(self, name: str, local: tuple) -> bool:
        fx, fy, how = window_fraction(
            self.plan.layout, local, self.plan.overrides.get(name))
        ok = self.ctx.input.tap_touch(fx, fy, hold_s=0.08)
        if ok:
            self.sent += 1
        else:
            self.failed += 1
        log.info(f"  catch: touch {name} @ ({fx:.3f},{fy:.3f}) [{how}] "
                 f"-> {'sent' if ok else 'FAILED (touch unavailable)'}")
        return ok


def catch_wild(ctx, plan: CatchPlan, target_key: int,
               party_keys_fn: Callable[[], set]) -> CatchResult:
    """Throw balls at the current wild until it is in the party.

    ``party_keys_fn`` re-reads the party and returns the set of
    encryption keys in it; the catch is confirmed when ``target_key``
    appears there. That is a fact about game memory rather than a guess
    from timing, which matters because the whole point of this routine
    is that the thing it is catching is rare.
    """
    t = _Toucher(ctx, plan)
    log.info(f"  CATCHING: BAG -> POKE BALLS -> slot 1 "
             f"(up to {plan.attempts} attempt(s))")

    for attempt in range(1, plan.attempts + 1):
        if ctx.should_stop():
            return CatchResult(False, "stopped before the throw",
                               attempt - 1, t.sent, t.failed)

        # Clear the "A wild X appeared!" text so the command menu is
        # up. On a retry this clears the wild's attack text instead.
        _dismiss(ctx, plan.intro_taps, plan.intro_gap)
        if not _wait(ctx, plan.menu_settle):
            return CatchResult(False, "stopped waiting for the menu",
                               attempt, t.sent, t.failed)

        t.at("bag", plan.bag)
        if not _wait(ctx, plan.bag_settle):
            return CatchResult(False, "stopped in the bag",
                               attempt, t.sent, t.failed)
        t.at("balls", plan.balls)
        if not _wait(ctx, plan.pocket_settle):
            return CatchResult(False, "stopped in the ball pocket",
                               attempt, t.sent, t.failed)
        t.at("ball", plan.ball)

        # Throw + shakes + "Gotcha!". Nothing to read yet.
        if not _wait(ctx, plan.throw_settle):
            return CatchResult(False, "stopped during the throw",
                               attempt, t.sent, t.failed)

        # Blast through the post-throw text chain.
        if plan.post_throw_taps:
            log.info(f"  catch: {plan.post_throw_taps} x B to clear the "
                     f"catch text")
            _dismiss(ctx, plan.post_throw_taps, plan.post_throw_gap)
            if ctx.should_stop():
                return CatchResult(False, "stopped clearing the text",
                                   attempt, t.sent, t.failed)

        if _confirm(ctx, plan, target_key, party_keys_fn):
            log.info(f"  CAUGHT on attempt {attempt} — it is in the party.")
            return CatchResult(True, f"caught on attempt {attempt}",
                               attempt, t.sent, t.failed)

        if t.failed and not t.sent:
            # Touch is unavailable entirely; retrying cannot help and
            # would burn the remaining attempts against a black hole.
            return CatchResult(
                False,
                "touch input is not reaching Azahar, so no ball was "
                "thrown. The wild is still on screen — catch it by hand.",
                attempt, t.sent, t.failed)

        log.warning(f"  catch attempt {attempt} not confirmed; "
                    f"{'retrying' if attempt < plan.attempts else 'giving up'}")

    return CatchResult(False, f"not confirmed after {plan.attempts} attempt(s)",
                       plan.attempts, t.sent, t.failed)


def _confirm(ctx, plan: CatchPlan, target_key: int,
             party_keys_fn: Callable[[], set]) -> bool:
    """Tap through the post-catch text, watching for it in the party."""
    deadline = max(1, int(plan.confirm_window / max(plan.confirm_gap, 0.05)))
    for _ in range(deadline):
        if ctx.should_stop():
            return False
        try:
            if target_key in (party_keys_fn() or set()):
                return True
        except Exception as exc:
            log.debug(f"  catch: party re-read failed: {exc}")
        # B clears Gotcha! / the Pokedex entry / "was sent to Box",
        # and answers "no" to the nickname prompt.
        ctx.input.tap(DISMISS_BUTTON, hold_s=0.05)
        ctx._stop_evt.wait(plan.confirm_gap)
    return False


def settle_after_battle(ctx, taps: int = 3, gap: float = 0.6,
                        tail: float = 2.0) -> None:
    """Clear whatever text is left and let the overworld slide back."""
    _dismiss(ctx, taps, gap)
    ctx._stop_evt.wait(tail)
