"""
Bring Azahar back when it dies in the middle of a hunt.

Azahar 2125.1.1 crashes inside its own soft-reset relaunch roughly once
every 650-900 resets. Both crashes in a USUM starter hunt were the same
two instructions, at azahar.exe+0xcb4f29/0xcb4f2d, an access violation
in the OpenGL render widget's paint: it follows
system -> GPU -> renderer while L+R+Start is tearing that renderer
down, and reads freed memory. The bot sends no memory reads in that
window, so nothing it does causes the crash and nothing it could stop
doing would prevent it.

It can be survived, though. A reset hunt's save IS its starting point,
so relaunching Azahar on the same ROM is just a slow soft reset: the
game boots, the presses go through the title and CONTINUE, and the
hunt carries on. This module does the relaunch:

* the executable is taken from the Azahar process that was running
  when the hunt started;
* the ROM is ``azahar.rom`` in config.yaml, or else the newest entry
  in Azahar's own recent-files list that names the running game;
* a crash loop is not survived forever: past ``max_restarts`` within
  ``restart_window_minutes`` the hunt stops and says so;
* the dialogs a fresh Azahar opens on its own are answered (see
  ``STARTUP_DIALOGS``) -- found the hard way, below.
"""
from __future__ import annotations

import ctypes
import logging
import os
import re
import subprocess
import sys
import time
from collections import deque
from ctypes import wintypes
from pathlib import Path

log = logging.getLogger(__name__)


class AzaharGone(Exception):
    """There is no Azahar window left to send input to."""


_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED = 0x1000
_SYNCHRONIZE = 0x00100000
_STILL_ACTIVE = 259


def _kernel32():
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    return k


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD(0)
    ctypes.windll.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd),
                                                  ctypes.byref(pid))
    return int(pid.value)


def exe_of(pid: int) -> str | None:
    """Full path of the executable running as ``pid``."""
    k = _kernel32()
    h = k.OpenProcess(_PROCESS_QUERY_LIMITED, False, pid)
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        k.CloseHandle(h)


def process_alive(pid: int) -> bool:
    k = _kernel32()
    h = k.OpenProcess(_PROCESS_QUERY_LIMITED, False, pid)
    if not h:
        return False
    try:
        code = wintypes.DWORD(0)
        k.GetExitCodeProcess(h, ctypes.byref(code))
        return code.value == _STILL_ACTIVE
    finally:
        k.CloseHandle(h)


def terminate(pid: int) -> None:
    k = _kernel32()
    h = k.OpenProcess(_PROCESS_TERMINATE | _SYNCHRONIZE, False, pid)
    if h:
        try:
            k.TerminateProcess(h, 1)
            k.WaitForSingleObject(h, 10_000)
        finally:
            k.CloseHandle(h)


# ----------------------------------------------------------------------
# Dialogs a fresh Azahar opens by itself
# ----------------------------------------------------------------------
#: Title -> where the harmless button sits, as fractions of the dialog's
#: client area.
#:
#: Found live (2026-10-09): the first relaunch came up with "Update
#: 2126.2 for Azahar is available. Would you like to download it?"
#: [Yes] [Ignore]. Until it is answered Qt blocks the main window, so
#: every posted key is DROPPED -- the hunt pressed A at the attract
#: movie for minutes, game attached and memory readable, nothing moving.
#: WM_CLOSE and Escape are both ignored by it; a click on Ignore is not.
#: Ignore downloads nothing.
STARTUP_DIALOGS = {"Update Available": (0.79, 0.74)}

_WM_MOUSEMOVE, _WM_LBUTTONDOWN, _WM_LBUTTONUP = 0x0200, 0x0201, 0x0202


def windows_of(pid: int) -> list[tuple[int, str]]:
    """Visible top-level windows owned by ``pid``: (hwnd, title)."""
    user32 = ctypes.windll.user32
    out: list[tuple[int, str]] = []

    def cb(hwnd, _l):
        if not user32.IsWindowVisible(hwnd) or window_pid(hwnd) != pid:
            return True
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        out.append((int(hwnd), buf.value or ""))
        return True

    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(proc(cb), 0)
    return out


def post_click(hwnd: int, x_frac: float, y_frac: float) -> None:
    """A click posted to ``hwnd`` -- the real pointer never moves, and
    nothing else on screen can receive it."""
    user32 = ctypes.windll.user32
    rect = wintypes.RECT()
    user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    x = int(rect.right * x_frac)
    y = int(rect.bottom * y_frac)
    lp = (y << 16) | (x & 0xFFFF)
    user32.PostMessageW(hwnd, _WM_MOUSEMOVE, 0, lp)
    time.sleep(0.05)
    user32.PostMessageW(hwnd, _WM_LBUTTONDOWN, 1, lp)
    time.sleep(0.08)
    user32.PostMessageW(hwnd, _WM_LBUTTONUP, 0, lp)


