"""
Live stream overlays: a tiny local web server for OBS browser sources.

The launcher publishes a state snapshot (phase, odds, last encounter,
recent shinies) after every encounter; the overlay page polls
``/state.json`` once a second. Pages: ``/bar`` (a compact counter strip)
and ``/panel`` (the full stats card). Bound to 127.0.0.1 only, so
nothing on the network can read it.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_PORT = 8765
LAYOUTS = ("bar", "panel")
_PAGE = Path(__file__).resolve().parent / "overlay" / "overlay.html"


class _Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second server bind a port that is in
    # use, so two launchers would both "own" it and OBS would reach
    # either; elsewhere it only skips TIME_WAIT after a restart.
    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True


class OverlayServer:
    def __init__(self, port: int = DEFAULT_PORT):
        self._state = b"{}"
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (http.server's name)
                path = self.path.split("?", 1)[0].strip("/")
                if path == "state.json":
                    self._send(server._state, "application/json")
                elif path in LAYOUTS or path == "":
                    self._send(_PAGE.read_bytes(), "text/html; charset=utf-8")
                else:
                    self.send_error(404)

            def _send(self, body: bytes, ctype: str):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):     # one GET a second per source
                pass

        self._httpd = _Server(("127.0.0.1", port), Handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True,
                         name="StreamOverlay").start()

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def url(self, layout: str) -> str:
        return f"http://127.0.0.1:{self.port}/{layout}"

    def publish(self, state: dict) -> None:
        """Replace the snapshot the pages read (one reference swap, so a
        request never sees half an update)."""
        self._state = json.dumps(state).encode()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def start(port: int = DEFAULT_PORT) -> Optional[OverlayServer]:
    """The running server, or None if the port is taken."""
    try:
        return OverlayServer(port)
    except OSError as exc:
        log.warning(f"stream overlay not started: port {port} is busy ({exc})")
        return None
