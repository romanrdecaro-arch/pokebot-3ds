"""
Entry point for pokebot-3ds.

    python run.py                 # uses config.yaml in cwd
    python run.py --config foo.yaml
    python run.py --mode observe  # override mode
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from pokebot.config_io import load_config as _load_yaml


_TARGET_PRESETS = {
    # "any" = match the first thing we see (single empty rule = no constraints).
    "any":            {"mode": "all",  "rules": [{}]},
    "shiny":          {"mode": "all",  "rules": [{"shiny": True}]},
    "perfect6":       {"mode": "all",  "rules": [{"perfect_iv_count_min": 6}]},
    "perfect5":       {"mode": "all",  "rules": [{"perfect_iv_count_min": 5}]},
    "shiny+perfect4": {"mode": "all",
                       "rules": [{"shiny": True, "perfect_iv_count_min": 4}]},
}


def _target_preset(name: str) -> dict:
    return _TARGET_PRESETS[name]


def main(argv=None):
    ap = argparse.ArgumentParser(description="pokebot-3ds")
    ap.add_argument("--config", default="config.yaml",
                    help="path to config file (yaml or json)")
    ap.add_argument("--mode", default=None,
                    help="override config mode (observe, encounter, soft_reset)")
    ap.add_argument("--game", default=None,
                    help="override game registry key")
    ap.add_argument("--dry-run", action="store_true",
                    help="don't actually press keys (useful for setup)")
    ap.add_argument("--starter", default=None,
                    help="starter to hunt in soft_reset mode "
                         "(e.g. chespin, fennekin, froakie)")
    ap.add_argument("--soft-reset-target", default=None,
                    choices=["starters", "snorlax", "lapras"],
                    help="which soft-reset routine to run. Defaults to "
                         "'starters'.")
    ap.add_argument("--movement", default=None,
                    choices=["horizontal", "vertical"],
                    help="walking axis for encounter mode "
                         "(horizontal = Left/Right, vertical = Up/Down)")
    ap.add_argument("--flee-delay", type=float, default=None,
                    help="seconds to wait after a wild appears before "
                         "fleeing (encounter mode). Raise for a slower "
                         "emulator; overrides config.yaml.")
    ap.add_argument("--fish-cast-settle", type=float, default=None,
                    help="seconds between the Y cast and the A hook "
                         "(fishing mode). Higher = waits longer for "
                         "the bite. Overrides config.yaml.")
    ap.add_argument("--trainer-name", default=None,
                    help="in-game OT name; used by the party locator "
                         "to identify your owned Pokémon (soft_reset "
                         "mode). Overrides config.yaml.")
    ap.add_argument("--press-speed", type=float, default=None,
                    help="seconds between button presses in the X/Y "
                         "starter sequence (soft_reset mode). Lower "
                         "= faster; sets advance_gap + xy_receive_gap.")
    ap.add_argument("--verify-address", default=None,
                    help="for debug mode: read 260 bytes at this hex "
                         "address and report whether it's a valid PK6 "
                         "record. No scanning. e.g. --verify-address "
                         "0x14abcd00")
    ap.add_argument("--target", default=None,
                    choices=["any", "shiny", "perfect6", "perfect5",
                             "shiny+perfect4"],
                    help="override target filter (else use config.yaml)")
    ap.add_argument("--verbose", "-v", action="store_true")
    # --- multi-instance: which emulator does THIS bot drive? ---------
    ap.add_argument("--window-pid", type=int, default=None,
                    help="process id of the Azahar window to drive. "
                         "Required when more than one emulator is open, "
                         "otherwise the bot targets whichever it finds "
                         "first and two bots fight over one game.")
    ap.add_argument("--window-title", default=None,
                    help="window-title fragment used to re-acquire the "
                         "emulator if it restarts and its pid changes")
    ap.add_argument("--rpc-port", type=int, default=None,
                    help="Azahar scripting port for this instance "
                         "(each emulator needs its own; overrides "
                         "rpc.port in the config)")
    ap.add_argument("--mouse-mode", default=None,
                    choices=["restore", "post", "cursor"],
                    help="how the RUN touch is delivered. restore "
                         "(default) clicks for real then puts your "
                         "pointer back; post sends no mouse events at "
                         "all but Azahar may ignore it; cursor is the "
                         "old behaviour that leaves the pointer on the "
                         "button.")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg_path = Path(args.config)
    if cfg_path.exists():
        config = _load_yaml(cfg_path)
    else:
        logging.warning(f"{cfg_path} not found; using defaults")
        config = {}

    def section(name: str) -> dict:
        """Fetch a config section, healing a present-but-empty one.

        A section written as bare ``input:`` with nothing under it
        parses to None, not {}. ``setdefault`` then hands back that
        None and the subscript raises TypeError, so any CLI override
        crashed against a perfectly legal config file.
        """
        existing = config.get(name)
        if not isinstance(existing, dict):
            existing = {}
            config[name] = existing
        return existing

    if args.mode:     config["mode"] = args.mode
    if args.game:     config["game"] = args.game
    if args.dry_run:  section("input")["dry_run"] = True
    if args.starter:  section("soft_reset")["starter"] = args.starter
    if args.soft_reset_target:
        section("soft_reset")["target"] = args.soft_reset_target
    if args.movement:
        section("random_encounters")["movement"] = args.movement
    if args.flee_delay is not None:
        section("random_encounters")["flee_delay"] = args.flee_delay
    if args.fish_cast_settle is not None:
        section("random_encounters")["fish_cast_settle"] = args.fish_cast_settle
    if args.trainer_name:
        section("soft_reset")["trainer_name"] = args.trainer_name
    if args.press_speed is not None:
        sr = section("soft_reset")
        sr["advance_gap"] = args.press_speed
        sr["xy_receive_gap"] = args.press_speed
    if args.target:   config["target"] = _target_preset(args.target)
    if args.verify_address:
        config["verify_address"] = int(args.verify_address, 0)
    if args.rpc_port is not None:
        section("rpc")["port"] = args.rpc_port

    # Pin this process to one emulator window BEFORE the bot starts, so
    # every keypress, touch and focus call in every mode targets the
    # same instance. Set once here rather than threaded through each
    # call site: one bot process drives exactly one window.
    if args.window_pid or args.window_title:
        from pokebot.platform_utils import set_target_window
        set_target_window(pid=args.window_pid,
                          title_match=args.window_title)

    # Click delivery. Default "post" keeps the physical pointer where
    # the user left it, so a hunt can run for hours without stealing
    # the mouse and two instances never fight over it.
    mouse_mode = args.mouse_mode or str(
        (config.get("input") or {}).get("mouse_mode", "restore")).lower()
    from pokebot.platform_utils import set_mouse_mode
    try:
        set_mouse_mode(mouse_mode)
    except ValueError as exc:
        logging.warning(f"{exc}; falling back to 'restore'")
        set_mouse_mode("restore")

    # delayed import so --help works without dependencies
    from pokebot.bot import Bot
    Bot(config).run()


if __name__ == "__main__":
    sys.exit(main())
