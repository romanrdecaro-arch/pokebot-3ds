"""
LiveHeX bridge mode.

Sends NO inputs. Stands up the NTR↔Azahar bridge so PKHeX +
PKHeX-Plugins LiveHeX can read/write the game through Azahar's RPC.

Use this when you want PKHeX's full, battle-tested GUI (box editor,
legality, trainer data, …) against the running Azahar game instead of
the bot parsing raw RAM itself.
"""
from __future__ import annotations

import logging

from ..ntr_bridge import NTRBridge

log = logging.getLogger(__name__)


def run(ctx) -> None:
    tid = ctx.game.title_ids[0] if ctx.game.title_ids else 0
    log.info("Mode: livehex (NTR bridge for PKHeX-Plugins)")
    rpc_cfg = ctx.config.get("rpc", {})
    host = rpc_cfg.get("livehex_host", "127.0.0.1")
    port = int(rpc_cfg.get("livehex_port", 8000))
    bridge = NTRBridge(ctx.rpc, tid, host=host, port=port)

    # Stop the bridge when the launcher requests a stop.
    import threading
    threading.Thread(
        target=lambda: (ctx._stop_evt.wait(), bridge.stop()),
        daemon=True).start()

    bridge.serve_forever()
