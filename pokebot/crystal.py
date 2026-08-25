"""
Live reading of a running Pokemon Crystal (Virtual Console) session.

Crystal's Game Boy WRAM lives at an undocumented host address inside
Azahar, so every read here is relative to a base discovered by scanning
for the party block (see :func:`locate_wram`). Once found, the base is
cached so later runs start instantly, and re-validated on use so a
moved or stale base is re-scanned rather than silently read as garbage.

Nothing here needs Celebi, or any particular story progress: the party
block exists from the moment you have one Pokemon, so the whole stack —
locate, read, evaluate DVs — is exercisable at the start of a save.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from . import gen2

log = logging.getLogger(__name__)

#: Bytes needed to validate a party block: count + list + 6 records.
SIGNATURE_LEN = 8 + gen2.PARTY_STRUCT_SIZE * 6

#: Read size per RPC round trip; chunks overlap by SIGNATURE_LEN.
CHUNK = 0x8000

#: Where a discovered base is remembered between runs.
CACHE_PATH = Path(__file__).resolve().parent.parent / ".crystal_wram.json"

#: Battle mode (GB 0xD22D): 0 overworld, 1 wild, 2 trainer.
GB_BATTLE_MODE = 0xD22D
BATTLE_NONE, BATTLE_WILD, BATTLE_TRAINER = 0, 1, 2

#: Enemy battle structure. PROVISIONAL — the in-battle struct is NOT
#: the party struct (its DVs do not sit at +0x15), so these come from
#: Data Crystal's map rather than from a verified parse. Treat any
#: enemy reading as unconfirmed until checked against a Pokemon whose
#: DVs are known from the party after catching it.
GB_ENEMY_SPECIES = 0xD206
GB_ENEMY_DVS = 0xD20D          # 2 bytes: Atk/Def then Spe/Spc
GB_ENEMY_LEVEL = 0xD213


def _gb(addr: int) -> int:
    """GB address -> offset from the start of WRAM."""
    return addr - gen2.GB_WRAM_LO


@dataclass(frozen=True)
class EnemyReading:
    """A provisional read of the in-battle opponent."""
    species: int
    level: int
    dvs: dict
    shiny: bool
    confirmed: bool = False


def scan_range(rpc, start: int, end: int, stop_after: int = 0,
               progress_every: float = 10.0) -> list[dict]:
    """Scan ``[start, end)`` for Crystal party blocks."""
    hits: list[dict] = []
    cur = start
    t0 = last_report = time.monotonic()

    while cur < end:
        size = min(CHUNK, end - cur)
        if size < SIGNATURE_LEN:
            break
        try:
            buf = rpc.read(cur, size)
        except Exception:
            cur += CHUNK          # unmapped page: skip, don't abort
            continue

        limit = len(buf) - SIGNATURE_LEN
        off = 0
        while off <= limit:
            if gen2.looks_like_party(buf, off):
                addr = cur + off
                hits.append({
                    "party_addr": addr,
                    "wram_base": addr - gen2.PARTY_COUNT_FROM_WRAM,
                    "party": gen2.read_party(buf, off),
                })
                if stop_after and len(hits) >= stop_after:
                    return hits
                off += SIGNATURE_LEN
                continue
            off += 1

        now = time.monotonic()
        if progress_every and now - last_report > progress_every:
            done, total = cur - start, end - start
            log.info(f"  {done / total:5.1%}  {cur:#010x}  "
                     f"{done / max(1e-6, now - t0) / 1048576:.2f} MB/s  "
                     f"hits={len(hits)}")
            last_report = now

        # Always advance: at a range tail `size` can equal
        # SIGNATURE_LEN and a zero step would spin forever.
        cur += max(1, size - SIGNATURE_LEN)

    return hits


def _load_cached_base() -> int | None:
    try:
        return int(json.loads(CACHE_PATH.read_text())["wram_base"])
    except Exception:
        return None


def _save_cached_base(base: int) -> None:
    try:
        CACHE_PATH.write_text(json.dumps({"wram_base": base}))
    except Exception as exc:
        log.warning(f"could not cache the WRAM base: {exc}")


def verify_base(rpc, base: int) -> bool:
    """Does a party block really sit at ``base``'s expected offset?"""
    try:
        buf = rpc.read(base + gen2.PARTY_COUNT_FROM_WRAM, SIGNATURE_LEN)
    except Exception:
        return False
    return gen2.looks_like_party(buf, 0)


def locate_wram(rpc, ranges, use_cache: bool = True) -> int | None:
    """Find the emulated WRAM base, preferring a validated cache."""
    if use_cache:
        cached = _load_cached_base()
        if cached is not None and verify_base(rpc, cached):
            log.info(f"WRAM base {cached:#010x} (cached, verified)")
            return cached
        if cached is not None:
            log.info("cached WRAM base no longer valid; re-scanning")

    for start, end in ranges:
        log.info(f"scanning {start:#010x}-{end:#010x} for the party block…")
        hits = scan_range(rpc, start, end, stop_after=1)
        if hits:
            base = hits[0]["wram_base"]
            log.info(f"WRAM base {base:#010x} "
                     f"(party block @ {hits[0]['party_addr']:#010x})")
            _save_cached_base(base)
            return base
    return None


class CrystalSession:
    """Reads a located Crystal session. Re-locates if the base goes stale."""

    def __init__(self, rpc, ranges, base: int | None = None):
        self.rpc = rpc
        self.ranges = ranges
        self.base = base

    def ensure_base(self) -> int | None:
        if self.base is not None and verify_base(self.rpc, self.base):
            return self.base
        self.base = locate_wram(self.rpc, self.ranges)
        return self.base

    def _read(self, gb_addr: int, size: int) -> bytes | None:
        if self.base is None:
            return None
        try:
            return self.rpc.read(self.base + _gb(gb_addr), size)
        except Exception:
            return None

    def party(self) -> list:
        """The live party, or [] if it cannot be read."""
        if self.ensure_base() is None:
            return []
        buf = self._read(gen2.GB_PARTY_COUNT, SIGNATURE_LEN)
        if buf is None or not gen2.looks_like_party(buf, 0):
            return []
        return gen2.read_party(buf, 0)

    def battle_mode(self) -> int:
        buf = self._read(GB_BATTLE_MODE, 1)
        return buf[0] if buf else BATTLE_NONE

    def enemy(self) -> EnemyReading | None:
        """PROVISIONAL read of the opponent — see the module notes.

        Returns None outside a battle. The DV offsets are unverified,
        so ``confirmed`` is always False: use this to compare against a
        Pokemon whose real DVs you can check from the party, not as a
        basis for deciding a hunt.
        """
        if self.ensure_base() is None:
            return None
        if self.battle_mode() == BATTLE_NONE:
            return None
        species = self._read(GB_ENEMY_SPECIES, 1)
        level = self._read(GB_ENEMY_LEVEL, 1)
        dv_raw = self._read(GB_ENEMY_DVS, 2)
        if not species or not level or not dv_raw:
            return None
        dvs = gen2.parse_dvs(int.from_bytes(dv_raw, "big"))
        return EnemyReading(species=species[0], level=level[0], dvs=dvs,
                            shiny=gen2.is_shiny(dvs), confirmed=False)
