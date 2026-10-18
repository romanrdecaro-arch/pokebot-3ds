"""
Read Azahar/Citra's controller-profile keybinds from qt-config.ini.

Azahar lets the user pick a controller profile (Settings → Controls →
Input Profile). The bot's keystrokes only land if they match the
buttons that profile actually maps to. Hard-coding the defaults is
brittle — if the user remaps DpadLeft from F to G, the bot still
sends F and the player doesn't move.

This module parses the active profile out of qt-config.ini and
returns a dict the InputDriver can consume. Falls back to config.yaml
binds (or the InputDriver's hard-coded defaults) when the file isn't
found or can't be parsed.

The Qt key codes stored in qt-config.ini are Qt::Key_* constants:

  - Letters Key_A (0x41) … Key_Z (0x5A) — happen to equal ASCII 'A'..'Z'
  - Digits  Key_0 (0x30) … Key_9 (0x39) — equal ASCII '0'..'9'
  - Specials live in 0x01000000+ range (arrow keys, modifiers, F-keys).

We translate those into the names the existing InputDriver already
understands ("a", "left", "f1", "space", …).
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# 3DS button → INI key under profiles\<n>\
_CONFIG_KEY = {
    "A":         "button_a",
    "B":         "button_b",
    "X":         "button_x",
    "Y":         "button_y",
    "L":         "button_l",
    "R":         "button_r",
    "Start":     "button_start",
    "Select":    "button_select",
    "DpadUp":    "button_up",
    "DpadDown":  "button_down",
    "DpadLeft":  "button_left",
    "DpadRight": "button_right",
}

# Circle Pad direction → its sub-key inside profiles\<n>\circle_pad.
_CIRCLE_KEY = {
    "CircleUp":    "up",
    "CircleDown":  "down",
    "CircleLeft":  "left",
    "CircleRight": "right",
}


# Qt::Key code → bind-name our InputDriver understands.
# Source: qt6/qtbase/src/corelib/global/qnamespace.h.
_QT_SPECIAL = {
    0x01000000: "esc",
    0x01000001: "tab",
    0x01000003: "backspace",
    0x01000004: "enter",
    # 0x01000005 Key_Enter (numpad) is deliberately absent: the bot
    # posts VK_RETURN, which Qt reports as Key_Return, so a numpad-Enter
    # bind would never match. Better reported as unsendable than missed.
    0x01000010: "home",
    0x01000011: "end",
    0x01000012: "left",
    0x01000013: "up",
    0x01000014: "right",
    0x01000015: "down",
    0x01000016: "page_up",
    0x01000017: "page_down",
    0x01000020: "shift",
    0x01000021: "ctrl",
    0x01000023: "alt",
}


def _qt_code_to_bind(code: int) -> Optional[str]:
    """Map a Qt::Key code → InputDriver bind name. None on unknown."""
    if code in _QT_SPECIAL:
        return _QT_SPECIAL[code]
    if 0x41 <= code <= 0x5A:                # Key_A … Key_Z
        return chr(code).lower()
    if 0x30 <= code <= 0x39:                # Key_0 … Key_9
        return chr(code)
    if code == 0x20:                        # Key_Space (== ASCII 0x20)
        return "space"
    if 0x01000030 <= code <= 0x0100003B:    # Key_F1 … Key_F12
        return f"f{code - 0x01000030 + 1}"
    return None


def _running_azahar_user_dir() -> Optional[Path]:
    """The portable ``user`` folder beside the running Azahar, if any.

    Azahar uses ``<exe folder>\\user`` instead of %APPDATA% whenever that
    folder exists. Checking %APPDATA% first meant a portable install's
    real config lost to a leftover file from some earlier install.
    """
    if not sys.platform.startswith("win"):
        return None
    try:
        from .azahar_recover import exe_of, window_pid
        from .platform_utils import find_azahar_hwnd
        hwnd = find_azahar_hwnd()
        exe = exe_of(window_pid(hwnd)) if hwnd else None
    except Exception as e:
        log.debug(f"couldn't locate the running Azahar: {e}")
        return None
    if not exe:
        return None
    user = Path(exe).parent / "user"
    return user if user.is_dir() else None


def _candidate_paths() -> list[Path]:
    """Likely locations of qt-config.ini, in priority order: the
    running emulator's portable folder, an explicit user dir, then the
    per-user install."""
    out: list[Path] = []
    portable = _running_azahar_user_dir()
    if portable:
        out.append(portable / "config" / "qt-config.ini")
    for env in ("AZAHAR_USER_DIR", "CITRA_USER_DIR"):
        p = os.environ.get(env)
        if p:
            out.append(Path(p) / "config" / "qt-config.ini")
    appdata = os.environ.get("APPDATA")
    if appdata:
        out.append(Path(appdata) / "Azahar" / "config" / "qt-config.ini")
        out.append(Path(appdata) / "Citra" / "config" / "qt-config.ini")
    return out


def find_config_path() -> Optional[Path]:
    for p in _candidate_paths():
        if p.is_file():
            return p
    return None


def _parse_package(raw: str) -> dict[str, str]:
    """Split a Citra ParamPackage string into its top-level fields.

    ``"code:70,engine:keyboard"`` → ``{"code": "70", "engine": "keyboard"}``.
    A nested package (the Circle Pad's per-direction binds) stays
    escaped inside its field; ``_unescape`` opens it.
    """
    raw = raw.strip().strip('"')
    parts: dict[str, str] = {}
    for piece in raw.split(","):
        if ":" not in piece:
            continue
        k, v = piece.split(":", 1)
        parts[k.strip()] = v.strip()
    return parts


def _unescape(value: str) -> str:
    """Undo ParamPackage's escaping of a nested package.

    ``$0`` is ``:``, ``$1`` is ``,`` and ``$2`` is ``$`` — so ``$2`` has
    to go last, or an escaped ``$`` followed by ``0`` would be misread.
    """
    return value.replace("$0", ":").replace("$1", ",").replace("$2", "$")


def _bind_or_reason(package: dict[str, str]
                    ) -> tuple[Optional[str], Optional[str]]:
    """``(bind, None)`` for a key the bot can press, else ``(None, why)``.

    The bot can only press keyboard keys. A button Azahar has mapped to
    a gamepad ignores the keyboard entirely, so a key sent for it is
    dropped without a trace — worth naming rather than guessing past.
    """
    if not package:
        return None, "not bound"
    engine = package.get("engine", "?")
    if engine != "keyboard":
        return None, f"controller ({engine})"
    try:
        code = int(package["code"])
    except (KeyError, ValueError):
        return None, "keyboard, with no key code"
    bind = _qt_code_to_bind(code)
    if bind is None:
        return None, f"a key the bot cannot send (Qt code {code:#x})"
    return bind, None


def _circle_packages(raw: str) -> dict[str, dict[str, str]]:
    """Each Circle Pad direction's own bind, keyed by KeyBinds name.

    A keyboard Circle Pad is ``engine:analog_from_button`` with one
    escaped package per direction. Anything else is a controller stick,
    which has no per-direction key at all.
    """
    top = _parse_package(raw)
    if top.get("engine") != "analog_from_button":
        stick = {"engine": top.get("engine", "?")}
        return {name: stick for name in _CIRCLE_KEY}
    return {name: _parse_package(_unescape(top.get(direction, "")))
            for name, direction in _CIRCLE_KEY.items()}


def _active_profile_number(section: dict[str, str]) -> int:
    """The INI number of the input profile Azahar is actually using.

    Azahar stores ``profile`` as a 0-based index and clamps it to the
    profiles that exist, while QSettings writes the array itself
    1-based — so index N lives under ``profiles\\<N+1>``. Reading
    ``profiles\\<N>`` found nothing on a fresh install (``profile=0``)
    and read the wrong profile for everyone else.
    """
    def number(key: str, default: int) -> int:
        try:
            return int(section.get(key, "").strip() or default)
        except ValueError:
            return default

    index = max(number("profile", 0), 0)
    size = number("profiles\\size", 0)
    if size > 0:
        index = min(index, size - 1)
    return index + 1


def load_screen_layout() -> dict:
    """Read Azahar's screen layout from qt-config.ini.

    Returns a dict with:
      ``layout_option`` int — 0 default (top above bottom), 1 single screen,
                              2 large screen, 3 side-by-side, 4 hybrid.
      ``swap_screen``   bool — top/bottom (or left/right) swapped.
      ``upright``       bool — screens rotated 90°.

    All defaults if the config can't be read (matches the bot's prior
    assumption: default vertical layout, no swap).
    """
    out = {"layout_option": 0, "swap_screen": False, "upright": False}
    cfg = find_config_path()
    if not cfg:
        return out
    try:
        text = cfg.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return out
    in_layout = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            in_layout = (line == "[Layout]")
            continue
        if not in_layout or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if k == "layout_option":
            try:
                out["layout_option"] = int(v)
            except ValueError:
                pass
        elif k == "swap_screen":
            out["swap_screen"] = v.lower() == "true"
        elif k == "upright_screen":
            out["upright"] = v.lower() == "true"
    return out


def bottom_screen_button(button_native_xy: tuple[int, int],
                         layout: Optional[dict] = None
                         ) -> tuple[float, float]:
    """Translate a native bottom-screen pixel coord (320×240) into the
    fractional window coord that PostMessage'd clicks should target.

    Handles Azahar's built-in layouts (0 default, 2 large, 3 side-by-side)
    with or without swap_screen. Custom layouts and "single screen" fall
    back to assuming the bottom screen fills the window. The window's
    aspect is assumed to match the layout's native aspect — modest
    user-resize letterboxing introduces small errors, but the in-game
    touch buttons are big enough that ~5% slop still hits.
    """
    if layout is None:
        layout = load_screen_layout()
    bx, by = button_native_xy
    bx_f = bx / 320.0
    by_f = by / 240.0
    opt = int(layout.get("layout_option", 0))
    swap = bool(layout.get("swap_screen", False))

    if opt == 3:
        # Side-by-side. Native top 400×240 + bottom 320×240 → 720×240.
        # Without swap: top on left, bottom on right.
        if swap:
            x_frac = (bx_f * 320) / 720.0
        else:
            x_frac = (400 + bx_f * 320) / 720.0
        y_frac = by_f
        return (x_frac, y_frac)

    if opt == 2:
        # Large screen: top fills ~80% of width, bottom is a small inset
        # in the lower-right corner (or upper-right if swapped). Treat
        # the bottom screen as occupying x: 0.80..1.0, y: 0.62..1.0
        # (or y: 0..0.38 when swapped).
        x_frac = 0.80 + bx_f * 0.20
        y_frac = (0.0 + by_f * 0.38) if swap else (0.62 + by_f * 0.38)
        return (x_frac, y_frac)

    # opt == 0 (default vertical) or 1/4 (single/hybrid — best-effort).
    # Native top 400×240 + bottom 320×240 stacked → 400×480, bottom
    # screen centered horizontally with 40-px letterbox each side.
    x_frac = (40 + bx_f * 320) / 400.0
    if swap:
        y_frac = by_f * 240 / 480.0           # bottom screen on top
    else:
        y_frac = (240 + by_f * 240) / 480.0   # bottom screen on bottom
    return (x_frac, y_frac)


def load_hotkeys() -> dict[str, str]:
    """Azahar's Main Window shortcuts, as {key sequence: action}.

    Worth reading before the bot holds a direction down. Azahar binds
    Left and Right to "Decrease/Increase Speed Limit" out of the box, so
    holding CircleLeft — which IS the Left arrow — does not nudge a
    cursor, it walks the emulation speed down to nothing. Same keys,
    completely different outcome, and nothing in the game would look
    broken while it happened.
    """
    path = find_config_path()
    if path is None:
        return {}
    try:
        raw = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return {}
    import urllib.parse
    out: dict[str, str] = {}
    for line in raw.splitlines():
        if not line.startswith("Shortcuts"):
            continue
        head, _, seq = line.partition("=")
        if not seq.strip() or not head.endswith("KeySeq"):
            continue
        parts = head.split("\\")
        if len(parts) < 3:
            continue
        out[seq.strip().lower()] = urllib.parse.unquote(parts[-2])
    return out


def load_active_profile_binds() -> Optional[dict]:
    """Read the active-profile keybindings out of Azahar's qt-config.ini.

    Returns ``{"A": "a", "DpadLeft": "f", …}`` mirroring the keys on
    pokebot.input_driver.KeyBinds, plus three underscored entries:
    ``_source`` (the file), ``_profile`` (its name) and ``_unusable``
    (``{button: why}`` for every button the bot has no keyboard key to
    press, e.g. one mapped to a controller). A button the profile does
    not mention is left out: Azahar uses its default for it, which
    KeyBinds already mirrors.

    Returns None when there is nothing to read (no config file, no
    Controls section, no profile) so callers fall back to their
    existing defaults.
    """
    cfg = find_config_path()
    if not cfg:
        return None
    try:
        text = cfg.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        log.debug(f"can't read {cfg}: {e}")
        return None

    # Manual line-walker rather than ConfigParser: Qt INI files use
    # backslashes inside option names (`profiles\1\button_a`), which
    # ConfigParser tolerates but optionxform/case-handling has bitten
    # us in the past. Linear scan keeps things explicit.
    in_controls = False
    section: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_controls = (line == "[Controls]")
            continue
        if not in_controls:
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        section[k.strip()] = v.strip()

    if not section:
        return None

    # Without a profiles array Azahar falls back to its legacy top-level
    # keys; the bot falls back to its defaults, which mirror Azahar's.
    if not section.get("profiles\\size", "").strip():
        return None
    number = _active_profile_number(section)
    prefix = f"profiles\\{number}\\"
    if not any(k.startswith(prefix) for k in section):
        return None

    packages = {button: _parse_package(section[prefix + ini_key])
                for button, ini_key in _CONFIG_KEY.items()
                if prefix + ini_key in section}
    circle = section.get(prefix + "circle_pad")
    if circle is not None:
        packages.update(_circle_packages(circle))

    resolved = {button: _bind_or_reason(package)
                for button, package in packages.items()}
    return {
        "_source": str(cfg),
        "_profile": section.get(prefix + "name",
                                str(number)).strip().strip('"'),
        "_unusable": {button: why for button, (bind, why)
                      in resolved.items() if bind is None},
        **{button: bind for button, (bind, _) in resolved.items()
           if bind is not None},
    }


def unusable_binds_message(profile: str,
                           unusable: dict[str, str]) -> Optional[str]:
    """What to tell the user about buttons the bot cannot press, or None.

    The common case is a profile set up for a gamepad: Azahar then
    ignores the keyboard for those buttons, and every key the bot sends
    is dropped with nothing on screen to say why.
    """
    if not unusable:
        return None
    by_reason: dict[str, list[str]] = {}
    for button, why in unusable.items():
        by_reason.setdefault(why, []).append(button)
    detail = "; ".join(f"{', '.join(buttons)}: {why}"
                       for why, buttons in by_reason.items())
    return (f"Azahar's active input profile {profile!r} has no keyboard "
            f"key the bot can press for {detail}. The bot presses "
            f"KEYBOARD keys, so Azahar ignores those presses. Fix: in "
            f"Azahar, Emulation > Configure > Controls, select a profile "
            f"that uses the keyboard (a new profile starts on the keyboard "
            f"defaults) or bind keyboard keys to those buttons, then start "
            f"the bot again.")
