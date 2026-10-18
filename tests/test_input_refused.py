"""
When Windows refuses a posted key, the bot says so.

PostMessage into a window owned by a process at a higher integrity level
fails with ERROR_ACCESS_DENIED -- User Interface Privilege Isolation.
That is Azahar started "as administrator" while the bot was not. Every
key is dropped, the pynput fallback is blocked the same way, and the hunt
used to press into nothing with no clue in the log (issue #3).

And a soft reset whose key-downs were not taken used to return as if
L+R+Start had gone out.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot import input_driver, platform_utils  # noqa: E402


class RefusingUser32:
    """PostMessageW that always fails."""

    def __init__(self):
        self.calls = 0

    def PostMessageW(self, *args):                       # noqa: N802
        self.calls += 1
        return 0


@pytest.fixture
def refused(monkeypatch):
    fake = RefusingUser32()
    monkeypatch.setattr(platform_utils, "_posting_user32", lambda: fake)
    monkeypatch.setattr(platform_utils, "_window_is_alive", lambda h: True)
    monkeypatch.setattr(platform_utils, "_elevation_warned", False)
    return fake


def elevation_errors(caplog):
    return [r for r in caplog.records
            if r.levelno >= logging.ERROR and "administrator" in
            r.getMessage()]


def test_access_denied_is_reported_as_azahar_running_elevated(
        refused, monkeypatch, caplog):
    monkeypatch.setattr(platform_utils, "_last_error", lambda: 5)
    with caplog.at_level(logging.INFO):
        assert platform_utils.post_key_to_window(1234, 0x41, 0.0) is False
    assert len(elevation_errors(caplog)) == 1


def test_it_is_said_once_not_on_every_press(refused, monkeypatch, caplog):
    monkeypatch.setattr(platform_utils, "_last_error", lambda: 5)
    with caplog.at_level(logging.INFO):
        for _ in range(5):
            platform_utils.post_key_down(1234, 0x41)
            platform_utils.post_key_up(1234, 0x41)
    assert len(elevation_errors(caplog)) == 1


def test_other_failures_do_not_blame_elevation(refused, monkeypatch,
                                               caplog):
    monkeypatch.setattr(platform_utils, "_last_error", lambda: 1400)
    with caplog.at_level(logging.INFO):
        assert platform_utils.post_key_down(1234, 0x41) is False
    assert elevation_errors(caplog) == []


@pytest.mark.skipif(not sys.platform.startswith("win"),
                    reason="real PostMessageW")
def test_the_real_post_path_reports_a_dead_window_without_blaming_elevation(
        monkeypatch, caplog):
    """The fakes above never run the real argtypes or last-error code."""
    monkeypatch.setattr(platform_utils, "_elevation_warned", False)
    with caplog.at_level(logging.INFO):
        ok = platform_utils._post_message(0x7FFF1234, 0x0400, 0x41,
                                          0xC0000001)
        err = platform_utils._last_error()
    assert ok is False
    assert err == 1400                       # ERROR_INVALID_WINDOW_HANDLE
    assert elevation_errors(caplog) == []


class Keyboard:
    """Stands in for pynput's controller and records what it typed."""

    def __init__(self):
        self.typed = []

    def press(self, key):
        self.typed.append(("down", key))

    def release(self, key):
        self.typed.append(("up", key))


