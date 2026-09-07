"""
Why isn't the catch working on THIS machine?

Every touch the bot makes -- RUN, BAG, POKE BALLS, the ball -- is a
fraction of the Azahar window worked out from three things: the screen
layout, the window size, and the display's DPI scaling. Get any of them
wrong and the touch is still delivered successfully, it just lands
somewhere harmless. Nothing errors, nothing is logged, the catch simply
does nothing.

So this prints all three, shows exactly where each button will be
touched, and can touch them for real so you can watch what happens.

    python scripts/diagnose_touch.py            # report only
    python scripts/diagnose_touch.py --touch bag
    python scripts/diagnose_touch.py --touch all

Run it with Azahar open and a wild battle on screen.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot import platform_utils as pu          # noqa: E402
from pokebot.modes.catch import (DEFAULT_BAG,     # noqa: E402
                                 DEFAULT_BALLS, DEFAULT_BALL)

POINTS = {
    "bag":   ("BAG", DEFAULT_BAG),
    "balls": ("POKE BALLS", DEFAULT_BALLS),
    "ball":  ("first ball slot", DEFAULT_BALL),
    "run":   ("RUN", (0.5, 0.86)),
}


def _ok(flag: bool) -> str:
    return "OK  " if flag else "BAD "


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--touch", choices=[*POINTS, "all"],
                    help="actually touch this point so you can watch it")
    ap.add_argument("--layout", default=None,
                    help="override the detected layout for the report")
    args = ap.parse_args()

    problems: list[str] = []

    print("=" * 62)
    print("  pokebot-3ds touch diagnostic")
    print("=" * 62)

    # --- 1. DPI ------------------------------------------------------
    state = pu.ensure_dpi_aware()
    scale = pu.display_scaling()
    scaled = scale != 1.0
    aware = state.startswith(("per-monitor", "system"))
    print("\n[1] Display")
    print(f"    scaling         : {scale * 100:.0f}%")
    print(f"    DPI awareness   : {state}")
    if scaled and not aware:
        print("    -> Windows will hand this process virtualised window")
        print("       coordinates while the click uses physical ones, so")
        print("       every touch lands short. THIS BREAKS CATCHING.")
        problems.append(
            f"display is scaled to {scale * 100:.0f}% and DPI awareness "
            f"could not be set")
    elif scaled:
        print("    -> scaled, but this process is DPI-aware, so the")
        print("       window rect and the click agree. Fine.")

    # --- 2. Azahar window -------------------------------------------
    hwnd = pu.find_azahar_hwnd()
    size = pu.get_client_size(hwnd) if hwnd else None
    print("\n[2] Azahar window")
    print(f"    hwnd            : {hwnd or 'NOT FOUND'}")
    print(f"    client size     : {size or 'unknown'}")
    if not hwnd:
        problems.append("no Azahar window found - is it running?")
        print("    -> Open Azahar and load your game, then re-run this.")
    elif not size:
        problems.append("could not measure the Azahar window")

    try:
        wins = pu.list_azahar_windows()
        if len(wins) > 1:
            print(f"    -> {len(wins)} emulator windows open; this drives "
                  f"the first unless --window-pid is set.")
            problems.append(f"{len(wins)} emulator windows are open")
    except Exception:
        pass

    # --- 3. Screen layout -------------------------------------------
    from pokebot.azahar_config import load_screen_layout, find_config_path
    raw = load_screen_layout()
    name, swap = pu.resolve_layout(args.layout or "auto")
    print("\n[3] Screen layout")
    print(f"    qt-config.ini   : {find_config_path() or 'NOT FOUND'}")
    print(f"    layout_option   : {raw.get('layout_option')} "
          f"(0 default, 1 single, 2 large, 3 side-by-side)")
    print(f"    swap_screen     : {raw.get('swap_screen')}")
    print(f"    resolved to     : {name}{' + swap' if swap else ''}")
    if not find_config_path():
        problems.append("Azahar's qt-config.ini was not found, so the "
                        "layout is a guess")

    # --- 4. Where the touches land ----------------------------------
    print("\n[4] Touch targets")
    if size:
        print(f"    {'button':<16} {'window fraction':<20} client px")
        for key, (label, local) in POINTS.items():
            fx, fy = pu.bottom_screen_fraction(size[0], size[1], name,
                                               local[0], local[1], swap)
            px, py = int(fx * size[0]), int(fy * size[1])
            inside = 0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0
            print(f"    {_ok(inside)}{label:<12} "
                  f"({fx:.3f}, {fy:.3f})      ({px}, {py})")
            if not inside:
                problems.append(f"{label} computes to outside the window")
    else:
        print("    (needs the window size)")

    # --- 5. Input path ----------------------------------------------
    print("\n[5] Input delivery")
    print(f"    mouse mode      : {pu.get_mouse_mode()}")

    # --- verdict -----------------------------------------------------
    print("\n" + "=" * 62)
    if problems:
        print("  PROBLEMS FOUND")
        for p in problems:
            print(f"   - {p}")
    else:
        print("  Nothing obviously wrong. If the catch still fails, run")
        print("  with --touch bag while a battle is on screen and watch")
        print("  whether the bag opens.")
    print("=" * 62)

    # --- optional live touch ----------------------------------------
    if args.touch and hwnd and size:
        keys = list(POINTS) if args.touch == "all" else [args.touch]
        for key in keys:
            label, local = POINTS[key]
            fx, fy = pu.bottom_screen_fraction(size[0], size[1], name,
                                               local[0], local[1], swap)
            print(f"\ntouching {label} at ({fx:.3f}, {fy:.3f}) ...")
            ok = pu.click_window_at(hwnd, fx, fy, hold_s=0.08)
            print(f"  delivered: {ok}  "
                  f"(delivered only means Windows accepted it)")
            if len(keys) > 1:
                import time
                time.sleep(2.0)

    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
