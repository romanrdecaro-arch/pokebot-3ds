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


# Qt::Key code → bind-name our InputDriver understands.
# Source: qt6/qtbase/src/corelib/global/qnamespace.h.
_QT_SPECIAL = {
    0x01000000: "esc",
    0x01000001: "tab",
    0x01000003: "backspace",
    0x01000004: "enter",
    0x01000005: "enter",        # Key_Enter (numpad) — same logical name
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


def _candidate_paths() -> list[Path]:
    """Likely locations of qt-config.ini, in priority order."""
    out: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        out.append(Path(appdata) / "Azahar" / "config" / "qt-config.ini")
        out.append(Path(appdata) / "Citra" / "config" / "qt-config.ini")
    # Portable installs
    for env in ("AZAHAR_USER_DIR", "CITRA_USER_DIR"):
        p = os.environ.get(env)
        if p:
            out.append(Path(p) / "config" / "qt-config.ini")
    return out


def find_config_path() -> Optional[Path]:
    for p in _candidate_paths():
        if p.is_file():
            return p
    return None


def _parse_value(raw: str) -> Optional[int]:
    """Pull the `code:N` field out of a Qt INI button value.

    Example raw: ``"code:70,engine:keyboard"`` → 70.
    Returns None if engine isn't keyboard or the code can't be parsed.
    """
    raw = raw.strip().strip('"')
    parts: dict[str, str] = {}
    for piece in raw.split(","):
        if ":" not in piece:
            continue
        k, v = piece.split(":", 1)
        parts[k.strip()] = v.strip()
    if parts.get("engine") != "keyboard":
        return None
    try:
        return int(parts["code"])
    except (KeyError, ValueError):
        return None


def load_active_profile_binds() -> Optional[dict[str, str]]:
    """Read the active-profile keybindings out of Azahar's qt-config.ini.

    Returns ``{"A": "a", "DpadLeft": "f", …}`` mirroring the keys on
    pokebot.input_driver.KeyBinds. Returns None on any failure (no
    config file, parse error, no Controls section, etc.) so callers
    can fall back to their existing defaults.
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

    profile_idx = section.get("profile", "1").strip() or "1"

    binds: dict[str, str] = {}
    for button, ini_key in _CONFIG_KEY.items():
        raw = section.get(f"profiles\\{profile_idx}\\{ini_key}")
        if raw is None:
            continue
        code = _parse_value(raw)
        if code is None:
            continue
        bind = _qt_code_to_bind(code)
        if bind:
            binds[button] = bind
    if not binds:
        return None
    return {
        "_source": str(cfg),
        "_profile": section.get(f"profiles\\{profile_idx}\\name",
                                profile_idx).strip().strip('"'),
        **binds,
    }
