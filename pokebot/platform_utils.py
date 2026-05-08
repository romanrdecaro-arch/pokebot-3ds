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


def find_azahar_hwnd(title_substrings=("Azahar", "Citra")) -> int:
    """Return Azahar's top-level window handle, or 0 if not found.

    Used by the input driver to PostMessage key events directly to
    Azahar without needing it to be the foreground window.
    Windows-only; returns 0 on other platforms.
    """
    if not sys.platform.startswith("win"):
        return 0
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return 0
    user32 = ctypes.windll.user32
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype  = ctypes.c_int
    user32.GetWindowTextW.argtypes  = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype   = ctypes.c_int
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype  = wintypes.BOOL

    found = [0]

    def _cb(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value or ""
        if any(s in title for s in title_substrings) and "pokebot" not in title.lower():
            found[0] = int(hwnd)
            return False
        return True

    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool,
                                  wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(EnumProc(_cb), 0)
    return found[0]


def click_window(hwnd: int) -> bool:
    """Click the centre of the window. See click_window_at for details."""
    return click_window_at(hwnd, 0.5, 0.5)


def click_window_at(hwnd: int, x_frac: float, y_frac: float,
                    hold_s: float = 0.05) -> bool:
    """Synthetic left-click at fractional coords (0..1) of the window.

    Used both to "wake up" Qt's input routing and to drive 3DS touch
    input. Tries two paths:

      1. SendInput hardware mouse click: SetCursorPos to the target
         then SendInput LBUTTONDOWN/UP. Cursor briefly moves but the
         click is real-mouse-equivalent so Qt always processes it.
      2. PostMessage WM_LBUTTONDOWN/UP fallback (no cursor move).

    Returns True when at least one path posted, False on non-Windows
    or when the hwnd is invalid.
    """
    if not hwnd or not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False
    user32 = ctypes.windll.user32
    user32.GetClientRect.argtypes = [wintypes.HWND,
                                     ctypes.POINTER(wintypes.RECT)]
    user32.GetClientRect.restype  = wintypes.BOOL
    user32.ClientToScreen.argtypes = [wintypes.HWND,
                                      ctypes.POINTER(wintypes.POINT)]
    user32.ClientToScreen.restype  = wintypes.BOOL

    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return False
    w = max(1, rect.right - rect.left)
    h = max(1, rect.bottom - rect.top)
    cx = max(1, min(w - 1, int(round(w * float(x_frac)))))
    cy = max(1, min(h - 1, int(round(h * float(y_frac)))))

    # --- Path 1: SendInput hardware mouse click ----------------------------
    # Convert client (cx, cy) → screen coords for SetCursorPos.
    pt = wintypes.POINT(cx, cy)
    if user32.ClientToScreen(hwnd, ctypes.byref(pt)):
        try:
            _send_mouse_click(pt.x, pt.y, hold_s)
            return True
        except Exception:
            pass  # fall through to PostMessage

    # --- Path 2: PostMessage fallback --------------------------------------
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONUP   = 0x0202
    MK_LBUTTON     = 0x0001
    lparam = (cx & 0xFFFF) | ((cy & 0xFFFF) << 16)
    user32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lparam)
    import time as _t
    _t.sleep(hold_s)
    user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lparam)
    return True


def _send_mouse_click(screen_x: int, screen_y: int, hold_s: float) -> None:
    """SetCursorPos + SendInput for a real left-click at screen coords."""
    import ctypes
    from ctypes import wintypes
    import time as _t

    user32 = ctypes.windll.user32

    # MOUSEINPUT struct
    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx",          wintypes.LONG),
                    ("dy",          wintypes.LONG),
                    ("mouseData",   wintypes.DWORD),
                    ("dwFlags",     wintypes.DWORD),
                    ("time",        wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_void_p)]

    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    INPUT_MOUSE = 0
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP   = 0x0004

    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos(int(screen_x), int(screen_y))
    _t.sleep(0.02)

    def _make_input(flags):
        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.u.mi = MOUSEINPUT(0, 0, 0, flags, 0, None)
        return inp

    user32.SendInput.argtypes = [ctypes.c_uint,
                                 ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype  = ctypes.c_uint

    arr = (INPUT * 1)(_make_input(MOUSEEVENTF_LEFTDOWN))
    user32.SendInput(1, arr, ctypes.sizeof(INPUT))
    _t.sleep(hold_s)
    arr = (INPUT * 1)(_make_input(MOUSEEVENTF_LEFTUP))
    user32.SendInput(1, arr, ctypes.sizeof(INPUT))


def post_key_to_window(hwnd: int, vk_code: int, hold_s: float = 0.05) -> bool:
    """PostMessage WM_KEYDOWN/WM_KEYUP to a window. Bypasses focus.

    Returns True when both messages were posted, False on non-Windows
    or when the hwnd is invalid. Not affected by which window the user
    is currently looking at — keys go straight into Azahar's message
    queue.
    """
    if not hwnd or not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
    except Exception:
        return False
    user32 = ctypes.windll.user32
    WM_KEYDOWN = 0x0100
    WM_KEYUP   = 0x0101
    # lParam encoding: bit 0-15 repeat count, 16-23 scan code (0 OK),
    # 30 prev key state (0=up→down for KEYDOWN, 1 for KEYUP).
    user32.PostMessageW(hwnd, WM_KEYDOWN, vk_code, 0x00000001)
    import time as _t
    _t.sleep(hold_s)
    user32.PostMessageW(hwnd, WM_KEYUP,   vk_code, 0xC0000001)
    return True


_SPECIAL_VK = {
    "left":      0x25,    "up":         0x26,
    "right":     0x27,    "down":       0x28,
    "space":     0x20,    "enter":      0x0D,
    "tab":       0x09,    "esc":        0x1B,
    "backspace": 0x08,    "home":       0x24,
    "end":       0x23,    "page_up":    0x21,
    "page_down": 0x22,
    "shift":     0x10,    "ctrl":       0x11,
    "alt":       0x12,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},  # F1..F12 = 0x70..0x7B
}


def char_to_vk(ch: str):
    """Return the Win32 virtual-key code for a bind name.

    Accepts:
      - single ASCII letter / digit ("a", "F", "5") → 0x30..0x5A
      - special-key names ("left", "space", "f1") → matching VK_* code

    Returns None when nothing matches (input driver falls back to
    pynput, which knows about the same special-key names natively).
    """
    if not ch:
        return None
    if len(ch) == 1:
        c = ch.upper()
        if 'A' <= c <= 'Z' or '0' <= c <= '9':
            return ord(c)
        return None
    return _SPECIAL_VK.get(ch.lower())


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

    # Synthetic click into the window's client area. Qt apps often
    # gate keyboard input routing on having received an actual click;
    # without this, the user has to click into Azahar manually before
    # the bot's keypresses register.
    click_window(target[0])

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
