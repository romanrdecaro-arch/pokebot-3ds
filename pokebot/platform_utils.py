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


# ---------------------------------------------------------------------------
# Which Azahar window does THIS process drive?
#
# One bot process drives exactly one emulator window, so the target is
# process-wide state rather than an argument threaded through every
# call site. run.py sets it once from --window-pid / --window-title;
# every find_azahar_hwnd() and focus_azahar() call then honours it
# automatically. Left unset, behaviour is unchanged: first match wins.
# ---------------------------------------------------------------------------

_TARGET_PID: int | None = None
_TARGET_TITLE: str | None = None

#: How synthetic clicks are delivered.
#:
#: "restore" — real hardware click, then the pointer is put straight
#:             back where it was. Default. Azahar (Qt) reliably honours
#:             a real click; posted mouse messages it may ignore
#:             entirely, so this is the option that both works and
#:             leaves your mouse where you left it. The pointer does
#:             jump for the duration of the click (~50 ms).
#: "post"    — PostMessage WM_LBUTTONDOWN/UP to the window only. The
#:             pointer genuinely never moves, but the app may ignore
#:             the messages: verified NOT delivered to a Tk window
#:             here, and untested against Azahar. Try it, and if
#:             fleeing stops working, go back to "restore".
#: "cursor"  — legacy: click and LEAVE the pointer on the button.
_MOUSE_MODE = "restore"


def set_mouse_mode(mode: str) -> None:
    """Choose how clicks are delivered. See ``_MOUSE_MODE``."""
    global _MOUSE_MODE
    if mode not in ("post", "cursor", "restore"):
        raise ValueError(
            f"mouse mode must be restore/post/cursor, got {mode!r}")
    _MOUSE_MODE = mode
    note = {
        "restore": "  (pointer is returned to where you left it)",
        "post": "  (no mouse events at all; may not register in Azahar)",
        "cursor": "  (legacy: the pointer is left on the RUN button)",
    }[mode]
    log.info(f"click delivery: {mode}{note}")


def get_mouse_mode() -> str:
    return _MOUSE_MODE


def set_target_window(pid: int | None = None,
                      title_match: str | None = None) -> None:
    """Pin this process to one emulator window.

    ``pid`` is the preferred selector — it is stable while Azahar runs
    and unambiguous between two copies of the same build. ``title_match``
    is the fallback used when the pid is gone (Azahar restarted), so a
    long hunt can re-acquire its window instead of silently driving the
    other instance.
    """
    global _TARGET_PID, _TARGET_TITLE
    _TARGET_PID = int(pid) if pid else None
    _TARGET_TITLE = title_match or None
    log.info(f"input target window: pid={_TARGET_PID or 'any'} "
             f"title~{_TARGET_TITLE or 'any'}")


def get_target_window() -> tuple[int | None, str | None]:
    return _TARGET_PID, _TARGET_TITLE


