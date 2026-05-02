"""
Embedded websocket server for the dashboard.

Runs in a background thread inside the bot process. Broadcasts JSON
messages to all connected dashboard clients whenever the bot has
something to share (party update, encounter logged, mode change, etc.).

Uses only Python stdlib (no `websockets` dependency) by implementing the
small subset of RFC 6455 we need. This keeps the install footprint to
just `pynput` (and even that's optional).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import socket
import struct
import threading
import time
from queue import Queue, Empty
from typing import Iterable

log = logging.getLogger(__name__)

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_handshake(req: bytes) -> bytes:
    """Build a 101 Switching Protocols response from a client handshake."""
    headers = req.decode("latin-1", "replace").split("\r\n")
    key = ""
    for h in headers:
        if h.lower().startswith("sec-websocket-key:"):
            key = h.split(":", 1)[1].strip()
            break
    accept = base64.b64encode(
        hashlib.sha1((key + WS_GUID).encode()).digest()
    ).decode()
    return ("HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode()


def _ws_send(sock: socket.socket, payload: bytes) -> None:
    """Send a single text frame (no fragmentation, server -> client unmasked)."""
    header = bytearray([0x81])  # FIN + text
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header += bytes([126]) + struct.pack(">H", n)
    else:
        header += bytes([127]) + struct.pack(">Q", n)
    sock.sendall(bytes(header) + payload)


class DashboardServer:
    """One-way push: the bot publishes messages, all clients receive them.

    Clients can also send messages back (e.g. UI commands), which are
    enqueued in `inbox` for the bot to consume."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8765):
        self.host = host
        self.port = port
        self._stop = threading.Event()
        self._clients: list[socket.socket] = []
        self._clients_lock = threading.Lock()
        self.inbox: Queue = Queue()      # incoming messages from clients
        self._thread: threading.Thread | None = None

    # ----- lifecycle ----------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._serve_forever,
                                        name="DashboardServer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._clients_lock:
            for c in self._clients:
                try: c.close()
                except Exception: pass

    # ----- broadcasting -------------------------------------------------
    def broadcast(self, msg_type: str, **fields) -> None:
        """Send {"type": msg_type, "ts": ..., **fields} to all clients."""
        payload = json.dumps(
            {"type": msg_type, "ts": time.time(), **fields},
            default=str,
        ).encode()
        dead = []
        with self._clients_lock:
            for c in self._clients:
                try:
                    _ws_send(c, payload)
                except Exception:
                    dead.append(c)
            for c in dead:
                self._clients.remove(c)
                try: c.close()
                except Exception: pass

    # ----- internals ----------------------------------------------------
    def _serve_forever(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.settimeout(0.5)
        try:
            srv.bind((self.host, self.port))
        except OSError as e:
            log.error(f"Dashboard server failed to bind {self.host}:{self.port}: {e}")
            return
        srv.listen(8)
        log.info(f"Dashboard websocket server on ws://{self.host}:{self.port}")
        while not self._stop.is_set():
            try:
                client, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_client,
                             args=(client,), daemon=True).start()
        srv.close()

    def _handle_client(self, sock: socket.socket) -> None:
        try:
            sock.settimeout(5)
            req = sock.recv(4096)
            if b"Upgrade: websocket" not in req and b"upgrade: websocket" not in req:
                # Allow plain GET / for sanity check
                sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                             b"Connection: close\r\n\r\n"
                             b"pokebot-3ds dashboard endpoint -- "
                             b"connect with WebSocket.\n")
                sock.close()
                return
            sock.sendall(_ws_handshake(req))
            sock.settimeout(None)
        except Exception as e:
            log.debug(f"handshake failed: {e}")
            sock.close()
            return

        with self._clients_lock:
            self._clients.append(sock)
        log.info(f"Dashboard client connected ({len(self._clients)} total)")
        # consume frames from the client (control + text)
        try:
            while not self._stop.is_set():
                hdr = sock.recv(2)
                if len(hdr) < 2: break
                opcode = hdr[0] & 0x0F
                masked = hdr[1] & 0x80
                length = hdr[1] & 0x7F
                if length == 126:
                    length = struct.unpack(">H", sock.recv(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", sock.recv(8))[0]
                mask = sock.recv(4) if masked else b""
                data = b""
                while len(data) < length:
                    data += sock.recv(length - len(data))
                if masked:
                    data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
                if opcode == 0x8:           # close
                    break
                if opcode == 0x1:           # text frame
                    try:
                        self.inbox.put(json.loads(data.decode()))
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            with self._clients_lock:
                if sock in self._clients:
                    self._clients.remove(sock)
            try: sock.close()
            except Exception: pass


def open_dashboard_in_browser(html_path: str) -> None:
    """Helper: open the dashboard.html in the user's default browser."""
    import webbrowser
    webbrowser.open("file://" + os.path.abspath(html_path))
