"""
NTR ↔ Azahar bridge.

PKHeX-Plugins' LiveHeX talks to 3DS hardware over the NTR debugger
protocol (TCP, default port 8000). Azahar isn't a 3DS and doesn't
speak NTR — it has its own UDP RPC. This module is a TCP server that
*pretends to be NTR*: it accepts LiveHeX's connection, answers the
process-list / read / write commands, and services each read/write by
calling Azahar's UDP RPC (pokebot.citra_rpc.CitraRPC).

So the data path becomes:

    PKHeX + PKHeX-Plugins  ──NTR/TCP:8000──▶  this bridge  ──UDP:45987──▶  Azahar

Wire format (faithful to PKHeX-Plugins NTRAPIFramework.cs):

  84-byte header, little-endian:
    0x00 u32  magic   = 0x12345678
    0x04 u32  seq
    0x08 u32  type
    0x0C u32  cmd
    0x10 u32  args[16]            (64 bytes)
    0x50 u32  dataLen
  then `dataLen` payload bytes.

  Commands LiveHeX uses:
    cmd 5  ListProcess  → we reply cmd 0 (info) with a process-list
                          string GetGame() can parse (pname + PID).
    cmd 9  ReadMem      args[1]=addr args[2]=size → reply cmd 9 with
                          the bytes, echoing the request seq.
    cmd 10 WriteMem     args[1]=addr args[2]=len, payload=bytes.
    cmd 0  heartbeat    client→server keepalive; we re-arm it by
                          periodically sending our own cmd 0.
"""
from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from typing import Optional

from .citra_rpc import CitraRPC, wait_for_emulator

log = logging.getLogger(__name__)

NTR_MAGIC   = 0x12345678
HEADER_SIZE = 84

# title-id → NTR process name LiveHeX's GetGame() looks for.
# (pnamestr in NTRAPIFramework.cs: kujira-1/2 = X/Y, sango-1/2 =
# OR/AS, niji_loc = SM, momiji = USUM.)
_TITLE_TO_PNAME = {
    0x0004000000055D00: "kujira-1",   # Pokémon X
    0x0004000000055E00: "kujira-2",   # Pokémon Y
    0x000400000011C400: "sango-1",    # Omega Ruby
    0x000400000011C500: "sango-2",    # Alpha Sapphire
    0x0004000000164800: "niji_loc",   # Sun
    0x0004000000175E00: "niji_loc",   # Moon
    0x00040000001B5000: "momiji",     # Ultra Sun
    0x00040000001B5100: "momiji",     # Ultra Moon
}

# Fake PID we hand LiveHeX. The value is arbitrary — every read/write
# is serviced against whatever process Azahar's RPC is attached to —
# but it must be non-(-1) and round-trip through Convert.ToInt32(hex).
_FAKE_PID = 0x11


def _pack_packet(seq: int, type_: int, cmd: int,
                 args=None, data: bytes = b"") -> bytes:
    buf = bytearray(HEADER_SIZE)
    struct.pack_into("<I", buf, 0x00, NTR_MAGIC)
    struct.pack_into("<I", buf, 0x04, seq & 0xFFFFFFFF)
    struct.pack_into("<I", buf, 0x08, type_ & 0xFFFFFFFF)
    struct.pack_into("<I", buf, 0x0C, cmd & 0xFFFFFFFF)
    for i in range(16):
        v = args[i] if args and i < len(args) else 0
        struct.pack_into("<I", buf, 0x10 + i * 4, v & 0xFFFFFFFF)
    struct.pack_into("<I", buf, 0x50, len(data) & 0xFFFFFFFF)
    return bytes(buf) + data


def _recv_exact(sock: socket.socket, n: int,
                stop=None) -> Optional[bytes]:
    """Read exactly ``n`` bytes. A recv timeout is NOT a disconnect —
    the client legitimately goes quiet between requests while it waits
    for our replies. Keep waiting through timeouts; only give up on a
    real EOF / error or when ``stop()`` is True. (socket.timeout is an
    OSError subclass, so it MUST be caught before the generic OSError —
    otherwise the bridge closes the connection after one idle second,
    which is exactly the ~1 s drop that was happening.)
    """
    out = bytearray()
    while len(out) < n:
        if stop is not None and stop():
            return None
        try:
            chunk = sock.recv(n - len(out))
        except socket.timeout:
            continue                       # idle, still connected
        except OSError:
            return None                    # real socket error
        if not chunk:
            return None                    # peer closed
        out += chunk
    return bytes(out)