def list_azahar_windows(title_substrings=("Azahar", "Citra")) -> list[dict]:
    """Every visible emulator window: ``{hwnd, pid, title}``.

    The launcher uses this to offer one window per bot instance.
    Windows-only; an empty list on other platforms.
    """
    if not sys.platform.startswith("win"):
        return []
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return []
    user32 = ctypes.windll.user32
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR,
                                      ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL

    out: list[dict] = []

    def _cb(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value or ""
        if (any(s in title for s in title_substrings)
                and "pokebot" not in title.lower()):
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            out.append({"hwnd": int(hwnd), "pid": int(pid.value),
                        "title": title})
        return True

    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool,
                                  wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(EnumProc(_cb), 0)
    out.sort(key=lambda w: w["pid"])
    return out


def find_azahar_hwnd(title_substrings=("Azahar", "Citra")) -> int:
    """Return the target Azahar window handle, or 0 if not found.

    Used by the input driver to PostMessage key events directly to
    Azahar without needing it to be the foreground window.
    Windows-only; returns 0 on other platforms.

    When ``set_target_window`` has pinned this process to one window,
    only that window can be returned — with several emulators open,
    returning "whichever matched first" would have two bots fighting
    over one game while the other ran unattended.
    """
    windows = list_azahar_windows(title_substrings)
    if not windows:
        return 0
    if _TARGET_PID is not None:
        for w in windows:
            if w["pid"] == _TARGET_PID:
                return w["hwnd"]
        # The pid is gone (Azahar restarted). Fall back to the title,
        # but never to "any window" — that is how a bot ends up
        # driving the instance belonging to a different hunt.
        if _TARGET_TITLE:
            for w in windows:
                if _TARGET_TITLE in w["title"]:
                    return w["hwnd"]
        return 0
    if _TARGET_TITLE:
        for w in windows:
            if _TARGET_TITLE in w["title"]:
                return w["hwnd"]
        return 0
    return windows[0]["hwnd"]


def click_window(hwnd: int) -> bool:
    """Click the centre of the window. See click_window_at for details."""
    return click_window_at(hwnd, 0.5, 0.5)


def click_window_at(hwnd: int, x_frac: float, y_frac: float,
                    hold_s: float = 0.05) -> bool:
    """Synthetic left-click at fractional coords (0..1) of the window.

    Used both to "wake up" Qt's input routing and to drive 3DS touch
    input. Delivery depends on the mouse mode (see ``_MOUSE_MODE``):

      * "restore" (default) — SetCursorPos + SendInput, a real
        hardware-equivalent click Qt always processes, then the
        pointer is put back where it was.
      * "post" — PostMessage WM_LBUTTONDOWN/UP only; the pointer never
        moves, but the app may ignore the messages entirely.
      * "cursor" — as "restore" but the pointer is left on the button.

    Returns True when a path reported success, False on non-Windows or
    when the hwnd is invalid. Note that "reported success" for the
    posted path means the API accepted the message, NOT that the
    application acted on it.
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

    def _post_click() -> bool:
        """WM_LBUTTONDOWN/UP straight to the window. No cursor movement."""
        WM_LBUTTONDOWN = 0x0201
        WM_LBUTTONUP   = 0x0202
        WM_MOUSEMOVE   = 0x0200
        MK_LBUTTON     = 0x0001
        lparam = (cx & 0xFFFF) | ((cy & 0xFFFF) << 16)
        import time as _t
        # A move first: Qt tracks the pointer position from the message
        # stream, and a press at a position it has never seen can be
        # dropped as spurious.
        user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lparam)
        down = user32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lparam)
        _t.sleep(hold_s)
        up = user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lparam)
        # PostMessageW returns 0 on failure, so the caller drops its
        # cached hwnd and re-acquires instead of assuming it landed.
        return bool(down and up)

    def _cursor_click() -> bool:
        """SetCursorPos + SendInput — moves the real pointer."""
        pt = wintypes.POINT(cx, cy)
        if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
            return False
        try:
            _send_mouse_click(pt.x, pt.y, hold_s)
            return True
        except Exception:
            return False

    if _MOUSE_MODE == "post":
        return _post_click()      # never emits a real mouse event
    # "restore" and "cursor" both send a real click; _send_mouse_click
    # puts the pointer back unless the mode is "cursor".
    return _cursor_click() or _post_click()


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

    # Remember where the pointer was BEFORE we move it. A hunt fires
    # this on every flee — for hours — and leaving the cursor parked on
    # Azahar's RUN button makes the machine unusable alongside the bot
    # and makes two instances fight over the pointer.
    origin = wintypes.POINT()
    have_origin = bool(user32.GetCursorPos(ctypes.byref(origin)))

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

    try:
        arr = (INPUT * 1)(_make_input(MOUSEEVENTF_LEFTDOWN))
        user32.SendInput(1, arr, ctypes.sizeof(INPUT))
        _t.sleep(hold_s)
        arr = (INPUT * 1)(_make_input(MOUSEEVENTF_LEFTUP))
        user32.SendInput(1, arr, ctypes.sizeof(INPUT))
    finally:
        # Restore in a finally: a half-done click must not strand the
        # pointer somewhere the user didn't put it.
        if have_origin and _MOUSE_MODE != "cursor":
            user32.SetCursorPos(origin.x, origin.y)


def post_key_to_window(hwnd: int, vk_code: int, hold_s: float = 0.05) -> bool:
    """PostMessage WM_KEYDOWN/WM_KEYUP to a window. Bypasses focus.

    Returns True when both messages were posted, False on non-Windows
    or when the hwnd is invalid. Not affected by which window the user
    is currently looking at — keys go straight into Azahar's message
    queue.

    The return value matters: ``InputDriver`` drops its cached window
    handle when this returns False, which is the only way the bot
    notices Azahar was restarted. This function used to ignore
    ``PostMessageW``'s result and unconditionally return True, so a
    destroyed handle looked like a successful keypress and an
    unattended hunt would keep "pressing" keys into a dead window
    forever, with no error and no encounters.
    """
    if not _window_is_alive(hwnd):
        return False
    import ctypes
    user32 = ctypes.windll.user32
    WM_KEYDOWN = 0x0100
    WM_KEYUP   = 0x0101
    # lParam encoding: bit 0-15 repeat count, 16-23 scan code (0 OK),
    # 30 prev key state (0=up→down for KEYDOWN, 1 for KEYUP).
    down = user32.PostMessageW(hwnd, WM_KEYDOWN, vk_code, 0x00000001)
    import time as _t
    _t.sleep(hold_s)
    up = user32.PostMessageW(hwnd, WM_KEYUP, vk_code, 0xC0000001)
    return bool(down) and bool(up)


def _window_is_alive(hwnd: int) -> bool:
    """True only on Windows, for a handle that still names a window.

    ``IsWindow`` guards against a handle that was destroyed *and* one
    that has since been recycled onto an unrelated window — posting
    keystrokes into some other app's message queue is worse than
    failing.
    """
    if not hwnd or not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
    except Exception:
        return False
    return bool(ctypes.windll.user32.IsWindow(hwnd))


def post_key_down(hwnd: int, vk_code: int) -> bool:
    """PostMessage a WM_KEYDOWN only (no release) — for holding a key
    while other keys are pressed (e.g. B held to run)."""
    if not _window_is_alive(hwnd):
        return False
    import ctypes
    return bool(ctypes.windll.user32.PostMessageW(
        hwnd, 0x0100, vk_code, 0x00000001))


def post_key_up(hwnd: int, vk_code: int) -> bool:
    """PostMessage a WM_KEYUP only — release a key held by
    ``post_key_down``."""
    if not _window_is_alive(hwnd):
        return False
    import ctypes
    return bool(ctypes.windll.user32.PostMessageW(
        hwnd, 0x0101, vk_code, 0xC0000001))


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

    # Resolve through find_azahar_hwnd so focusing honours the same
    # per-process window target the keypresses use. Focusing one
    # instance while typing into another is exactly the bug that made
    # two concurrent hunts unusable.
    candidates = list_azahar_windows(title_substrings)
    target = [find_azahar_hwnd(title_substrings)]
    seen_titles = [w["title"] for w in candidates]

    if not target[0]:
        pid, title_match = get_target_window()
        if pid or title_match:
            log.warning(
                f"Target Azahar window (pid={pid or 'any'}, "
                f"title~{title_match or 'any'}) is not open. "
                f"Visible matches: {seen_titles!r}")
        else:
            log.warning(f"Could not find an Azahar window to focus. "
                        f"Visible matches considered: {seen_titles!r}")
        return False

    chosen = next((w["title"] for w in candidates
                   if w["hwnd"] == target[0]), "?")
    log.info(f"Focusing Azahar window: hwnd={target[0]} title={chosen!r}")

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
    #
    # This honours the mouse mode, so on the default "post" it is a
    # posted click and the physical pointer stays where you left it.
    # It fires on every soft-reset attempt, which is why it mattered.
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


# ---------------------------------------------------------------------------
# 3DS screen geometry → touch coordinates (resize/layout robust)
# ---------------------------------------------------------------------------

# 3DS native screen sizes.
_TOP_W, _TOP_H = 400, 240
_BOT_W, _BOT_H = 320, 240

# Azahar screen layouts → (canvas_w, canvas_h, bottom_x, bottom_y).
# The two screens are drawn on this virtual canvas, then the canvas is
# scaled to fit the client area preserving aspect (letterboxed,
# centred). So the RUN button's window position is correct at ANY
# window size — we recompute it from the live client rect every flee.
_LAYOUTS = {
    # default: top above bottom, bottom centred under top.
    "vertical":     (_TOP_W, _TOP_H + _BOT_H,
                     (_TOP_W - _BOT_W) // 2, _TOP_H),
    # side-by-side: top left, bottom right, same height.
    "side_by_side": (_TOP_W + _BOT_W, _TOP_H, _TOP_W, 0),
}


def get_client_size(hwnd: int):
    """(width, height) of the window's client area, or None."""
    if not hwnd or not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None
    user32 = ctypes.windll.user32
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    w, h = rect.right - rect.left, rect.bottom - rect.top
    return (w, h) if w > 0 and h > 0 else None


def bottom_screen_fraction(client_w: int, client_h: int, layout: str,
                           local_x: float, local_y: float):
    """Map a point on the 3DS BOTTOM (touch) screen — given as
    fractions ``local_x``/``local_y`` of that 320x240 screen — to
    fractions of the whole window client area, for the given Azahar
    ``layout``. Accounts for the aspect-preserving letterbox so it's
    correct at any window size. Returns (fx, fy)."""
    canvas_w, canvas_h, bx, by = _LAYOUTS.get(
        layout, _LAYOUTS["vertical"])
    scale = min(client_w / canvas_w, client_h / canvas_h)
    render_w, render_h = canvas_w * scale, canvas_h * scale
    ox = (client_w - render_w) / 2.0
    oy = (client_h - render_h) / 2.0
    px = ox + (bx + local_x * _BOT_W) * scale
    py = oy + (by + local_y * _BOT_H) * scale
    return (px / client_w, py / client_h)
