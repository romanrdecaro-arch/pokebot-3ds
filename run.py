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

# minimal yaml loader that handles the subset we use; falls back to
# the real PyYAML if installed
try:
    import yaml
    def _load_yaml(path: Path) -> dict:
        return yaml.safe_load(path.read_text()) or {}
except ImportError:
    import json
    def _load_yaml(path: Path) -> dict:
        # accept JSON as a degenerate yaml dialect for our config
        text = path.read_text()
        if path.suffix.lower() == ".json":
            return json.loads(text)
        # very small yaml parser: only top-level scalars + 1-level dicts
        # for users who don't want to install PyYAML. Use JSON for full power.
        out: dict = {}
        stack = [(0, out)]
        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].rstrip()
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip())
            while stack and indent < stack[-1][0]:
                stack.pop()
            head = line.strip()
            if ":" not in head:
                continue
            k, _, v = head.partition(":")
            k = k.strip()
            v = v.strip()
            cur = stack[-1][1]
            if v == "":
                new: dict = {}
                cur[k] = new
                stack.append((indent + 2, new))
            else:
                if v.lower() in ("true", "false"):
                    cur[k] = (v.lower() == "true")
                elif v.startswith("0x"):
                    cur[k] = int(v, 16)
                else:
                    try:
                        cur[k] = int(v)
                    except ValueError:
                        try: cur[k] = float(v)
                        except ValueError:
                            cur[k] = v.strip("'\"")
        return out


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
    ap.add_argument("--target", default=None,
                    choices=["any", "shiny", "perfect6", "perfect5",
                             "shiny+perfect4"],
                    help="override target filter (else use config.yaml)")
    ap.add_argument("--verbose", "-v", action="store_true")
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

    if args.mode:     config["mode"] = args.mode
    if args.game:     config["game"] = args.game
    if args.dry_run:  config.setdefault("input", {})["dry_run"] = True
    if args.starter:  config.setdefault("soft_reset", {})["starter"] = args.starter
    if args.target:   config["target"] = _target_preset(args.target)

    # delayed import so --help works without dependencies
    from pokebot.bot import Bot
    Bot(config).run()


if __name__ == "__main__":
    sys.exit(main())
