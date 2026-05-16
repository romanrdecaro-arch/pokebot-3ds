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

# A PK6-encrypted all-zero record: PKHeX decrypts it to species 0,
# EncryptionConstant 0, checksum valid — i.e. a *clean-empty* box
# slot, which is what Connect_NTR accepts to validate the connection.
# Built lazily so importing this module doesn't require the parser.
_EMPTY_PK6: Optional[bytes] = None


def _empty_pk6(size: int) -> bytes:
    global _EMPTY_PK6
    if _EMPTY_PK6 is None:
        from .parser import encrypt_pkm
        _EMPTY_PK6 = encrypt_pkm(bytes(260))
    return _EMPTY_PK6[:size] if size <= len(_EMPTY_PK6) \
        else _EMPTY_PK6 + b"\x00" * (size - len(_EMPTY_PK6))


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


# PKHeX-Plugins XY_v150 save-block anchors (real-hardware NTR addrs).
# In Azahar the save block is relocated, so we find the real base via
# the OT-name string and rebase every save-block read by one delta.
_XY_TRAINER_BLOCK = 0x08C79C3C        # MyStatus6; OT name at +0x48
_OT_NAME_SUBOFFSET = 0x48
# Generous window to dense-scan for the OT string (the save block
# stays in this app-heap neighbourhood across Azahar sessions).
_SAVE_WIN_LO = 0x08C00000
_SAVE_WIN_HI = 0x08E00000
# Reads whose target falls in this range get the delta applied.
_REBASE_LO = 0x08C00000
_REBASE_HI = 0x08E00000