def dismiss_dialogs(pid: int, main_hwnd: int, seen: set | None = None
                    ) -> list[str]:
    """Answer any known start-up dialog of ``pid``; report the others.

    Returns the titles clicked. An unknown dialog is only logged (once,
    via ``seen``): clicking a button nobody has looked at is how a bot
    agrees to something.
    """
    clicked = []
    for hwnd, title in windows_of(pid):
        if hwnd == main_hwnd:
            continue
        where = STARTUP_DIALOGS.get(title)
        if where is not None:
            log.warning(f"  Azahar opened {title!r}; answering it so it "
                        f"stops blocking input")
            post_click(hwnd, *where)
            clicked.append(title)
        elif seen is not None and title not in seen:
            seen.add(title)
            log.warning(f"  Azahar has a window {title!r} open that the bot "
                        f"does not know; if keys stop landing, close it.")
    return clicked


# ----------------------------------------------------------------------
# Which ROM to relaunch
# ----------------------------------------------------------------------
def qt_config_path() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Azahar" / "config" / \
        "qt-config.ini"


def recent_files(ini_text: str) -> list[str]:
    """Azahar's ``Paths\\recentFiles`` list, newest first.

    Qt writes it as a comma-separated list, quoting the entries that
    contain commas themselves -- which every No-Intro name does.
    """
    for line in ini_text.splitlines():
        if line.startswith("Paths\\recentFiles="):
            raw = line.split("=", 1)[1]
            return [m.group(1) if m.group(1) is not None else m.group(2)
                    for m in re.finditer(r'\s*(?:"([^"]*)"|([^,]+))\s*,?',
                                         raw)
                    if (m.group(1) or m.group(2) or "").strip()]
    return []


