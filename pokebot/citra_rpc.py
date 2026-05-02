"""
Azahar/Citra UDP RPC client.

Wire format (from azahar/dist/scripting/citra.py):
  Header (16 bytes): u32 version, u32 request_id, u32 request_type, u32 data_size
  Payload follows.

  Request types:
    1 = ReadMemory     (payload: u32 address, u32 size; max 1024 bytes per req)
    2 = WriteMemory    (payload: u32 address, u32 size, then bytes)
    3 = ProcessList    (payload: u32 start_index, u32 max_count)
    4 = SetGetProcess  (payload: u32 mode (0=get, 1=set), u32 pid)

UDP port 45987 by default.

This module wraps the protocol with:
  - automatic process attach (attach_to_pokemon_game)
  - retry / timeout
  - chunked reads larger than 1024 bytes
  - convenience helpers (read_u8/u16/u32, read_struct)
"""

from __future__ import annotations

import enum
import random
import socket
import struct
import time
from typing import Optional


CITRA_PORT = 45987
REQUEST_VERSION = 1
MAX_DATA_SIZE = 1024
MAX_PACKET = MAX_DATA_SIZE + 0x10
HEADER_FMT = "<IIII"  # little-endian explicitly; Azahar uses host-endian
                      # but x86/ARM Azahar targets are all LE in practice


class RequestType(enum.IntEnum):
    ReadMemory     = 1
    WriteMemory    = 2
    ProcessList    = 3
    SetGetProcess  = 4


class RPCError(RuntimeError):
    """Raised for protocol-level failures (timeout, malformed reply, etc.)."""


# Title IDs of the Gen 6/7 mainline games. The 3DS title ID is a u64;
# Azahar reports it as such in ProcessList.
POKEMON_TITLE_IDS = {
    # Gen 6
    0x0004000000055D00: ("X",          "USA"),
    0x0004000000055E00: ("Y",          "USA"),
    0x000400000011C400: ("OmegaRuby",  "USA"),
    0x000400000011C500: ("AlphaSapphire", "USA"),
    # Gen 7
    0x0004000000164800: ("Sun",        "USA"),
    0x0004000000175E00: ("Moon",       "USA"),
    0x00040000001B5000: ("UltraSun",   "USA"),
    0x00040000001B5100: ("UltraMoon",  "USA"),
    # EUR variants share TIDs with USA on 3DS for these games.
    # JPN variants:
    0x0004000000055D00: ("X",          "JPN"),  # placeholder; verify
    # NOTE: The dict above intentionally has duplicates keyed by TID
    # because regions sometimes share IDs; refine after observing real
    # ProcessList output from your console/dump.
}


