"""
OS-level helpers we'd rather not reinvent.

Currently houses one thing: ``focus_azahar()``, which finds Azahar's
emulator window and brings it to the foreground so subsequent
keystrokes land in the right window. Without it, the bot's keys go
to whatever the user clicked last (usually the launcher).
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)


def focus_azahar(title_substrings=("Azahar", "Citra", "Pokémon",
                                    "Pokemon")) -> bool:
    """Best-effort bring-Azahar-to-front.

    Returns True if a matching window was found and a foreground
    request was issued, False otherwise. Always non-fatal — if the
    OS denies the focus change, we just log and move on.
    """
    if sys.platform.startswith("win"):
        return _focus_windows(title_substrings)
    if sys.platform == "darwin":
        return _focus_macos()
    if sys.platform.startswith("linux"):
        return _focus_linux(title_substrings)
    return False


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

def _focus_windows(title_substrings) -> bool:
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False

    user32 = ctypes.windll.user32
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype  = ctypes.c_int
    user32.GetWindowTextW.argtypes  = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype   = ctypes.c_int
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype  = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype  = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype  = wintypes.BOOL

    target = [None]

    def _enum_callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value or ""
        if any(s in title for s in title_substrings):
            target[0] = hwnd
            return False
        return True

    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool,
                                  wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(EnumProc(_enum_callback), 0)

    if not target[0]:
        log.warning("Could not find an Azahar window to focus.")
        return False

    # SW_RESTORE = 9 — restore from minimised in case the user collapsed it.
    user32.ShowWindow(target[0], 9)
    ok = bool(user32.SetForegroundWindow(target[0]))
    if not ok:
        # Windows occasionally refuses SetForegroundWindow when the
        # caller didn't last receive input. AttachThreadInput is the
        # workaround. Keep it simple — log and continue; the user can
        # alt-tab once and the bot still works.
        log.info("SetForegroundWindow returned 0; window may flash in "
                 "the taskbar instead of foregrounding. Click into "
                 "Azahar once if keys aren't landing.")
    return True


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------

def _focus_macos() -> bool:
    import subprocess
    for app in ("Azahar", "Citra"):
        try:
            r = subprocess.run(
                ["osascript", "-e", f'tell application "{app}" to activate'],
                capture_output=True, timeout=2)
            if r.returncode == 0:
                return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# Linux (X11 only — Wayland needs platform-specific tools)
# ---------------------------------------------------------------------------

def _focus_linux(title_substrings) -> bool:
    import subprocess
    for sub in title_substrings:
        try:
            r = subprocess.run(["wmctrl", "-a", sub],
                               capture_output=True, timeout=2)
            if r.returncode == 0:
                return True
        except FileNotFoundError:
            return False
        except Exception:
            continue
    return False
