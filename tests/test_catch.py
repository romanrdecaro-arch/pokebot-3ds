"""
Tests for catching a wild target instead of fleeing it.

The expensive failure here is losing a shiny: throwing at nothing
because touch is dead, walking away while the battle is still open, or
reporting a catch that did not happen. Those get the attention.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot.modes import catch  # noqa: E402


TARGET_KEY = 0xDEADBEEF


class FakeInput:
    """Records presses and touches. ``touch_ok`` fakes a dead touch path."""

    def __init__(self, touch_ok: bool = True):
        self.touch_ok = touch_ok
        self.touches: list[tuple[float, float]] = []
        self.taps: list[str] = []

    def tap_touch(self, x, y, hold_s=0.08) -> bool:
        self.touches.append((round(x, 3), round(y, 3)))
        return self.touch_ok

    def tap(self, button, hold_s=0.05) -> None:
        self.taps.append(button)


class FakeDashboard:
    """Mirrors DashboardServer.broadcast exactly.

    Deliberately keyword-only after the type: the real one is
    ``broadcast(msg_type, **fields)``, and a caller that passes a dict
    positionally must fail here the same way it fails in production.
    """

    def __init__(self):
        self.events: list[tuple] = []

    def broadcast(self, msg_type: str, **fields) -> None:
        self.events.append((msg_type, fields))


class FakeCtx:
    """Enough of BotContext for the catch routine.

    ``_stop_evt.wait`` returns immediately so timed sequences run at
    test speed instead of the ~30 s they take in the game.
    """

    def __init__(self, touch_ok: bool = True):
        self.input = FakeInput(touch_ok)
        self.dashboard = FakeDashboard()
        self._stop_evt = threading.Event()
        self.waits: list[float] = []
        # Patch wait to record and return at once.
        real = self._stop_evt.wait

        def instant(timeout=None):
            self.waits.append(timeout)
            return real(0)

        self._stop_evt.wait = instant       # type: ignore[method-assign]

    def should_stop(self) -> bool:
        return self._stop_evt.is_set()

    def request_stop(self, reason=""):
        self._stop_evt.set()


def party_after(n_polls: int, key: int = TARGET_KEY):
    """A party that gains ``key`` on the n-th read (0 = already there)."""
    calls = {"n": 0}

    def read():
        calls["n"] += 1
        return {key} if calls["n"] > n_polls else {0x1111}

    read.calls = calls          # type: ignore[attr-defined]
    return read


def fast_plan(**kw) -> catch.CatchPlan:
    base = dict(intro_taps=1, intro_gap=0.0, menu_settle=0.0,
                bag_settle=0.0, pocket_settle=0.0, throw_settle=0.0,
                confirm_window=3.0, confirm_gap=1.0, attempts=3,
                overrides={"bag": (0.1, 0.2), "balls": (0.3, 0.4),
                           "ball": (0.5, 0.6)})
    base.update(kw)
    return catch.CatchPlan(**base)


# ----------------------------------------------------------------------
# The sequence itself
# ----------------------------------------------------------------------
def test_it_touches_bag_then_balls_then_the_first_slot():
    ctx = FakeCtx()
    res = catch.catch_wild(ctx, fast_plan(), TARGET_KEY, party_after(0))

    assert res.caught
    assert ctx.input.touches == [(0.1, 0.2), (0.3, 0.4), (0.5, 0.6)]


def test_the_appearance_text_is_cleared_before_touching_the_bag():
    """Touching BAG while the 'wild X appeared' box is up does nothing."""
    ctx = FakeCtx()
    catch.catch_wild(ctx, fast_plan(intro_taps=4), TARGET_KEY,
                     party_after(0))

    assert ctx.input.taps[:4] == ["B"] * 4


def test_a_confirmed_catch_stops_throwing():
    ctx = FakeCtx()
    res = catch.catch_wild(ctx, fast_plan(), TARGET_KEY, party_after(0))

    assert res.attempts == 1
    assert len(ctx.input.touches) == 3      # exactly one throw


def test_it_retries_when_the_ball_fails():
    """A non-Master ball can break out; the turn comes back around."""
    ctx = FakeCtx()
    # Never lands in the party until the 4th poll, i.e. after retries.
    res = catch.catch_wild(ctx, fast_plan(attempts=3),
                           TARGET_KEY, party_after(4))

    assert res.caught
    assert res.attempts > 1
    assert len(ctx.input.touches) == 3 * res.attempts


def test_it_gives_up_after_the_configured_attempts():
    ctx = FakeCtx()
    res = catch.catch_wild(ctx, fast_plan(attempts=2), TARGET_KEY,
                           lambda: {0x1111})

    assert not res.caught
    assert res.attempts == 2
    assert "not confirmed" in res.detail
    assert len(ctx.input.touches) == 6


# ----------------------------------------------------------------------
# Confirmation is read from memory, not assumed from timing
# ----------------------------------------------------------------------
def test_a_catch_is_only_confirmed_from_the_party():
    ctx = FakeCtx()
    res = catch.catch_wild(ctx, fast_plan(attempts=1), TARGET_KEY,
                           lambda: set())

    assert not res.caught


def test_the_nickname_prompt_is_declined_while_confirming():
    """B answers 'no' to the nickname prompt and clears Gotcha! text."""
    ctx = FakeCtx()
    catch.catch_wild(ctx, fast_plan(intro_taps=0), TARGET_KEY,
                     party_after(2))

    assert ctx.input.taps.count("B") >= 2


def test_a_party_read_that_throws_does_not_abort_the_catch():
    ctx = FakeCtx()
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] < 3:
            raise RuntimeError("RPC hiccup")
        return {TARGET_KEY}

    res = catch.catch_wild(ctx, fast_plan(), TARGET_KEY, flaky)

    assert res.caught


def test_a_none_party_read_is_treated_as_empty():
    ctx = FakeCtx()
    res = catch.catch_wild(ctx, fast_plan(attempts=1), TARGET_KEY,
                           lambda: None)

    assert not res.caught       # and no TypeError


# ----------------------------------------------------------------------
# Failure modes that must not silently burn the encounter
# ----------------------------------------------------------------------
def test_dead_touch_input_bails_out_instead_of_retrying():
    """No point throwing five times at a window that receives nothing."""
    ctx = FakeCtx(touch_ok=False)
    res = catch.catch_wild(ctx, fast_plan(attempts=5), TARGET_KEY,
                           lambda: set())

    assert not res.caught
    assert res.attempts == 1
    assert "not reaching Azahar" in res.detail
    assert not res.ok


def test_an_unconfirmed_catch_with_working_touches_is_still_ok():
    """A full party sends the catch to a box, where we cannot see it."""
    ctx = FakeCtx()
    res = catch.catch_wild(ctx, fast_plan(attempts=1), TARGET_KEY,
                           lambda: set())

    assert not res.caught
    assert res.ok               # every touch landed; it may be in a box


def test_stopping_mid_sequence_does_not_keep_throwing():
    ctx = FakeCtx()
    ctx.request_stop("user pressed stop")

    res = catch.catch_wild(ctx, fast_plan(), TARGET_KEY, party_after(0))

    assert not res.caught
    assert ctx.input.touches == []
    assert "stopped" in res.detail


# ----------------------------------------------------------------------
# Touch geometry
# ----------------------------------------------------------------------
def test_an_override_wins_over_measured_geometry():
    fx, fy, how = catch.window_fraction("side_by_side", (0.1, 0.2),
                                        override=(0.9, 0.8))
    assert (fx, fy, how) == (0.9, 0.8, "override")


def test_geometry_falls_back_to_the_local_point_when_unmeasurable(
        monkeypatch):
    import pokebot.platform_utils as pu
    monkeypatch.setattr(pu, "find_azahar_hwnd", lambda *a, **k: 0)

    fx, fy, how = catch.window_fraction("side_by_side", (0.25, 0.12))

    assert (fx, fy) == (0.25, 0.12)
    assert how == "fallback"


def test_local_points_map_into_the_bottom_screen_side_by_side():
    """The bag buttons must land in the right-hand (touch) half."""
    from pokebot.platform_utils import bottom_screen_fraction

    for name, local in (("bag", catch.DEFAULT_BAG),
                        ("balls", catch.DEFAULT_BALLS),
                        ("ball", catch.DEFAULT_BALL)):
        fx, fy = bottom_screen_fraction(1920, 1080, "side_by_side",
                                        local[0], local[1])
        assert 0.5 < fx < 1.0, f"{name} x={fx} not on the bottom screen"
        assert 0.0 < fy < 1.0, f"{name} y={fy} off screen"


# Button rectangles read off a 1920x1080 Azahar window running
# Pokemon X/Y side-by-side, in screen pixels. The client area is the
# window minus the title bar (22), menu bar (30) and status bar (25).
_SHOT_W, _SHOT_CLIENT_H, _SHOT_TOP = 1920, 953, 52
_BUTTON_BOXES = {
    "bag":   (1075, 725, 1290, 848),    # BAG, clipped by the screen edge
    "balls": (1500, 288, 1900, 395),    # POKE BALLS pocket
    "ball":  (1080, 252, 1475, 318),    # first item row
}


@pytest.mark.parametrize("name,local", [
    ("bag", catch.DEFAULT_BAG),
    ("balls", catch.DEFAULT_BALLS),
    ("ball", catch.DEFAULT_BALL),
])
def test_the_default_points_land_inside_the_real_buttons(name, local):
    """Calibration guard.

    These fractions were derived from screenshots of the actual game.
    If someone retunes them, this fails unless the new value still
    lands on the button it is named after.
    """
    from pokebot.platform_utils import bottom_screen_fraction

    fx, fy = bottom_screen_fraction(_SHOT_W, _SHOT_CLIENT_H,
                                    "side_by_side", local[0], local[1])
    px = fx * _SHOT_W
    py = fy * _SHOT_CLIENT_H + _SHOT_TOP
    x0, y0, x1, y1 = _BUTTON_BOXES[name]
    assert x0 < px < x1, f"{name}: x={px:.0f} outside [{x0},{x1}]"
    assert y0 < py < y1, f"{name}: y={py:.0f} outside [{y0},{y1}]"


def test_the_default_points_are_distinct():
    pts = {catch.DEFAULT_BAG, catch.DEFAULT_BALLS, catch.DEFAULT_BALL}
    assert len(pts) == 3


# ----------------------------------------------------------------------
# Config parsing
# ----------------------------------------------------------------------
def test_an_empty_config_gives_the_measured_defaults():
    plan = catch.CatchPlan.from_config({})
    assert plan.bag == catch.DEFAULT_BAG
    assert plan.balls == catch.DEFAULT_BALLS
    assert plan.ball == catch.DEFAULT_BALL
    assert plan.attempts >= 1


def test_none_config_is_accepted():
    assert catch.CatchPlan.from_config(None).bag == catch.DEFAULT_BAG


def test_points_can_be_retuned_from_config():
    plan = catch.CatchPlan.from_config({"ball_local": [0.3, 0.4]})
    assert plan.ball == (0.3, 0.4)


def test_a_malformed_point_falls_back_instead_of_crashing():
    plan = catch.CatchPlan.from_config({"bag_local": "somewhere"})
    assert plan.bag == catch.DEFAULT_BAG


def test_window_overrides_are_picked_up():
    plan = catch.CatchPlan.from_config({"bag_touch": [0.61, 0.74]})
    assert plan.overrides["bag"] == (0.61, 0.74)


def test_a_malformed_override_is_ignored():
    plan = catch.CatchPlan.from_config({"bag_touch": [0.61]})
    assert "bag" not in plan.overrides


def test_attempts_can_never_be_zero():
    assert catch.CatchPlan.from_config({"catch_attempts": 0}).attempts == 1


def test_the_screen_layout_is_carried_through():
    plan = catch.CatchPlan.from_config({"screen_layout": "VERTICAL"})
    assert plan.layout == "vertical"


@pytest.mark.parametrize("bad", ["fast", None, [1, 2]])
def test_a_junk_settle_value_uses_the_default(bad):
    plan = catch.CatchPlan.from_config({"catch_throw_settle": bad})
    assert plan.throw_settle == 6.0


# ----------------------------------------------------------------------
# Wiring into the encounter loop
# ----------------------------------------------------------------------
def test_the_alert_no_longer_claims_the_bot_stopped_when_it_will_catch(
        caplog):
    """The banner used to hardcode 'Bot STOPPED'."""
    from pokebot.modes import encounter

    class Mon:
        species, nickname, exp, gender = 656, "", 1000, "F"
        pid, nature, shiny = 0x1234, "Timid", True
        ivs = {"hp": 31}

    ctx = FakeCtx()
    ctx.target = None

    with caplog.at_level("INFO"):
        encounter._alert(ctx, Mon(), 0x08800000, 7, will_catch=True)
    assert "Catching it now" in caplog.text
    assert "Bot STOPPED" not in caplog.text

    caplog.clear()
    with caplog.at_level("INFO"):
        encounter._alert(ctx, Mon(), 0x08800000, 7, will_catch=False)
    assert "Bot STOPPED" in caplog.text


def test_the_caught_event_matches_the_dashboard_signature():
    """broadcast takes **fields, not a dict -- passing one raises."""
    from pokebot.dashboard_server import DashboardServer

    DashboardServer().broadcast(
        "target_caught", species=656, shiny=True, count=42, caught=1)


def test_the_caught_event_has_a_human_readable_line():
    from pokebot.dashboard_server import _FORMATTERS

    line = _FORMATTERS["target_caught"](
        {"species": 656, "shiny": True, "count": 42, "caught": 1})
    assert "CAUGHT" in line and "656" in line


def test_settling_after_the_battle_clears_leftover_text():
    ctx = FakeCtx()
    catch.settle_after_battle(ctx, taps=3, gap=0.1, tail=1.0)
    assert ctx.input.taps == ["B", "B", "B"]


# ----------------------------------------------------------------------
# Clearing the post-throw text chain
# ----------------------------------------------------------------------
def test_the_throw_is_followed_by_a_burst_of_b_presses():
    ctx = FakeCtx()
    catch.catch_wild(ctx, fast_plan(intro_taps=0, post_throw_taps=25),
                     TARGET_KEY, party_after(0))

    # 25 after the throw, before the confirm loop starts tapping.
    assert ctx.input.taps.count("B") >= 25


# ----------------------------------------------------------------------
# Confirming the catch
# ----------------------------------------------------------------------
def test_a_growing_party_confirms_the_catch():
    """The key read back need not match the one seen in the wild slot.

    When it doesn't, the key check alone reported CATCH FAILED on a
    catch that worked, and the hunt stopped.
    """
    ctx = FakeCtx()
    calls = {"n": 0}

    def party():
        calls["n"] += 1
        # Two before the throw, three once it lands -- none of them the
        # target's key.
        return {1, 2} if calls["n"] <= 1 else {1, 2, 3}

    res = catch.catch_wild(ctx, fast_plan(intro_taps=0), TARGET_KEY, party)

    assert res.caught, res.detail


def test_a_static_party_is_still_not_a_catch():
    ctx = FakeCtx()
    res = catch.catch_wild(ctx, fast_plan(intro_taps=0, attempts=1),
                           TARGET_KEY, lambda: {1, 2})
    assert not res.caught


def test_a_shrinking_party_is_not_a_catch():
    """Guard the comparison direction."""
    ctx = FakeCtx()
    calls = {"n": 0}

    def party():
        calls["n"] += 1
        return {1, 2, 3} if calls["n"] <= 1 else {1, 2}

    res = catch.catch_wild(ctx, fast_plan(intro_taps=0, attempts=1),
                           TARGET_KEY, party)
    assert not res.caught


def test_the_key_still_confirms_the_catch():
    ctx = FakeCtx()
    res = catch.catch_wild(ctx, fast_plan(intro_taps=0),
                           TARGET_KEY, party_after(0))
    assert res.caught


def test_a_full_party_is_flagged_rather_than_called_a_failure():
    """Six in the party means the catch goes to a box, unverifiable."""
    ctx = FakeCtx()
    full = {10, 11, 12, 13, 14, 15}

    res = catch.catch_wild(ctx, fast_plan(intro_taps=0, attempts=1),
                           TARGET_KEY, lambda: full)

    assert not res.caught
    assert res.party_full
    assert "PC box" in res.detail or "box" in res.detail


def test_a_non_full_party_is_not_flagged_as_full():
    ctx = FakeCtx()
    res = catch.catch_wild(ctx, fast_plan(intro_taps=0, attempts=1),
                           TARGET_KEY, lambda: {1, 2})
    assert not res.party_full


def test_a_party_read_that_explodes_does_not_crash_the_catch():
    ctx = FakeCtx()

    def boom():
        raise RuntimeError("rpc died")

    res = catch.catch_wild(ctx, fast_plan(intro_taps=0, attempts=1),
                           TARGET_KEY, boom)
    assert not res.caught       # reported, not raised


def test_the_ball_is_confirmed_with_a_so_it_is_actually_thrown():
    """Touching the ball only selects it; A is what throws it."""
    ctx = FakeCtx()
    catch.catch_wild(ctx, fast_plan(intro_taps=0, post_throw_taps=0,
                                    attempts=1, confirm_window=0.0),
                     TARGET_KEY, party_after(0))

    assert "A" in ctx.input.taps


def test_the_throw_confirm_comes_after_the_ball_touch():
    """An A press before the ball is selected picks the wrong thing."""
    ctx = FakeCtx()
    order: list[str] = []
    real_tap, real_touch = ctx.input.tap, ctx.input.tap_touch
    ctx.input.tap = lambda b, hold_s=0.05: (order.append(b),
                                            real_tap(b, hold_s))[1]
    ctx.input.tap_touch = lambda x, y, hold_s=0.08: (
        order.append("touch"), real_touch(x, y, hold_s))[1]

    catch.catch_wild(ctx, fast_plan(intro_taps=0, post_throw_taps=0,
                                    attempts=1, confirm_window=0.0),
                     TARGET_KEY, party_after(0))

    assert order.index("A") > _last_index(order, "touch")


def _last_index(seq, value):
    return len(seq) - 1 - seq[::-1].index(value)


def test_the_throw_confirm_is_configurable():
    plan = catch.CatchPlan.from_config({"catch_throw_taps": 2,
                                        "catch_throw_button": "Y"})
    assert plan.throw_taps == 2
    assert plan.throw_button == "Y"


def test_the_throw_confirm_defaults_to_one_a_press():
    plan = catch.CatchPlan.from_config({})
    assert plan.throw_taps == 1
    assert plan.throw_button == "A"


def test_every_retry_confirms_its_own_throw():
    ctx = FakeCtx()
    res = catch.catch_wild(ctx, fast_plan(intro_taps=0, post_throw_taps=0,
                                          attempts=3, confirm_window=0.0),
                           TARGET_KEY, lambda: set())

    assert res.attempts == 3
    assert ctx.input.taps.count("A") == 3


def test_stopping_before_the_confirm_throws_nothing():
    ctx = FakeCtx()

    real = ctx.input.tap_touch

    def stop_on_ball(x, y, hold_s=0.08):
        ok = real(x, y, hold_s)
        if len(ctx.input.touches) == 3:      # the ball was just selected
            ctx.request_stop("user")
        return ok

    ctx.input.tap_touch = stop_on_ball
    res = catch.catch_wild(ctx, fast_plan(intro_taps=0), TARGET_KEY,
                           party_after(99))

    assert not res.caught
    assert "A" not in ctx.input.taps


def test_the_b_burst_comes_after_the_ball_not_before():
    """Pressing B before the ball lands backs out of the bag."""
    ctx = FakeCtx()

    order: list[str] = []
    real_tap, real_touch = ctx.input.tap, ctx.input.tap_touch
    ctx.input.tap = lambda b, hold_s=0.05: (order.append("B"),
                                            real_tap(b, hold_s))[1]
    ctx.input.tap_touch = lambda x, y, hold_s=0.08: (
        order.append(f"touch{len(ctx.input.touches)}"),
        real_touch(x, y, hold_s))[1]

    catch.catch_wild(ctx, fast_plan(intro_taps=0, post_throw_taps=25),
                     TARGET_KEY, party_after(0))

    assert order[:3] == ["touch0", "touch1", "touch2"]
    assert order[3] == "B"


# ----------------------------------------------------------------------
# Trusting the throw (Master Ball hunts)
# ----------------------------------------------------------------------
def test_an_unverified_throw_reports_caught_and_returns():
    """No party read at all: throw, clear the text, back to walking."""
    reads = []

    def party():
        reads.append(1)
        return set()

    ctx = FakeCtx()
    res = catch.catch_wild(
        ctx, fast_plan(intro_taps=0, confirm=False, attempts=5),
        TARGET_KEY, party)

    assert res.caught
    assert res.attempts == 1              # never throws a second ball
    assert reads == []                    # never read the party
    assert "not verified" in res.detail


def test_an_unverified_throw_still_throws_the_ball():
    ctx = FakeCtx()
    catch.catch_wild(ctx, fast_plan(intro_taps=0, confirm=False),
                     TARGET_KEY, lambda: set())

    assert len(ctx.input.touches) == 3     # bag, balls, ball
    assert "A" in ctx.input.taps           # and the throw confirm


def test_an_unverified_throw_clears_the_text_first():
    ctx = FakeCtx()
    catch.catch_wild(ctx, fast_plan(intro_taps=0, confirm=False,
                                    post_throw_taps=10),
                     TARGET_KEY, lambda: set())

    assert ctx.input.taps.count("B") == 10


def test_verification_stays_available():
    """Anything that can break out still needs the confirm loop."""
    ctx = FakeCtx()
    res = catch.catch_wild(ctx, fast_plan(intro_taps=0, confirm=True,
                                          attempts=2),
                           TARGET_KEY, lambda: set())
    assert not res.caught
    assert res.attempts == 2               # it retried


def test_the_shipped_config_trusts_the_throw():
    import yaml

    cfg = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config.yaml")
        .read_text(encoding="utf-8"))
    plan = catch.CatchPlan.from_config(cfg["random_encounters"])
    assert plan.confirm is False
    assert plan.post_throw_taps == 10


def test_the_library_default_still_verifies():
    """Only the shipped config opts out; the default is conservative."""
    assert catch.CatchPlan().confirm is True


def test_the_b_burst_is_configurable():
    plan = catch.CatchPlan.from_config({"catch_post_throw_taps": 7})
    assert plan.post_throw_taps == 7


def test_the_b_burst_defaults_to_ten():
    assert catch.CatchPlan.from_config({}).post_throw_taps == 10


def test_the_intro_gap_is_configurable():
    """It had no config key at all, so it could not be slowed."""
    assert catch.CatchPlan.from_config(
        {"catch_intro_gap": 0.85}).intro_gap == 0.85


def test_every_catch_gap_can_be_set_from_config():
    """Guards the gap that was missing: one unreachable key is enough
    to make 'slow the whole sequence down' quietly not work."""
    cfg = {
        "catch_intro_gap": 1.0, "catch_menu_settle": 2.0,
        "catch_bag_settle": 3.0, "catch_pocket_settle": 4.0,
        "catch_select_settle": 5.0, "catch_throw_tap_gap": 6.0,
        "catch_throw_settle": 7.0, "catch_post_throw_gap": 8.0,
    }
    p = catch.CatchPlan.from_config(cfg)
    assert (p.intro_gap, p.menu_settle, p.bag_settle, p.pocket_settle,
            p.select_settle, p.throw_tap_gap, p.throw_settle,
            p.post_throw_gap) == (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0)


def test_the_b_burst_can_be_switched_off():
    """Zero means no burst.

    The confirm loop still taps B at least once per attempt -- that is
    how it walks the party while watching -- so the assertion is about
    the burst being gone, not about silence.
    """
    ctx = FakeCtx()
    catch.catch_wild(ctx, fast_plan(intro_taps=0, post_throw_taps=0,
                                    attempts=1, confirm_window=0.0),
                     TARGET_KEY, party_after(99))
    assert ctx.input.taps.count("B") == 1     # the single confirm poll


def test_stopping_during_the_b_burst_ends_the_catch():
    ctx = FakeCtx()

    real = ctx.input.tap

    def stop_after_five(b, hold_s=0.05):
        real(b, hold_s)
        if len(ctx.input.taps) >= 5:
            ctx.request_stop("user")

    ctx.input.tap = stop_after_five
    res = catch.catch_wild(ctx, fast_plan(intro_taps=0, attempts=3),
                           TARGET_KEY, party_after(99))

    assert not res.caught
    assert res.attempts == 1                  # did not start a 2nd throw
    assert len(ctx.input.touches) == 3