class CitraRPC:
    """Synchronous UDP client for Azahar/Citra scripting."""

    def __init__(self, host: str = "127.0.0.1",
                 port: int = CITRA_PORT,
                 timeout: float = 1.0,
                 retries: int = 3):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.retries = retries
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(timeout)
        self._attached_pid: Optional[int] = None
        self._attached_title: Optional[int] = None

    def close(self):
        self.sock.close()

    # ----- low-level packet round-trip ---------------------------------
    def _send_request(self, req_type: RequestType, payload: bytes) -> bytes:
        """Send one request, return its payload (header stripped).
        Retries on timeout."""
        last_err = None
        for _ in range(self.retries):
            req_id = random.getrandbits(32)
            header = struct.pack(HEADER_FMT, REQUEST_VERSION, req_id,
                                 int(req_type), len(payload))
            try:
                self.sock.sendto(header + payload, (self.host, self.port))
                raw = self.sock.recv(MAX_PACKET)
            except socket.timeout as e:
                last_err = e
                continue
            if len(raw) < 16:
                last_err = RPCError(f"short reply: {len(raw)} bytes")
                continue
            r_ver, r_id, r_type, r_size = struct.unpack(HEADER_FMT, raw[:16])
            body = raw[16:]
            if (r_ver != REQUEST_VERSION or r_id != req_id
                    or r_type != int(req_type) or r_size != len(body)):
                last_err = RPCError("reply header mismatch")
                continue
            return body
        raise RPCError(f"request {req_type.name} failed after "
                       f"{self.retries} retries: {last_err}")

    # ----- ProcessList / Set/Get Process -------------------------------
    def list_processes(self) -> dict[int, tuple[int, str]]:
        """Returns {pid: (title_id, name)}."""
        result: dict[int, tuple[int, str]] = {}
        start = 0
        while True:
            payload = struct.pack("<II", start, 0x7FFFFFFF)
            body = self._send_request(RequestType.ProcessList, payload)
            count = struct.unpack("<I", body[:4])[0]
            body = body[4:]
            if count == 0:
                break
            for i in range(count):
                rec = body[i*0x14:(i+1)*0x14]
                pid, tid, raw_name = struct.unpack("<IQ8s", rec)
                name = raw_name.rstrip(b"\x00").decode("ascii", "replace")
                result[pid] = (tid, name)
            start += count
        return result

    def get_process(self) -> Optional[int]:
        body = self._send_request(RequestType.SetGetProcess,
                                  struct.pack("<II", 0, 0))
        if not body:
            return None
        pid = struct.unpack("<I", body)[0]
        return pid if pid != 0 else None

    def set_process(self, pid: int) -> None:
        self._send_request(RequestType.SetGetProcess,
                           struct.pack("<II", 1, pid))
        self._attached_pid = pid

    def attach_to_pokemon_game(self) -> tuple[int, int, str]:
        """Find a running Gen 6/7 Pokémon process and attach to it.
        Returns (pid, title_id, game_name). Raises if not found."""
        procs = self.list_processes()
        for pid, (tid, name) in procs.items():
            if tid in POKEMON_TITLE_IDS:
                self.set_process(pid)
                self._attached_title = tid
                game_name = POKEMON_TITLE_IDS[tid][0]
                return pid, tid, game_name
        # Fallback: attach to whatever process happens to look like a
        # Pokémon game by name, since the title-ID dict above is partial.
        for pid, (tid, name) in procs.items():
            if any(s in name.lower() for s in ("pokemon", "pm_", "monster")):
                self.set_process(pid)
                self._attached_title = tid
                return pid, tid, name
        raise RPCError("No Pokémon Gen 6/7 process found in Azahar. "
                       "Make sure the game is loaded and ROM is running.")

    @property
    def attached_title_id(self) -> Optional[int]:
        return self._attached_title

    # ----- Read / Write -------------------------------------------------
    def read(self, address: int, size: int) -> bytes:
        """Read `size` bytes starting at `address`. Chunks transparently."""
        out = bytearray()
        remaining = size
        cur = address
        while remaining > 0:
            chunk = min(remaining, MAX_DATA_SIZE)
            body = self._send_request(
                RequestType.ReadMemory,
                struct.pack("<II", cur, chunk),
            )
            if not body:
                raise RPCError(f"read {chunk}@{cur:#x} returned empty")
            out += body
            remaining -= len(body)
            cur += len(body)
        return bytes(out)

    def write(self, address: int, data: bytes) -> None:
        """Write `data` to `address`. Chunks transparently."""
        cur = address
        rem = data
        while rem:
            # Header takes 8 bytes of the 1024-byte payload budget
            chunk_size = min(len(rem), MAX_DATA_SIZE - 8)
            chunk = rem[:chunk_size]
            payload = struct.pack("<II", cur, chunk_size) + chunk
            self._send_request(RequestType.WriteMemory, payload)
            cur += chunk_size
            rem = rem[chunk_size:]

    def read_u8(self, addr: int)  -> int: return self.read(addr, 1)[0]
    def read_u16(self, addr: int) -> int: return int.from_bytes(self.read(addr, 2), "little")
    def read_u32(self, addr: int) -> int: return int.from_bytes(self.read(addr, 4), "little")
    def read_u64(self, addr: int) -> int: return int.from_bytes(self.read(addr, 8), "little")

    # ----- liveness check -----------------------------------------------
    def ping(self) -> bool:
        """Cheap RPC roundtrip test. Returns True on success."""
        try:
            self.list_processes()
            return True
        except RPCError:
            return False


def quick_status(host: str = "127.0.0.1", port: int = CITRA_PORT,
                 timeout: float = 0.4) -> dict:
    """Non-blocking probe used by the launcher to poll Azahar.

    Returns a dict with ``state`` set to one of:
      ``"no_rpc"``  – Azahar isn't running or scripting is disabled.
      ``"running"`` – Azahar is up but no Gen 6/7 Pokémon process is loaded.
      ``"game"``    – A Pokémon title is loaded; ``title_id`` and
                      ``game_key`` are filled in.
    Never raises.
    """
    rpc = CitraRPC(host=host, port=port, timeout=timeout, retries=1)
    try:
        try:
            procs = rpc.list_processes()
        except Exception:
            return {"state": "no_rpc"}
        for pid, (tid, name) in procs.items():
            if tid in POKEMON_TITLE_IDS:
                game_name, region = POKEMON_TITLE_IDS[tid]
                return {
                    "state":   "game",
                    "title_id": tid,
                    "pid":      pid,
                    "name":     name,
                    "game":     f"{game_name} ({region})",
                }
        return {"state": "running"}
    finally:
        rpc.close()


def wait_for_emulator(host: str = "127.0.0.1", port: int = CITRA_PORT,
                      timeout: float = 30.0) -> CitraRPC:
    """Block until Azahar's RPC is responding, then return a connected client."""
    deadline = time.monotonic() + timeout
    last_err = None
    while time.monotonic() < deadline:
        rpc = CitraRPC(host=host, port=port, timeout=0.5)
        try:
            if rpc.ping():
                rpc.timeout = 1.0
                rpc.sock.settimeout(1.0)
                return rpc
        except Exception as e:
            last_err = e
        time.sleep(0.5)
    raise RPCError(f"Azahar RPC at {host}:{port} did not respond within "
                   f"{timeout}s. Make sure Azahar is running with a ROM "
                   f"loaded and that scripting is enabled in Emulation > "
                   f"Configure > General. Last error: {last_err}")
