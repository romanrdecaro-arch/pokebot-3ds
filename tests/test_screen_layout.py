"""
Tests for locating the touch screen inside the Azahar window.

Every touch the bot makes — RUN, BAG, POKE BALLS, the ball — is a
fraction of the window computed from the screen layout. Get the layout
wrong and all of them land in empty space: no error, no failed read,
the catch just silently does nothing. That is exactly what happened on
a second PC, where Azahar was on its default layout while config.yaml
still claimed the side-by-side one set by hand on the first machine.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot import platform_utils as pu  # noqa: E402


# The bottom-screen-local points the catch sequence uses.
BAG = (0.135, 0.86)
BALLS = (0.743, 0.205)
BALL = (0.245, 0.117)


# ----------------------------------------------------------------------
# Resolving which layout to use
# ----------------------------------------------------------------------
@pytest.mark.parametrize("option,expected", [
    (0, "vertical"),        # a fresh Azahar install
    (1, "vertical"),        # single screen — no separate bottom rect
    (2, "large_screen"),
    (3, "side_by_side"),
    (4, "vertical"),
    (5, "large_screen"),
])
def test_azahars_layout_option_picks_the_geometry(option, expected,
                                                  monkeypatch):
    monkeypatch.setattr(pu, "_LAYOUT_BY_OPTION", pu._LAYOUT_BY_OPTION)
    monkeypatch.setattr(
        "pokebot.azahar_config.load_screen_layout",
        lambda: {"layout_option": option, "swap_screen": False})
    assert pu.resolve_layout("auto") == (expected, False)


def test_swap_screen_is_carried_through(monkeypatch):
    monkeypatch.setattr(
        "pokebot.azahar_config.load_screen_layout",
        lambda: {"layout_option": 3, "swap_screen": True})
    assert pu.resolve_layout("auto") == ("side_by_side", True)


def test_an_explicit_layout_overrides_detection(monkeypatch):
    """Someone who pins a value means it."""
    monkeypatch.setattr(
        "pokebot.azahar_config.load_screen_layout",
        lambda: {"layout_option": 0, "swap_screen": False})
    assert pu.resolve_layout("side_by_side") == ("side_by_side", False)


def test_auto_is_the_default_when_nothing_is_configured(monkeypatch):
    monkeypatch.setattr(
        "pokebot.azahar_config.load_screen_layout",
        lambda: {"layout_option": 3, "swap_screen": False})
    assert pu.resolve_layout(None)[0] == "side_by_side"
    assert pu.resolve_layout("")[0] == "side_by_side"


def test_an_unknown_option_falls_back_rather_than_raising(monkeypatch):
    monkeypatch.setattr(
        "pokebot.azahar_config.load_screen_layout",
        lambda: {"layout_option": 99, "swap_screen": False})
    assert pu.resolve_layout("auto") == ("vertical", False)


def test_an_unreadable_config_falls_back_rather_than_raising(monkeypatch):
    def boom():
        raise OSError("no qt-config.ini")

    monkeypatch.setattr("pokebot.azahar_config.load_screen_layout", boom)
    assert pu.resolve_layout("auto") == ("vertical", False)


# ----------------------------------------------------------------------
# The geometry itself
# ----------------------------------------------------------------------
@pytest.mark.parametrize("layout", ["vertical", "side_by_side",
                                    "large_screen"])
@pytest.mark.parametrize("point", [BAG, BALLS, BALL])
def test_every_touch_lands_inside_the_window(layout, point):
    fx, fy = pu.bottom_screen_fraction(1920, 1010, layout, *point)
    assert 0.0 <= fx <= 1.0
    assert 0.0 <= fy <= 1.0


@pytest.mark.parametrize("layout", ["vertical", "side_by_side",
                                    "large_screen"])
def test_geometry_is_resolution_independent(layout):
    """Same point, three window sizes, same fractions."""
    a = pu.bottom_screen_fraction(1920, 1010, layout, *BAG)
    b = pu.bottom_screen_fraction(960, 505, layout, *BAG)
    assert a == pytest.approx(b)


def test_the_layouts_disagree_enough_to_miss_every_button():
    """The bug in one assertion.

    If these were close, a wrong layout would still hit. They are not:
    BAG moves by a quarter of the window, which is why the catch did
    nothing at all rather than working unreliably.
    """
    sbs = pu.bottom_screen_fraction(1920, 1010, "side_by_side", *BAG)
    ver = pu.bottom_screen_fraction(1920, 1010, "vertical", *BAG)
    assert abs(sbs[0] - ver[0]) > 0.2


def test_side_by_side_puts_the_touch_screen_on_the_right():
    fx, _ = pu.bottom_screen_fraction(1920, 1010, "side_by_side", *BAG)
    assert fx > 0.5


def test_vertical_puts_the_touch_screen_on_the_bottom():
    _, fy = pu.bottom_screen_fraction(1920, 1010, "vertical", *BAG)
    assert fy > 0.5


def test_large_screen_puts_the_touch_screen_bottom_right():
    fx, fy = pu.bottom_screen_fraction(1920, 1010, "large_screen", *BAG)
    assert fx > 0.7 and fy > 0.5


# ----------------------------------------------------------------------
# Swapped screens
# ----------------------------------------------------------------------
def test_swapping_side_by_side_moves_the_touch_screen_left():
    normal = pu.bottom_screen_fraction(1920, 1010, "side_by_side", *BAG)
    swapped = pu.bottom_screen_fraction(1920, 1010, "side_by_side",
                                        *BAG, swap=True)
    assert normal[0] > 0.5 > swapped[0]


def test_swapping_vertical_moves_the_touch_screen_up():
    normal = pu.bottom_screen_fraction(1920, 1010, "vertical", *BAG)
    swapped = pu.bottom_screen_fraction(1920, 1010, "vertical",
                                        *BAG, swap=True)
    assert normal[1] > 0.5 > swapped[1]


def test_swapping_keeps_the_touch_inside_the_window():
    for layout in ("vertical", "side_by_side", "large_screen"):
        fx, fy = pu.bottom_screen_fraction(1920, 1010, layout, *BALLS,
                                           swap=True)
        assert 0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0, layout


# ----------------------------------------------------------------------
# The shipped config
# ----------------------------------------------------------------------
def test_the_shipped_config_detects_rather_than_assumes():
    """A pinned layout is a claim about someone else's machine."""
    import yaml

    cfg = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["random_encounters"]["screen_layout"] == "auto"