def game_words(game_name: str) -> str:
    """"UltraMoon" -> "Ultra Moon", the way ROM files name it."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", game_name).strip()


def rom_for(game_name: str, ini_text: str) -> str | None:
    """The newest recent file that names this game, if it still exists."""
    words = game_words(game_name)
    if not words:
        return None
    pattern = re.compile(r"pok[eé]mon\W+" + re.escape(words) + r"\b",
                         re.IGNORECASE)
    for path in recent_files(ini_text):
        if pattern.search(Path(path).name) and Path(path).exists():
            return path
    return None


# ----------------------------------------------------------------------
# The relaunch
# ----------------------------------------------------------------------
class Recovery:
    """Relaunches Azahar on the hunt's ROM, a bounded number of times."""

    def __init__(self, exe: str, rom: str, pid: int = 0,
                 max_restarts: int = 5, window_s: float = 3600.0,
                 boot_timeout: float = 90.0, dialog_watch: float = 20.0):
        self.exe = exe
        self.rom = rom
        self.pid = pid
        self.max_restarts = max(1, int(max_restarts))
        self.window_s = float(window_s)
        self.boot_timeout = float(boot_timeout)
        #: How long after a relaunch to keep watching for a start-up
        #: dialog. The update check is a network round trip, so the
        #: dialog can arrive after the game is already running.
        self.dialog_watch = float(dialog_watch)
        self.restarts: deque[float] = deque()
        self._unknown: set = set()

    def dismiss_dialogs(self) -> list[str]:
        """Answer any known dialog the current Azahar has open."""
        from .platform_utils import find_azahar_hwnd

        if not self.pid:
            return []
        return dismiss_dialogs(self.pid, find_azahar_hwnd() or 0,
                               self._unknown)

    @classmethod
    def for_hunt(cls, ctx) -> "Recovery | None":
        """Work out how to relaunch THIS Azahar on THIS game, or None.

        Called at the start of a hunt, while Azahar is still alive --
        afterwards there is no process left to ask where it lives.
        """
        cfg = (ctx.config.get("azahar") or {}) if hasattr(ctx, "config") \
            else {}
        if not cfg.get("restart_on_crash", True):
            log.info("  crash recovery is off (azahar.restart_on_crash)")
            return None
        if not sys.platform.startswith("win"):
            return None
        from .citra_rpc import POKEMON_TITLE_IDS
        from .platform_utils import find_azahar_hwnd

        hwnd = find_azahar_hwnd() or 0
        pid = window_pid(hwnd) if hwnd else 0
        exe = cfg.get("exe") or (exe_of(pid) if pid else None)
        tid = getattr(ctx.rpc, "attached_title_id", None)
        if tid is None:
            try:
                tid = next((t for t, _n in ctx.rpc.list_processes().values()
                            if t in POKEMON_TITLE_IDS), None)
            except Exception:
                tid = None
        name = POKEMON_TITLE_IDS.get(tid, ("", ""))[0] if tid else ""
        rom = cfg.get("rom") or None
        if not rom:
            try:
                rom = rom_for(name, qt_config_path().read_text(
                    encoding="utf-8", errors="replace"))
            except OSError:
                rom = None
        if not exe or not rom:
            log.warning(f"  crash recovery OFF: could not tell "
                        f"{'which ROM is running' if exe else 'where azahar.exe is'}"
                        f". Set azahar.rom (and azahar.exe) in config.yaml "
                        f"to have a crashed Azahar relaunched.")
            return None
        log.info(f"  crash recovery on: relaunches {Path(exe).name} with "
                 f"{Path(rom).name}")
        return cls(exe, rom, pid,
                   max_restarts=cfg.get("max_restarts", 5),
                   window_s=float(cfg.get("restart_window_minutes", 60))
                   * 60)

    def allowed(self, now: float) -> bool:
        while self.restarts and now - self.restarts[0] > self.window_s:
            self.restarts.popleft()
        return len(self.restarts) < self.max_restarts

    def revive(self, ctx) -> bool:
        """Relaunch Azahar on the ROM and re-attach. True when it is back."""
        now = time.monotonic()
        if not self.allowed(now):
            log.error(f"  Azahar has crashed {len(self.restarts)} times in "
                      f"{self.window_s / 60:.0f} minutes. Not relaunching "
                      f"it again -- something is wrong beyond the known "
                      f"reset crash.")
            return False
        self.restarts.append(now)
        self._clear_old()
        log.warning(f"  relaunching Azahar on {Path(self.rom).name} "
                    f"(restart {len(self.restarts)} of at most "
                    f"{self.max_restarts} per {self.window_s / 60:.0f} min)")
        try:
            proc = subprocess.Popen(
                [self.exe, self.rom], cwd=str(Path(self.exe).parent),
                creationflags=(getattr(subprocess, "DETACHED_PROCESS", 0)
                               | getattr(subprocess,
                                         "CREATE_NEW_PROCESS_GROUP", 0)),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True)
        except OSError as exc:
            log.error(f"  could not start {self.exe}: {exc}")
            return False
        self.pid = proc.pid
        return self._wait_until_back(ctx)

    def _clear_old(self) -> None:
        """Make sure the crashed process is really gone first.

        Windows Error Reporting can hold a crashed process open for a
        while; two Azahars would both answer RPC on the same port.
        """
        if not self.pid:
            return
        deadline = time.monotonic() + 15
        while process_alive(self.pid) and time.monotonic() < deadline:
            time.sleep(0.5)
        if process_alive(self.pid):
            log.warning(f"  the crashed Azahar (PID {self.pid}) is still "
                        f"there; ending it")
            terminate(self.pid)

    def _wait_until_back(self, ctx) -> bool:
        """Window up, game attached, and no start-up dialog in the way.

        Attaching alone is not "back": a dialog blocking the main window
        leaves the game running and readable with every key dropped.
        """
        from .platform_utils import ensure_dpi_aware, find_azahar_hwnd

        try:
            ensure_dpi_aware()           # dialog click coordinates
        except Exception:
            pass
        started = time.monotonic()
        deadline = started + self.boot_timeout
        attached = None
        while time.monotonic() < deadline and not ctx.should_stop():
            if find_azahar_hwnd():
                self.dismiss_dialogs()
                if attached is None:
                    try:
                        attached = ctx.rpc.attach_to_pokemon_game()
                        log.info(f"  Azahar is back: attached to "
                                 f"{attached[2]} (PID {attached[0]})")
                    except Exception as exc:
                        log.debug(f"  not attached yet: {exc}")
                if (attached is not None
                        and time.monotonic() - started >= self.dialog_watch):
                    # The window handle the driver cached died with the
                    # old process.
                    if hasattr(ctx.input, "_azahar_hwnd"):
                        ctx.input._azahar_hwnd = 0
                    return True
            time.sleep(0.5)
        log.error(f"  Azahar did not come back with the game within "
                  f"{self.boot_timeout:.0f}s.")
        return False
