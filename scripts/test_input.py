"""
Find out which input method Azahar actually acts on.

Keypresses were not reaching Pokémon Crystal: PostMessage, pynput and
scancode SendInput all reported success and did nothing, verified by
screenshotting before and after. That leaves two suspects worth
testing properly rather than guessing at:

  * WHICH WINDOW. Qt handles keys in a child render widget, not the
    top-level frame, and a posted message goes to exactly the window
    you address. The bot has only ever addressed the top-level.
  * WHICH BUTTON. Movement can come from the D-pad or the Circle Pad,
    and Azahar binds them to different keys (F/H/T/G vs the arrow
    keys), so one may work where the other does not.

This presses a button every way it can and watches the screen, which
is the only judge that cannot be fooled: PostMessageW returning
success means "queued", not "acted upon".

    python scripts/test_input.py            # tests Start
    python scripts/test_input.py --button DpadRight

Have Crystal loaded and on the overworld. Nothing here is destructive:
Start opens the menu and the script closes it again.
"""
from __future__ import annotations

import argparse
import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pokebot.azahar_config import load_active_profile_binds   # noqa: E402
from pokebot.platform_utils import (list_azahar_windows,      # noqa: E402
                                    focus_azahar)

u = ctypes.windll.user32

#: A visible change of at least this many pixels counts as "it worked".
CHANGED_PX = 2500


def vk_for(char: str) -> int:
    return u.VkKeyScanW(ord(char[:1])) & 0xFF


def children(top: int) -> list:
    out = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def cb(h, _l):
        r = wintypes.RECT()
        u.GetClientRect(h, ctypes.byref(r))
        if r.right > 300 and r.bottom > 200:      # plausible render area
            out.append(h)
        return True

    u.EnumChildWindows(top, proc(cb), 0)
    return out


def grab(box):
    from PIL import ImageGrab
    return ImageGrab.grab(bbox=box).convert("L")


def changed_pixels(a, b) -> int:
    from PIL import ImageChops
    return sum(1 for p in ImageChops.difference(a, b).getdata() if p > 25)


def post(hwnd: int, vk: int, hold: float = 0.12) -> None:
    u.PostMessageW(hwnd, 0x0100, vk, 0x00000001)
    time.sleep(hold)
    u.PostMessageW(hwnd, 0x0101, vk, 0xC0000001)


def send_vk(vk: int, hold: float = 0.12) -> None:
    u.keybd_event(vk, 0, 0, 0)
    time.sleep(hold)
    u.keybd_event(vk, 0, 2, 0)


def send_scan(vk: int, hold: float = 0.12) -> None:
    class KBD(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class U(ctypes.Union):
        _fields_ = [("ki", KBD)]

    class INP(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", U)]

    u.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INP), ctypes.c_int]
    sc = u.MapVirtualKeyW(vk, 0)
    for flags in (0x0008, 0x0008 | 0x0002):
        arr = (INP * 1)(INP(type=1, u=U(ki=KBD(0, sc, flags, 0, None))))
        u.SendInput(1, arr, ctypes.sizeof(INP))
        time.sleep(hold)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--button", default="Start",
                    help="logical button to test (default Start)")
    ap.add_argument("--settle", type=float, default=1.2)
    args = ap.parse_args(argv)

    wins = [w for w in list_azahar_windows()]
    if not wins:
        print("No Azahar window found — load Crystal first.")
        return 1
    top = wins[0]["hwnd"]
    print(f"window: {wins[0]['title']!r}")

    binds = {k: v for k, v in (load_active_profile_binds() or {}).items()
             if not k.startswith("_")}
    key = binds.get(args.button)
    if not key:
        print(f"No Azahar bind for {args.button!r}; known: {sorted(binds)}")
        return 1
    vk = vk_for(key)
    print(f"button {args.button} -> key {key!r} -> VK {vk:#04x}\n")

    focus_azahar()
    time.sleep(1.0)
    r = wintypes.RECT()
    u.GetWindowRect(top, ctypes.byref(r))
    box = (r.left, r.top, r.right, r.bottom)

    trials = [(f"PostMessage -> top-level {top}",
               lambda h=top: post(h, vk))]
    for kid in children(top):
        trials.append((f"PostMessage -> child {kid}",
                       lambda h=kid: post(h, vk)))
    trials.append(("SendInput virtual-key (needs focus)",
                   lambda: send_vk(vk)))
    trials.append(("SendInput scancode (needs focus)",
                   lambda: send_scan(vk)))

    winners = []
    for label, action in trials:
        before = grab(box)
        action()
        time.sleep(args.settle)
        delta = changed_pixels(before, grab(box))
        ok = delta >= CHANGED_PX
        print(f"  {label:44} changed {delta:6d} px  {'<-- WORKS' if ok else ''}")
        if ok:
            winners.append(label)
            # put the screen back the way we found it
            send_vk(vk_for(binds.get("B", "s")))
            time.sleep(args.settle)

    print()
    if winners:
        print("Working input path(s):")
        for w in winners:
            print(f"  * {w}")
        print("\nTell me which one and I'll make the bot use it.")
    else:
        print("Nothing moved the screen. Azahar is not acting on synthetic")
        print("input at all — check Emulation > Configure > General for a")
        print("'pause when in background' style option, and confirm the")
        print("key works when YOU press it with the window focused.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