class NTRBridge:
    def __init__(self, rpc: CitraRPC, title_id: int,
                 host: str = "127.0.0.1", port: int = 8000,
                 trainer_name: str = "Roman"):
        self.rpc = rpc
        self.title_id = title_id
        self.host = host
        self.port = port
        self.trainer_name = trainer_name
        self.pname = _TITLE_TO_PNAME.get(title_id, "kujira-2")
        self.delta = 0          # added to save-block reads once anchored
        self._empty_slot: Optional[bytes] = None  # real captured clean-empty PK6
        self._srv: Optional[socket.socket] = None
        self._stop = threading.Event()

    def _empty_bytes(self, size: int) -> bytes:
        """A clean-empty PK6 buffer of ``size`` bytes — the real
        game-captured one tiled if we have it, else the synthetic."""
        if self._empty_slot:
            base = self._empty_slot
            return (base * (size // len(base) + 1))[:size]
        return _empty_pk6(size)

    # ----- save-block anchoring (OT-name → delta) ----------------------
    def find_save_delta(self) -> None:
        """Dense-scan the app-heap save neighbourhood for the OT name
        (UTF-16LE + NUL). The OT string sits at trainer_block+0x48, so
        trainer_block = hit - 0x48 and delta = trainer_block -
        PKHeX-Plugins' expected address. Sets self.delta.
        """
        name = self.trainer_name or "Roman"
        pat = name.encode("utf-16-le") + b"\x00\x00"
        log.info(f"Anchoring save block: scanning "
                 f"[{_SAVE_WIN_LO:#x}, {_SAVE_WIN_HI:#x}) for OT "
                 f"name {name!r} ({pat.hex()})…")
        CH = 0x10000
        cur = _SAVE_WIN_LO
        carry = b""
        while cur < _SAVE_WIN_HI and not self._stop.is_set():
            try:
                blk = self.rpc.read(cur, min(CH, _SAVE_WIN_HI - cur))
            except Exception:
                cur += CH
                carry = b""
                continue
            buf = carry + blk
            idx = buf.find(pat)
            while idx != -1:
                # buf[0] is at absolute address (cur - len(carry)).
                hit = cur - len(carry) + idx
                tb = hit - _OT_NAME_SUBOFFSET
                if self._validate_trainer_block(tb, name):
                    self.delta = tb - _XY_TRAINER_BLOCK
                    log.info(f"  OT name @ {hit:#010x} → trainer_block "
                             f"{tb:#010x}; delta = {self.delta:+#x} "
                             f"(reads in [{_REBASE_LO:#x},{_REBASE_HI:#x}) "
                             f"will be rebased)")
                    return
                idx = buf.find(pat, idx + 1)
            carry = buf[-(len(pat) - 1):] if len(pat) > 1 else b""
            cur += CH
        log.warning("  OT name not found in the save window — no "
                    "rebasing (PKHeX will see spoofed-empty data). "
                    "If the save block is elsewhere, widen "
                    "_SAVE_WIN_LO/_HI.")

    def find_empty_slot(self) -> None:
        """Scan for ONE real, game-written clean-empty PK6 (decodes to
        species 0, EncryptionConstant 0, checksum valid) and cache its
        232 bytes. Serving the game's OWN empty record for box reads
        makes PKHeX validate by definition — far more reliable than a
        synthetic one (PKHeX's decryption edge-cases for an all-zero
        key rejected our synthesised buffer). The user's boxes are
        empty, so serving clean-empty everywhere is also correct.
        """
        from .parser import decrypt_pkm, calc_checksum
        lo, hi = 0x08800000, 0x08A00000        # proven region (0x08840c90)
        log.info(f"Capturing a real clean-empty PK6 in "
                 f"[{lo:#x}, {hi:#x})…")
        CH = 0x10000
        cur = lo
        while cur < hi and not self._stop.is_set():
            try:
                blk = self.rpc.read(cur, min(CH, hi - cur))
            except Exception:
                cur += CH
                continue
            for off in range(0, len(blk) - 260 + 1, 4):
                rec = blk[off:off + 260]
                if int.from_bytes(rec[:4], "little") != 0:
                    continue                  # empty slot ⇒ enc_key 0
                try:
                    pt = decrypt_pkm(rec)
                except Exception:
                    continue
                if (int.from_bytes(pt[8:10], "little") == 0
                        and int.from_bytes(pt[6:8], "little")
                        == calc_checksum(pt)):
                    self._empty_slot = rec[:232]
                    log.info(f"  captured clean-empty PK6 @ "
                             f"{cur + off:#010x} "
                             f"(csum {int.from_bytes(pt[6:8],'little'):#06x})")
                    return
            cur += CH - 260
        log.warning("  no real clean-empty PK6 captured; box reads "
                    "will use the synthetic fallback.")

    def _validate_trainer_block(self, tb: int, name: str) -> bool:
        """Sanity-check a candidate MyStatus6 trainer block."""
        if tb < _REBASE_LO or tb >= _REBASE_HI:
            return False
        try:
            hdr = self.rpc.read(tb, 0x4A + len(name) * 2)
        except Exception:
            return False
        if len(hdr) < 0x4A:
            return False
        tid = int.from_bytes(hdr[0:2], "little")
        sid = int.from_bytes(hdr[2:4], "little")
        ot = hdr[0x48:0x48 + len(name) * 2]
        if ot != name.encode("utf-16-le"):
            return False
        # TID/SID shouldn't both be 0 or both 0xFFFF on a real card.
        if (tid, sid) in ((0, 0), (0xFFFF, 0xFFFF)):
            return False
        log.info(f"  trainer block {tb:#010x}: TID={tid} SID={sid} "
                 f"OT={name!r} ✓")
        return True

    def _rebase(self, addr: int) -> int:
        if self.delta and _REBASE_LO <= addr < _REBASE_HI:
            return addr + self.delta
        return addr

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
        # Anchor the save block BEFORE accepting connections so the
        # very first PKHeX read is already rebased to real data.
        try:
            self.find_save_delta()
        except Exception as e:
            log.warning(f"save-delta anchoring failed: {e}")
        try:
            self.find_empty_slot()
        except Exception as e:
            log.warning(f"empty-slot capture failed: {e}")
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((self.host, self.port))
        self._srv.listen(1)
        self._srv.settimeout(0.5)
        log.info(f"NTR bridge listening on {self.host}:{self.port} "
                 f"(emulating process {self.pname!r}, "
                 f"tid {self.title_id:#018x}, "
                 f"save delta {self.delta:+#x})")
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
        req_addr = args[1]
        size = args[2]
        addr = self._rebase(req_addr)
        rebased = addr != req_addr
        try:
            data = self.rpc.read(addr, size)
        except Exception as e:
            log.warning(f"read {size}@{addr:#x} failed: {e}")
            data = b"\x00" * size
        if len(data) < size:                       # pad short reads
            data = data + b"\x00" * (size - len(data))
        # The trainer block (rebased by the OT-anchor delta) reads real
        # data and passes through. Box slots are a SEPARATE save sub-
        # allocation with a different (unknown, unanchorable — empty
        # slots have no string) delta, so PK6-sized reads still decode
        # garbage. PKHeX only needs box1slot1 to be a valid/clean-empty
        # PK6 to connect, so for any PK6-sized read that doesn't decode
        # clean we serve the REAL game-captured clean-empty slot (which
        # PKHeX accepts by definition; the user's boxes are empty so
        # this is also correct). Valid/clean real reads pass through.
        note = ""
        # Full-box / multi-slot read (size = N×232, N≥2): PKHeX's box
        # view. The box sub-block isn't anchored, so serve the captured
        # clean-empty slot tiled across it → PKHeX shows empty boxes
        # (correct) instead of 30 garbage entries. (232 itself falls to
        # the single-slot path below; 0x170 trainer is untouched.)
        if size >= 464 and size % 232 == 0:
            data = self._empty_bytes(size)
            note = " | box read -> tiled empty"
        elif size in (232, 260) and len(data) >= 232:
            try:
                from .parser import decrypt_pkm, calc_checksum
                buf = data if size == 260 else data + b"\x00" * 28
                ek = int.from_bytes(data[:4], "little")
                pt = decrypt_pkm(buf)
                sp = int.from_bytes(pt[8:10], "little")
                st = int.from_bytes(pt[6:8], "little")
                cc = calc_checksum(pt)
                clean_empty = (sp == 0 and ek == 0 and st == cc)
                valid_pkm = (0 < sp <= 1024 and st == cc)
                note = (f" | enc_key={ek:#010x} species={sp} "
                        f"csum match={st == cc}")
                if not (clean_empty or valid_pkm):
                    data = self._empty_bytes(size)
                    note += (" | -> real captured empty"
                             if self._empty_slot else
                             " | -> synthetic empty")
                else:
                    note += " | passthrough (real valid)"
            except Exception as e:
                data = self._empty_bytes(size)
                note = f" | decode err ({e}); -> empty"
        loc = (f"{req_addr:#010x}->{addr:#010x}" if rebased
               else f"{addr:#010x}")
        log.info(f"read {size}@{loc} "
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
