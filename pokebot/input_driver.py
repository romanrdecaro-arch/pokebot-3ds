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
from dataclasses import dataclass
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


#: Every logical button name the driver accepts. Anything else is a
#: caller bug — see ``InputDriver._key_for``.
BUTTON_NAMES = frozenset(KeyBinds.__dataclass_fields__)


def _check_button(button) -> None:
    """Fail fast, and legibly, on a bad button name.

    Without this the error surfaces as ``TypeError: attribute name must
    be string`` from deep inside ``getattr``/pynput, which says nothing
    about which caller passed what. A caller that loses track of its
    button variable is a bug worth reporting clearly.
    """
    if button not in BUTTON_NAMES:
        raise ValueError(
            f"unknown button {button!r} (type {type(button).__name__}); "
            f"expected one of: {', '.join(sorted(BUTTON_NAMES))}"
        )


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
        # Keys currently held down, so close() can let go of them. A
        # latched key survives the bot process and leaves Azahar stuck
        # running or cancelling.
        self._held_keys: set = set()
        self._held_vks: set = set()
        if not PYNPUT_OK:
            log.warning("pynput not available -- input driver in DRY-RUN mode. "
                        "Install with: pip install pynput")

    def close(self):
        """Release anything still held down.

        This used to be a no-op on the theory that pynput needs no
        cleanup — but a key held via ``hold()``/``move_running()`` stays
        latched in the *emulator* after the bot exits, so Azahar keeps
        running/cancelling until the user presses it by hand.
        """
        for key in list(self._held_keys):
            try:
                if self._kb is not None:
                    self._kb.release(key)
            except Exception as exc:
                log.warning(f"could not release a held key on close: {exc}")
        self._held_keys.clear()

        for vk in list(self._held_vks):
            try:
                from .platform_utils import post_key_up
                post_key_up(self._azahar_hwnd, vk)
            except Exception as exc:
                log.warning(f"could not release a held vk on close: {exc}")
        self._held_vks.clear()

    def __enter__(self):  return self
    def __exit__(self, *a): self.close()

    def _key_for(self, button: str):
        _check_button(button)
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

    def tap_touch(self, x_frac: float, y_frac: float,
                  hold_s: float = 0.06) -> bool:
        """Click on Azahar's bottom screen at fractional window coords.

        ``x_frac=0.5, y_frac=0.92`` lands on the RUN button in Pokémon
        X/Y's wild-battle UI when Azahar is in its default vertical
        layout (top screen above bottom). Returns True on success,
        False when the bot can't find the Azahar window or PostMessage
        is unavailable. No pynput fallback — touch input has no
        global keyboard equivalent.
        """
        if self.dry_run or not sys.platform.startswith("win"):
            log.info(f"[DRY] touch ({x_frac:.2f}, {y_frac:.2f}) "
                     f"({hold_s}s)")
            return False
        try:
            from .platform_utils import find_azahar_hwnd, click_window_at
        except Exception:
            return False
        if not self._azahar_hwnd:
            self._azahar_hwnd = find_azahar_hwnd() or 0
        if not self._azahar_hwnd:
            return False
        ok = click_window_at(self._azahar_hwnd, x_frac, y_frac, hold_s)
        if not ok:
            self._azahar_hwnd = 0
        return ok

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

    def move_running(self, direction: str, hold_s: float = 0.35) -> str:
        """Press ``direction`` while B is held down → the player RUNS
        (Gen 6 Running Shoes). PostMessage path (no focus needed)
        preferred; pynput fallback (Azahar must be focused)."""
        if self.dry_run:
            log.info(f"[DRY] run {direction} ({hold_s}s, B held)")
            time.sleep(hold_s)
            return "dry"

        if sys.platform.startswith("win"):
            try:
                from .platform_utils import (
                    find_azahar_hwnd, post_key_to_window,
                    post_key_down, post_key_up, char_to_vk,
                )
                b_char = getattr(self.binds, "B", None)
                d_char = getattr(self.binds, direction, None)
                b_vk = char_to_vk(b_char) if b_char else None
                d_vk = char_to_vk(d_char) if d_char else None
                if not self._azahar_hwnd:
                    self._azahar_hwnd = find_azahar_hwnd() or 0
                if self._azahar_hwnd and b_vk and d_vk:
                    post_key_down(self._azahar_hwnd, b_vk)
                    try:
                        post_key_to_window(self._azahar_hwnd, d_vk,
                                           hold_s)
                    finally:
                        post_key_up(self._azahar_hwnd, b_vk)
                    return "postmessage"
            except Exception:
                pass

        # pynput fallback — needs Azahar focused.
        if self._kb is None:
            # Last resort: at least move (walk) so the bot isn't stuck.
            return self.tap(direction, hold_s=hold_s)
        bkey = self._key_for("B")
        dkey = self._key_for(direction)
        # try/finally, because a raise between the presses used to
        # leave B latched down in the emulator for the rest of the
        # run — the player then runs/cancels through every subsequent
        # menu and the hunt quietly goes haywire.
        try:
            self._press(bkey)
            self._press(dkey)
            time.sleep(hold_s)
        finally:
            self._release(dkey)
            self._release(bkey)
        return "pynput"

    # -- held-key bookkeeping ---------------------------------------------
    def _press(self, key) -> None:
        self._kb.press(key)
        self._held_keys.add(key)

    def _release(self, key) -> None:
        self._held_keys.discard(key)
        self._kb.release(key)

    def hold(self, button: str):
        key = self._key_for(button)
        if self.dry_run:
            log.info(f"[DRY] hold  {button}")
            return
        self._press(key)

    def release(self, button: str):
        key = self._key_for(button)
        if self.dry_run:
            log.info(f"[DRY] release {button}")
            return
        self._release(key)

    def combo(self, *buttons: str, hold_s: float = 0.1):
        """Press multiple buttons together briefly, then release all."""
        keys = [self._key_for(b) for b in buttons]
        if self.dry_run:
            log.info(f"[DRY] combo {'+'.join(buttons)} ({hold_s}s)")
            time.sleep(hold_s)
            return
        try:
            for k in keys:
                self._press(k)
            time.sleep(hold_s)
        finally:
            # Release even if a press partway through raised, so a
            # failed L+R+Start doesn't leave L latched.
            for k in keys:
                self._release(k)

    def soft_reset(self, hold_s: float = 0.5):
        """3DS soft-reset combo: L + R + Start held together.

        Prefer the PostMessage path on Windows — pynput's SendInput
        wrapper crashes on Python 3.14 (ctypes signature mismatch),
        and PostMessage doesn't need Azahar focused either way.
        """
        if self.dry_run:
            log.info(f"[DRY] soft_reset ({hold_s}s)")
            time.sleep(hold_s)
            return
        if sys.platform.startswith("win"):
            try:
                from .platform_utils import (
                    find_azahar_hwnd, post_key_down, post_key_up,
                    char_to_vk,
                )
                vks = [char_to_vk(getattr(self.binds, b, None))
                       for b in ("L", "R", "Start")]
                if not self._azahar_hwnd:
                    self._azahar_hwnd = find_azahar_hwnd() or 0
                if self._azahar_hwnd and all(vks):
                    try:
                        for vk in vks:
                            post_key_down(self._azahar_hwnd, vk)
                            self._held_vks.add(vk)
                        time.sleep(hold_s)
                    finally:
                        # Without this, a failure between the downs and
                        # the ups fell through to the pynput combo below
                        # with L and R still latched in Azahar.
                        for vk in vks:
                            post_key_up(self._azahar_hwnd, vk)
                            self._held_vks.discard(vk)
                    return
            except Exception as e:
                log.warning(f"  soft_reset PostMessage failed ({e}); "
                            f"falling back to pynput.")
        # pynput fallback (needs focus; may crash on Python 3.14 —
        # the PostMessage path above is the primary route).
        self.combo("L", "R", "Start", hold_s=hold_s)
