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
    def tap(self, button: str, hold_s: float = 0.05):
        """Press and release a button."""
        key = self._key_for(button)
        if self.dry_run:
            log.info(f"[DRY] tap {button} ({hold_s}s)")
            time.sleep(hold_s)
            return
        self._kb.press(key)
        time.sleep(hold_s)
        self._kb.release(key)

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