class NTRBridge:
    def __init__(self, rpc: CitraRPC, title_id: int,
                 host: str = "127.0.0.1", port: int = 8000):
        self.rpc = rpc
        self.title_id = title_id
        self.host = host
        self.port = port
        self.pname = _TITLE_TO_PNAME.get(title_id, "kujira-2")
        self._srv: Optional[socket.socket] = None
        self._stop = threading.Event()

    # ----- process-list reply -----------------------------------------
    def _process_list_text(self) -> bytes:
        """A string GetGame() can parse. It does:
            pname = ", pname:" + name.PadLeft(9)
            pid   = ToInt32(log[indexOf(pname)-10 : ..10], 16)
        So we need exactly ``<10 hex chars>, pname:<sp-padded name>``.
        """
        padded = self.pname.rjust(9)               # PadLeft(9)
        pid10 = f"{_FAKE_PID:010X}"                 # 10 hex chars
        return (f"pid: 0x{_FAKE_PID:08X}, "
                f"{pid10}, pname:{padded}, "
                f"tid: {self.title_id:016X}\n").encode("utf-8")

    # ----- lifecycle ---------------------------------------------------
    def serve_forever(self) -> None:
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((self.host, self.port))
        self._srv.listen(1)
        self._srv.settimeout(0.5)
        log.info(f"NTR bridge listening on {self.host}:{self.port} "
                 f"(emulating process {self.pname!r}, "
                 f"tid {self.title_id:#018x})")
        log.info("In PKHeX → Auto-Legality → LiveHeX, set protocol NTR, "
                 f"IP {self.host}, port {self.port}, then Connect.")
        while not self._stop.is_set():
            try:
                client, addr = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            log.info(f"LiveHeX connected from {addr}")
            try:
                self._handle_client(client)
            except Exception as e:
                log.warning(f"client handler ended: {e}")
            finally:
                try:
                    client.close()
                except OSError:
                    pass
            log.info("LiveHeX disconnected; waiting for a new connection.")
        self._srv.close()

    def stop(self) -> None:
        self._stop.set()

    # ----- per-connection loop ----------------------------------------
    def _handle_client(self, sock: socket.socket) -> None:
        sock.settimeout(1.0)
        # ALL writes go through this lock. The heartbeat thread and the
        # request handler both write to the same socket; unsynchronized
        # sendall() calls interleave their bytes, the client reads a
        # corrupt header, sees magic != 0x12345678, and drops the
        # connection (~1 s in). Serialising every write fixes it.
        send_lock = threading.Lock()

        def _send(pkt: bytes) -> bool:
            with send_lock:
                try:
                    sock.sendall(pkt)
                    return True
                except OSError:
                    return False

        hb_stop = threading.Event()

        def _hb():
            while not hb_stop.is_set() and not self._stop.is_set():
                if not _send(_pack_packet(0, 0, 0)):
                    return
                hb_stop.wait(1.0)

        hb_thread = threading.Thread(target=_hb, daemon=True)

        # Greet with the process list so GetGame() learns pname+PID.
        if not _send(_pack_packet(0, 0, 0,
                                  data=self._process_list_text())):
            return
        hb_thread.start()
        stop_pred = lambda: self._stop.is_set()
        try:
            while not self._stop.is_set():
                hdr = _recv_exact(sock, HEADER_SIZE, stop=stop_pred)
                if hdr is None:
                    break
                magic = struct.unpack_from("<I", hdr, 0x00)[0]
                if magic != NTR_MAGIC:
                    log.warning(f"bad magic {magic:#x}; dropping client")
                    break
                seq  = struct.unpack_from("<I", hdr, 0x04)[0]
                cmd  = struct.unpack_from("<I", hdr, 0x0C)[0]
                args = struct.unpack_from("<16I", hdr, 0x10)
                data_len = struct.unpack_from("<I", hdr, 0x50)[0]
                payload = b""
                if data_len:
                    payload = _recv_exact(sock, data_len,
                                          stop=stop_pred) or b""

                if cmd == 0:
                    _send(_pack_packet(0, 0, 0))      # re-arm heartbeat
                elif cmd == 5:
                    _send(_pack_packet(
                        0, 0, 0, data=self._process_list_text()))
                elif cmd == 9:
                    self._do_read(_send, seq, args)
                elif cmd == 10:
                    self._do_write(args, payload)
                # other cmds: silently ignore (LiveHeX doesn't need them)
        finally:
            hb_stop.set()

    # ----- command handlers -------------------------------------------
    def _do_read(self, send, seq: int, args) -> None:
        addr = args[1]
        size = args[2]
        try:
            data = self.rpc.read(addr, size)
        except Exception as e:
            log.warning(f"read {size}@{addr:#x} failed: {e}")
            data = b"\x00" * size
        if len(data) < size:                       # pad short reads
            data = data + b"\x00" * (size - len(data))
        # Diagnostic: PKHeX validates the connection by reading box1
        # slot1 (232 B) and box reads (slot-sized). Decode what we hand
        # back so we can see exactly why Connect_NTR accepts/rejects.
        note = ""
        if size in (232, 260) and len(data) >= 232:
            try:
                from .parser import decrypt_pkm, calc_checksum
                buf = data if size == 260 else data + b"\x00" * 28
                ek = int.from_bytes(data[:4], "little")
                pt = decrypt_pkm(buf)
                sp = int.from_bytes(pt[8:10], "little")
                st = int.from_bytes(pt[6:8], "little")
                cc = calc_checksum(pt)
                note = (f" | enc_key={ek:#010x} species={sp} "
                        f"csum stored={st:#06x} calc={cc:#06x} "
                        f"match={st == cc}")
            except Exception as e:
                note = f" | decode failed: {e}"
        log.info(f"read {size}@{addr:#010x} "
                 f"first16={data[:16].hex()}{note}")
        send(_pack_packet(seq, 0, 9, data=data))
        # NTRClient.ReadBytes polls for a log line containing
        # "finished" and otherwise blocks the full 10 s timeout per
        # read. Real NTR emits it after a read completes; emit our own
        # cmd 0 info packet so reads return immediately. GetGame()
        # ignores it (no kujira/sango/etc substring).
        send(_pack_packet(0, 0, 0, data=b"finished\n"))

    def _do_write(self, args, payload: bytes) -> None:
        addr = args[1]
        length = args[2]
        if not payload or length == 0:
            return
        try:
            self.rpc.write(addr, payload[:length])
        except Exception as e:
            log.warning(f"write {length}@{addr:#x} failed: {e}")


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

def main(host: str = "127.0.0.1", port: int = 8000) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S")
    log.info("Connecting to Azahar RPC…")
    rpc = wait_for_emulator(timeout=30)
    pid, tid, name = rpc.attach_to_pokemon_game()
    log.info(f"Attached to PID {pid}, TID {tid:#018x} ({name})")
    bridge = NTRBridge(rpc, tid, host=host, port=port)
    try:
        bridge.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down.")
        bridge.stop()


if __name__ == "__main__":
    main()
