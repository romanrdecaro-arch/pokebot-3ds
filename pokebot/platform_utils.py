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


def focus_azahar(title_substrings=("Azahar", "Citra")) -> bool:
    """Best-effort bring-Azahar-to-front.

    Match list is intentionally narrow: just the emulator brand names.
    'Pokémon' / 'Pokemon' substrings are NOT included because they
    match every other Pokémon emulator window the user might have
    open (DeSmuME with a DS Pokémon ROM, mGBA with a GBA Pokémon ROM,
    etc.) and the bot would happily steer those instead.

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
    """Bring an Azahar/Citra window to the foreground on Windows.

    Uses the canonical 'ALT key + AttachThreadInput' trick because
    Windows by default forbids SetForegroundWindow from a process
    that didn't recently receive input. Without these workarounds
    the window will only flash in the taskbar.
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype  = ctypes.c_int
    user32.GetWindowTextW.argtypes  = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype   = ctypes.c_int
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype  = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype  = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype  = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype  = wintypes.BOOL
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype  = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD,
                                         wintypes.BOOL]
    user32.AttachThreadInput.restype  = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype  = wintypes.BOOL
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    target = [None]
    seen_titles = []

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
            seen_titles.append(title)
            # Skip our own launcher / file-explorer windows; we want the
            # one that has the game running (it'll have the version + ROM).
            lo = title.lower()
            if "pokebot" in lo:
                return True
            target[0] = hwnd
            return False
        return True

    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool,
                                  wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(EnumProc(_enum_callback), 0)

    if not target[0]:
        log.warning(f"Could not find an Azahar window to focus. "
                    f"Visible matches considered: {seen_titles!r}")
        return False

    log.info(f"Focusing Azahar window: hwnd={target[0]} "
             f"title={seen_titles[-1] if seen_titles else '?'!r}")

    # 1. ALT key press: gives us 'foreground-grant' rights.
    #    keybd_event with KEYEVENTF_KEYUP=2 to release.
    user32.keybd_event(0x12, 0, 0, 0)   # VK_MENU down
    user32.keybd_event(0x12, 0, 2, 0)   # VK_MENU up

    # 2. Restore if minimised.
    if user32.IsIconic(target[0]):
        user32.ShowWindow(target[0], 9)  # SW_RESTORE

    # 3. AttachThreadInput trick — make our thread cooperate with the
    #    target window's thread, then SetForegroundWindow is granted.
    fg_hwnd = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
    target_thread = user32.GetWindowThreadProcessId(target[0], None)
    cur_thread = kernel32.GetCurrentThreadId()

    attached = []
    if fg_thread and fg_thread != cur_thread:
        if user32.AttachThreadInput(cur_thread, fg_thread, True):
            attached.append(fg_thread)
    if target_thread and target_thread != cur_thread \
            and target_thread != fg_thread:
        if user32.AttachThreadInput(cur_thread, target_thread, True):
            attached.append(target_thread)

    user32.BringWindowToTop(target[0])
    ok = bool(user32.SetForegroundWindow(target[0]))
    user32.ShowWindow(target[0], 5)  # SW_SHOW

    for tid in attached:
        user32.AttachThreadInput(cur_thread, tid, False)

    if not ok:
        log.warning("SetForegroundWindow still returned 0 after the "
                    "ALT/AttachThreadInput workaround. Keys may still "
                    "land in the wrong window — click into Azahar once.")
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
