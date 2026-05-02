"""
Main bot orchestrator.

Responsibilities:
  - Connect to Azahar RPC, attach to a Gen 6/7 game.
  - Load the matching Game entry from the registry.
  - Spin up the dashboard websocket server.
  - Spin up the input driver.
  - Dispatch to the configured mode.
  - Handle Ctrl+C and dashboard "stop" commands cleanly.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from dataclasses import dataclass
from typing import Optional

from . import games as games_mod
from .citra_rpc import CitraRPC, wait_for_emulator
from .dashboard_server import DashboardServer
from .input_driver import InputDriver, KeyBinds
from .modes import MODES
from .targets import Target, target_from_dict

log = logging.getLogger(__name__)


@dataclass
class BotContext:
    """Passed into mode functions; shared mutable state of the bot."""
    rpc:        CitraRPC
    game:       games_mod.Game
    dashboard:  DashboardServer
    input:      InputDriver
    target:     Optional[Target]
    config:     dict
    _stop_evt:  threading.Event

    def should_stop(self) -> bool:
        return self._stop_evt.is_set()

    def request_stop(self, reason: str = "") -> None:
        if reason:
            log.info(f"stop requested: {reason}")
        self._stop_evt.set()


class Bot:
    def __init__(self, config: dict):
        self.config = config
        self._stop_evt = threading.Event()
        self.rpc: CitraRPC | None = None
        self.dashboard: DashboardServer | None = None
        self.input: InputDriver | None = None
        self.game: games_mod.Game | None = None
        self.target: Target | None = None

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------
    def _connect(self) -> None:
        rpc_cfg = self.config.get("rpc", {})
        log.info("Waiting for Azahar/Citra RPC...")
        self.rpc = wait_for_emulator(
            host=rpc_cfg.get("host", "127.0.0.1"),
            port=int(rpc_cfg.get("port", 45987)),
            timeout=float(rpc_cfg.get("connect_timeout", 30)),
        )
        log.info("RPC connected.")

        # Attach to the running game
        game_override = self.config.get("game")
        if game_override and game_override in games_mod.GAMES:
            self.game = games_mod.GAMES[game_override]
            log.info(f"Using configured game: {self.game.display}")
            # still need a process attach
            try:
                pid, tid, name = self.rpc.attach_to_pokemon_game()
                log.info(f"Attached to PID {pid}, TID {tid:#018x} ({name})")
            except Exception as e:
                log.warning(f"Process attach failed: {e}. Running in "
                            f"detached mode -- reads will use the currently "
                            f"selected Azahar process if any.")
        else:
            pid, tid, name = self.rpc.attach_to_pokemon_game()
            log.info(f"Attached to PID {pid}, TID {tid:#018x} ({name})")
            g = games_mod.find_game_by_title_id(tid)
            if not g:
                log.warning(f"TID {tid:#018x} not in registry; "
                            f"using empty offsets. Configure 'game' "
                            f"explicitly in config.yaml or add this TID "
                            f"to games.py.")
                g = games_mod.Game(
                    key=f"unknown-{tid:016x}",
                    title=f"Unknown ({tid:#018x})",
                    title_ids=(tid,),
                    generation=7,
                )
            self.game = g
            log.info(f"Game: {self.game.display}")

        # Apply per-run offset overrides from config.yaml [offsets:] section.
        offset_cfg = self.config.get("offsets") or {}
        if offset_cfg:
            applied = []
            for key, val in offset_cfg.items():
                if hasattr(self.game.offsets, key):
                    parsed = int(val, 0) if isinstance(val, str) else int(val)
                    if parsed:
                        setattr(self.game.offsets, key, parsed)
                        applied.append(f"{key}={parsed:#010x}")
            if applied:
                log.info(f"Config offset overrides: {', '.join(applied)}")

        if not (self.game.offsets.party_base or self.game.offsets.foe_base):
            # soft_reset will auto-discover after the first starter pickup,
            # so this isn't fatal — keep it informational, not a warning.
            log.info("No memory offsets configured yet. soft_reset will "
                     "auto-discover party_base after the first starter is "
                     "in the party; other modes will skip reads until then.")
        elif not self.game.verified:
            log.warning("This game's offsets are NOT verified -- expect "
                        "many features to be no-ops until you verify "
                        "addresses with `python -m pokebot.find_offsets`.")

    def _start_dashboard(self) -> None:
        dash_cfg = self.config.get("dashboard", {})
        if not dash_cfg.get("enabled", True):
            self.dashboard = DashboardServer()  # not started
            return
        self.dashboard = DashboardServer(
            host=dash_cfg.get("host", "127.0.0.1"),
            port=int(dash_cfg.get("port", 8765)),
        )
        self.dashboard.start()

    def _start_input(self) -> None:
        input_cfg = self.config.get("input", {})
        binds = KeyBinds(**input_cfg.get("binds", {}))
        dry = bool(input_cfg.get("dry_run", False))
        self.input = InputDriver(binds=binds, dry_run=dry)

    def _load_target(self) -> None:
        self.target = target_from_dict(self.config.get("target") or {})

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------
    def run(self) -> None:
        self._install_signal_handlers()
        self._connect()
        self._start_dashboard()
        self._start_input()
        self._load_target()

        ctx = BotContext(
            rpc=self.rpc,                     # type: ignore[arg-type]
            game=self.game,                   # type: ignore[arg-type]
            dashboard=self.dashboard,         # type: ignore[arg-type]
            input=self.input,                 # type: ignore[arg-type]
            target=self.target,
            config=self.config,
            _stop_evt=self._stop_evt,
        )

        # background thread: process inbound dashboard commands
        threading.Thread(target=self._dashboard_command_loop,
                         args=(ctx,), name="DashCmd",
                         daemon=True).start()

        mode_name = self.config.get("mode", "observe")
        if mode_name not in MODES:
            raise ValueError(f"Unknown mode: {mode_name}. "
                             f"Available: {list(MODES)}")

        # Initial status broadcast
        self.dashboard.broadcast("status",
            mode=mode_name,
            game=self.game.display,
            verified_offsets=self.game.verified,
            party_offset=f"{self.game.offsets.party_base:#010x}",
            foe_offset=f"{self.game.offsets.foe_base:#010x}",
            offsets_configured=bool(self.game.offsets.party_base
                                    or self.game.offsets.foe_base),
        )

        log.info(f"Starting mode: {mode_name}")
        try:
            MODES[mode_name](ctx)
        finally:
            self._cleanup()

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------
    def _dashboard_command_loop(self, ctx: BotContext) -> None:
        while not ctx.should_stop():
            try:
                msg = ctx.dashboard.inbox.get(timeout=0.5)
            except Exception:
                continue
            cmd = (msg or {}).get("cmd", "")
            log.info(f"dashboard cmd: {cmd}")
            if cmd == "stop":
                ctx.request_stop("dashboard")
            elif cmd == "ping":
                ctx.dashboard.broadcast("pong")

    def _install_signal_handlers(self) -> None:
        def _handler(sig, frame):
            log.info("Caught signal -- stopping.")
            self._stop_evt.set()
        try:
            signal.signal(signal.SIGINT, _handler)
            signal.signal(signal.SIGTERM, _handler)
        except ValueError:
            pass  # not the main thread

    def _cleanup(self) -> None:
        log.info("Cleaning up...")
        if self.input:     self.input.close()
        if self.dashboard: self.dashboard.stop()
        if self.rpc:       self.rpc.close()
        log.info("Bye.")