def refusing_driver(monkeypatch):
    """A driver whose posts are refused for elevation, with a keyboard
    standing by that must never be used."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(platform_utils, "find_azahar_hwnd", lambda *a: 1234)
    monkeypatch.setattr(platform_utils, "post_key_to_window",
                        lambda h, vk, s=0.05: False)
    monkeypatch.setattr(platform_utils, "post_key_down", lambda h, vk: False)
    monkeypatch.setattr(platform_utils, "post_key_up", lambda h, vk: False)
    monkeypatch.setattr(platform_utils, "_elevation_warned", True)
    drv = input_driver.InputDriver(binds=input_driver.KeyBinds(),
                                   dry_run=False)
    drv.dry_run = False
    drv._kb = Keyboard()
    return drv


def test_a_refused_tap_does_not_type_into_the_focused_window(monkeypatch):
    drv = refusing_driver(monkeypatch)
    assert drv.tap("A", hold_s=0.0) == "refused"
    assert drv._kb.typed == []


def test_a_refused_hold_does_not_type_into_the_focused_window(monkeypatch):
    drv = refusing_driver(monkeypatch)
    assert drv.hold("DpadLeft") is False
    assert drv._kb.typed == []


def test_a_refused_run_does_not_type_into_the_focused_window(monkeypatch):
    drv = refusing_driver(monkeypatch)
    assert drv.move_running("DpadUp", hold_s=0.0) == "refused"
    assert drv._kb.typed == []


def test_a_refused_soft_reset_does_not_type_into_the_focused_window(
        monkeypatch):
    drv = refusing_driver(monkeypatch)
    drv.soft_reset(hold_s=0.0)
    assert drv._kb.typed == []


def test_a_soft_reset_that_did_not_go_out_says_so(monkeypatch, caplog):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(platform_utils, "find_azahar_hwnd", lambda *a: 1234)
    monkeypatch.setattr(platform_utils, "post_key_down", lambda h, vk: False)
    monkeypatch.setattr(platform_utils, "post_key_up", lambda h, vk: False)
    drv = input_driver.InputDriver(binds=input_driver.KeyBinds(),
                                   dry_run=False)
    drv.dry_run = False
    drv._kb = None
    with caplog.at_level(logging.WARNING, logger="pokebot.input_driver"):
        drv.soft_reset(hold_s=0.0)
    assert any("soft_reset" in r.getMessage()
               and "did not" in r.getMessage() for r in caplog.records)
    assert drv._held_vks == set()
    assert drv._azahar_hwnd == 0


def test_a_stale_window_handle_gets_one_retry(monkeypatch, caplog):
    """Azahar restarted: the old handle refuses, the new one takes it."""
    monkeypatch.setattr(sys, "platform", "win32")
    handles = iter([1111, 2222])
    monkeypatch.setattr(platform_utils, "find_azahar_hwnd",
                        lambda *a: next(handles))
    downs = []

    def down(hwnd, vk):
        downs.append(hwnd)
        return hwnd == 2222

    monkeypatch.setattr(platform_utils, "post_key_down", down)
    monkeypatch.setattr(platform_utils, "post_key_up", lambda h, vk: True)
    drv = input_driver.InputDriver(binds=input_driver.KeyBinds(),
                                   dry_run=False)
    drv.dry_run = False
    drv._kb = None
    with caplog.at_level(logging.WARNING, logger="pokebot.input_driver"):
        drv.soft_reset(hold_s=0.0)
    assert downs == [1111] * 3 + [2222] * 3
    assert caplog.records == []
    assert drv._azahar_hwnd == 2222


@pytest.mark.skipif(not sys.platform.startswith("win"),
                    reason="scripts/test_input.py is Windows-only")
def test_the_input_test_script_posts_the_vk_the_bot_posts():
    """It once turned "left" into the L key via VkKeyScanW("l")."""
    import test_input as script

    assert script.vk_for("left") == 0x25
    assert script.vk_for("a") == 0x41
    assert script.vk_for("m") == 0x4D


def test_a_soft_reset_that_went_out_is_quiet(monkeypatch, caplog):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(platform_utils, "find_azahar_hwnd", lambda *a: 1234)
    monkeypatch.setattr(platform_utils, "post_key_down", lambda h, vk: True)
    monkeypatch.setattr(platform_utils, "post_key_up", lambda h, vk: True)
    drv = input_driver.InputDriver(binds=input_driver.KeyBinds(),
                                   dry_run=False)
    drv.dry_run = False
    drv._kb = None
    with caplog.at_level(logging.WARNING, logger="pokebot.input_driver"):
        drv.soft_reset(hold_s=0.0)
    assert caplog.records == []
