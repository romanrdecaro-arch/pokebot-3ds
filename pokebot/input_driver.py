"""
Keyboard input driver.

Azahar's scripting RPC supports memory read/write but does not expose a
controller-press API. To send button presses we drive Azahar via OS-level
keypresses, sending its configured keybinds. The user must (a) keep
Azahar focused while the bot is running, OR (b) configure pynput's
Win32/X11 backend to send keys to a specific window (more involved).

Required dependency:
    pip install pynput

If pynput is not installed, this module degrades to a no-op driver that
logs what it would have pressed; useful for dry-run testing the rest of
the bot.

Default keybinds below match Azahar's defaults; remap freely.
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

try:
    from pynput.keyboard import Controller, Key, KeyCode  # type: ignore
    PYNPUT_OK = True
except Exception:        # ImportError or backend init failure
    PYNPUT_OK = False
    Controller = None    # type: ignore
    Key = None           # type: ignore
    KeyCode = None       # type: ignore


@dataclass
class KeyBinds:
    """Mapping from logical 3DS button to keyboard key.
    Strings are pynput key names: single chars are literal, anything else
    is looked up on `pynput.keyboard.Key`."""
    A:        str = "a"
    B:        str = "s"
    X:        str = "z"
    Y:        str = "x"
    L:        str = "q"
    R:        str = "w"
    Start:    str = "m"
    Select:   str = "n"
    DpadUp:   str = "t"
    DpadDown: str = "g"
    DpadLeft: str = "f"
    DpadRight:str = "h"
    CircleUp: str = "up"
    CircleDown:  str = "down"
    CircleLeft:  str = "left"
    CircleRight: str = "right"


def _resolve_key(name: str):
    """Convert a key-bind string into a pynput key object."""
    if not PYNPUT_OK:
        return name
    if len(name) == 1:
        return KeyCode.from_char(name)
    return getattr(Key, name, None) or KeyCode.from_char(name)


class InputDriver:
    """Press buttons by name. Use within a `with` block or call .close()."""

    def __init__(self, binds: Optional[KeyBinds] = None,
                 dry_run: bool = False):
        self.binds = binds or KeyBinds()
        self.dry_run = dry_run or not PYNPUT_OK
        self._kb = Controller() if PYNPUT_OK and not dry_run else None
        # Cached Azahar window handle (Windows). Looked up lazily on
        # first key press; refreshed whenever PostMessage fails.
        self._azahar_hwnd: int = 0
        self._postmsg_warned: bool = False
        if not PYNPUT_OK:
            log.warning("pynput not available -- input driver in DRY-RUN mode. "
                        "Install with: pip install pynput")

    def close(self):
        # nothing to release; pynput Controller doesn't need it
        pass

    def __enter__(self):  return self
    def __exit__(self, *a): self.close()

    def _key_for(self, button: str):
        return _resolve_key(getattr(self.binds, button))

    # -- core operations --------------------------------------------------
    def tap(self, button: str, hold_s: float = 0.05) -> str:
        """Press and release a button. Returns the path taken:
        ``"dry"``, ``"postmessage"``, ``"pynput"``, or ``"none"``
        (no path was usable — keystroke definitely did not land).

        On Windows, posts WM_KEYDOWN/WM_KEYUP directly to Azahar's
        window so the keypress lands regardless of which app the user
        is looking at. Falls back to pynput's global keyboard
        controller on other platforms or when the hwnd lookup fails.
        """
        if self.dry_run:
            log.info(f"[DRY] tap {button} ({hold_s}s)")
            time.sleep(hold_s)
            return "dry"

        # Path A: Windows PostMessage to the Azahar window (no focus
        # required). Most reliable for emulator automation because
        # the user can keep doing other things on their PC.
        if sys.platform.startswith("win"):
            if self._send_via_postmessage(button, hold_s):
                return "postmessage"

        # Path B: pynput global keyboard. Requires Azahar to be focused.
        if self._kb is None:
            return "none"
        key = self._key_for(button)
        self._kb.press(key)
        time.sleep(hold_s)
        self._kb.release(key)
        return "pynput"

    def diagnose(self) -> dict:
        """One-shot snapshot of where keystrokes will be sent. Useful for
        logging at mode startup so the user can tell why their keys
        aren't landing.
        """
        info = {
            "dry_run": self.dry_run,
            "platform": sys.platform,
            "pynput_ok": PYNPUT_OK,
            "azahar_hwnd": 0,
        }
        if sys.platform.startswith("win"):
            try:
                from .platform_utils import find_azahar_hwnd
                info["azahar_hwnd"] = find_azahar_hwnd() or 0
            except Exception as e:
                info["hwnd_lookup_error"] = repr(e)
        return info

    def _send_via_postmessage(self, button: str, hold_s: float) -> bool:
        try:
            from .platform_utils import (
                find_azahar_hwnd, post_key_to_window, char_to_vk,
            )
        except Exception:
            return False
        char = getattr(self.binds, button, None)
        vk = char_to_vk(char) if char else None
        if vk is None:
            return False
        if not self._azahar_hwnd:
            self._azahar_hwnd = find_azahar_hwnd() or 0
        if not self._azahar_hwnd:
            if not self._postmsg_warned:
                log.warning("PostMessage path: no Azahar window found; "
                            "falling back to pynput (Azahar must be "
                            "focused for keys to land).")
                self._postmsg_warned = True
            return False
        ok = post_key_to_window(self._azahar_hwnd, vk, hold_s)
        if not ok:
            # hwnd may have gone stale (Azahar restarted); try again
            # next call.
            self._azahar_hwnd = 0
        return ok

    def hold(self, button: str):
        key = self._key_for(button)
        if self.dry_run:
            log.info(f"[DRY] hold  {button}")
            return
        self._kb.press(key)

    def release(self, button: str):
        key = self._key_for(button)
        if self.dry_run:
            log.info(f"[DRY] release {button}")
            return
        self._kb.release(key)

    def combo(self, *buttons: str, hold_s: float = 0.1):
        """Press multiple buttons together briefly, then release all."""
        keys = [self._key_for(b) for b in buttons]
        if self.dry_run:
            log.info(f"[DRY] combo {'+'.join(buttons)} ({hold_s}s)")
            time.sleep(hold_s)
            return
        for k in keys:
            self._kb.press(k)
        time.sleep(hold_s)
        for k in keys:
            self._kb.release(k)

    def soft_reset(self, hold_s: float = 0.5):
        """Standard 3DS soft-reset combo: L + R + Start (or Select)."""
        self.combo("L", "R", "Start", hold_s=hold_s)
