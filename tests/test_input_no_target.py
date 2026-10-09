"""
With no Azahar window, the input driver presses NOTHING.

When Azahar crashed in the middle of a USUM hunt, every input path fell
back to pynput's global keyboard -- which types into whatever window has
focus. For nine minutes the bot sent L+R+Start ("q", "w", "m") and a
stream of A and Left into the desktop, until its timeouts ran out.

The fallback still exists for what it was written for: a window that is
there but will not take posted messages.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot import input_driver, platform_utils  # noqa: E402

pytestmark = pytest.mark.skipif(not sys.platform.startswith("win"),
                                reason="PostMessage path is Windows-only")


class Keyboard:
    """Stands in for pynput's controller and records what it typed."""

    def __init__(self):
        self.typed = []

    def press(self, key):
        self.typed.append(("down", key))

    def release(self, key):
        self.typed.append(("up", key))


@pytest.fixture
def driver(monkeypatch):
    drv = input_driver.InputDriver(binds=input_driver.KeyBinds(),
                                   dry_run=False)
    drv.dry_run = False
    kb = Keyboard()
    drv._kb = kb
    drv.typed = kb.typed                       # type: ignore[attr-defined]
    return drv


def no_window(monkeypatch):
    monkeypatch.setattr(platform_utils, "find_azahar_hwnd", lambda *a: 0)


def test_a_tap_with_no_azahar_types_nothing(driver, monkeypatch):
    no_window(monkeypatch)
    assert driver.tap("A", hold_s=0.0) == "none"
    assert driver.typed == []


def test_the_soft_reset_with_no_azahar_types_nothing(driver, monkeypatch):
    """L+R+Start into a focused text box would be q, w and m."""
    no_window(monkeypatch)
    driver.soft_reset(hold_s=0.0)
    assert driver.typed == []


def test_a_hold_with_no_azahar_types_nothing(driver, monkeypatch):
    no_window(monkeypatch)
    assert driver.hold("DpadLeft") is False
    assert driver.typed == []


def test_running_with_no_azahar_types_nothing(driver, monkeypatch):
    no_window(monkeypatch)
    assert driver.move_running("DpadUp", hold_s=0.0) == "none"
    assert driver.typed == []


def test_target_alive_says_so(driver, monkeypatch):
    no_window(monkeypatch)
    assert driver.target_alive() is False
    monkeypatch.setattr(platform_utils, "find_azahar_hwnd", lambda *a: 4242)
    assert driver.target_alive() is True


def test_a_window_that_refuses_posts_still_gets_the_fallback(driver,
                                                             monkeypatch):
    """The case the pynput path exists for is unchanged."""
    monkeypatch.setattr(platform_utils, "find_azahar_hwnd", lambda *a: 4242)
    monkeypatch.setattr(platform_utils, "post_key_to_window",
                        lambda *a, **k: False)
    assert driver.tap("A", hold_s=0.0) == "pynput"
    assert [d for d, _ in driver.typed] == ["down", "up"]
