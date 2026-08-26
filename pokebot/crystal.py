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

#: How far either side of the LAST KNOWN base to look before falling
#: back to a full-heap sweep.
_NEARBY = 0x400000

#: How far either side of the first hit to look for its sibling copies.
#: Observed spacing between the live WRAM and the save buffers was well
#: under 64 KB; 256 KB is generous and costs a fraction of a second.
_NEIGHBOURHOOD = 0x40000

#: Where a discovered base is remembered between runs.
CACHE_PATH = Path(__file__).resolve().parent.parent / ".crystal_wram.json"

#: Battle mode (GB 0xD22D): 0 overworld, 1 wild, 2 trainer.
GB_BATTLE_MODE = 0xD22D
BATTLE_NONE, BATTLE_WILD, BATTLE_TRAINER = 0, 1, 2

#: wEnemyMon uses the Gen 2 battle_struct layout, confirmed against a
#: live wild battle: species, item, 4 moves, DVs, 4 PP, happiness,
#: level. Every field lined up at once (Oddish, Absorb, happiness 70,
#: level 5), which is what pins the DVs to +6 rather than the D20D the
#: RAM map suggested.
GB_ENEMY_SPECIES = 0xD206
GB_ENEMY_DVS = 0xD20C          # wEnemyMon + 6
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
        # Prefilter in C rather than calling looks_like_party on every
        # byte: the species list always ends with an 0xFF terminator at
        # off+7, so bytes.find jumps straight between candidates. Over a
        # multi-hundred-megabyte heap this is the difference between a
        # scan that finishes and one that does not.
        pos = 0
        while True:
            idx = buf.find(b"\xff", pos)
            if idx < 0:
                break
            off = idx - 7
            pos = idx + 1
            if off < 0 or off > limit:
                continue
            if not 1 <= buf[off] <= 6:        # party count, cheap reject
                continue
            if not gen2.looks_like_party(buf, off):
                continue
            addr = cur + off
            hits.append({
                "party_addr": addr,
                "wram_base": addr - gen2.PARTY_COUNT_FROM_WRAM,
                "party": gen2.read_party(buf, off),
            })
            if stop_after and len(hits) >= stop_after:
                return hits
            pos = off + SIGNATURE_LEN

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


def churn(rpc, base: int, size: int = 0x2000,
          gap: float = 0.4) -> int:
    """How many bytes change over ``gap`` seconds — a liveness probe.

    Several regions hold a valid-looking party block: the live WRAM and
    the save/backup buffers the game keeps. They are indistinguishable
    by the party signature alone, but only the live one ticks — its
    clock, RNG and sprite state change constantly, while a save buffer
    is static. Measured on a real session: live 57 bytes, buffers 14
    and 0.
    """
    try:
        a = rpc.read(base, size)
        time.sleep(gap)
        b = rpc.read(base, size)
    except Exception:
        return -1
    return sum(1 for i in range(min(len(a), len(b))) if a[i] != b[i])


def is_live_base(rpc, base: int, min_churn: int = 1) -> bool:
    """Is this the LIVE WRAM rather than a save buffer copy?

    Deliberately generous: only a base that does not tick AT ALL is
    rejected. A stricter threshold would reject the real thing whenever
    the game sits on a quiet screen, and each rejection costs a full
    re-scan — far worse than briefly tolerating a dull-looking base.
    """
    return churn(rpc, base) >= min_churn


def locate_wram(rpc, ranges, use_cache: bool = True,
                out_candidates: list | None = None) -> int | None:
    """Find the LIVE WRAM base.

    Several regions hold a valid party block: the live WRAM and the
    game's save/backup buffers. They are indistinguishable by the party
    signature, which is why "first hit wins" locked onto a buffer and
    read a correct party forever while battle state stayed zero.

    An absolute liveness threshold does not separate them either — a
    buffer was measured churning 14 bytes against the live region's 57,
    so any fixed cut-off is either too strict (rejects the real thing
    on a quiet screen) or too loose (accepts the buffer). The only
    reliable test is COMPARING the candidates, so that is what happens
    every time: gather the copies that sit near each other and take the
    liveliest.
    """
    # Start from the cached base when there is one — the copies cluster,
    # so its neighbourhood almost always contains the live region too.
    cached = _load_cached_base() if use_cache else None
    if cached:
        log.info(f"checking around the last base {cached:#010x}…")
        hits = scan_range(rpc, max(0, cached - _NEARBY), cached + _NEARBY,
                          stop_after=8, progress_every=0)
        base = _pick_live(rpc, hits)
        if base is not None:
            _save_cached_base(base)
            if out_candidates is not None:
                out_candidates[:] = [h["wram_base"] for h in hits]
            return base
        log.info("  nothing live nearby; sweeping the heap")

    for start, end in ranges:
        log.info(f"scanning {start:#010x}-{end:#010x} for the party block…")
        # Stop at the FIRST hit, then gather its siblings from a small
        # window around it. Asking the full-range scan for 8 candidates
        # meant that whenever fewer than 8 existed it read the entire
        # ~900 MB range every time — about 85s of sustained RPC traffic
        # per attempt, which starved the detection loop and hammered
        # the emulator.
        first = scan_range(rpc, start, end, stop_after=1)
        if not first:
            continue
        anchor = first[0]["wram_base"]
        hits = scan_range(rpc, max(start, anchor - _NEIGHBOURHOOD),
                          min(end, anchor + _NEIGHBOURHOOD),
                          stop_after=8, progress_every=0)
        known = {h["wram_base"] for h in hits}
        hits += [h for h in first if h["wram_base"] not in known]
        base = _pick_live(rpc, hits)
        if base is None:
            continue
        _save_cached_base(base)
        if out_candidates is not None:
            out_candidates[:] = [h["wram_base"] for h in hits]
        return base
    return None


def _pick_live(rpc, hits) -> int | None:
    """Choose the liveliest candidate, or None when there are none.

    Several regions hold a valid party block — the live WRAM plus the
    game's save/backup buffers. Only the live one ticks, so churn is
    what tells them apart.
    """
    if not hits:
        return None
    scored = []
    for h in hits:
        c = churn(rpc, h["wram_base"])
        scored.append((c, h))
        log.info(f"  candidate {h['wram_base']:#010x}  churn={c}")

    scored.sort(key=lambda pair: pair[0], reverse=True)
    best_churn, best = scored[0]
    if best_churn <= 0:
        # Nothing here ticks, so nothing here is the live region.
        # Returning the best of a dead field is what produced a bot
        # that read the party perfectly and never saw a battle.
        log.info("  no candidate is ticking; not accepting any of them")
        return None
    base = best["wram_base"]
    log.info(f"WRAM base {base:#010x} "
             f"(party block @ {best['party_addr']:#010x}, "
             f"churn {best_churn})")
    return base


class CrystalSession:
    """Reads a located Crystal session. Re-locates if the base goes stale."""

    #: How often to re-confirm the base is LIVE, not just parseable.
    LIVE_CHECK_EVERY_S = 30.0

    def __init__(self, rpc, ranges, base: int | None = None):
        self.rpc = rpc
        self.ranges = ranges
        self.base = base
        self._next_live_check = 0.0
        #: Other party-block copies seen when locating, so the periodic
        #: check can re-compare instead of re-scanning.
        self._candidates: list = []

    def ensure_base(self) -> int | None:
        """Return a base that still holds a party AND is the liveliest.

        verify_base alone is not enough: the emulator keeps save and
        backup buffers holding a perfectly valid party block, so a base
        can verify forever while battle state reads permanently zero and
        no encounter is ever seen.

        An absolute liveness threshold does not separate them either — a
        buffer was measured churning 14 bytes against the live region's
        57. So the periodic check RE-COMPARES the copies found last
        time, which costs a couple of small reads each rather than a
        rescan, and switches if a different one is now livelier.
        """
        if self.base is not None and verify_base(self.rpc, self.base):
            now = time.monotonic()
            if now < self._next_live_check:
                return self.base
            self._next_live_check = now + self.LIVE_CHECK_EVERY_S

            if not self._candidates:
                # A session handed an explicit base has no rivals to
                # compare against yet. Find its neighbours once, or it
                # can never notice it is sitting on a save buffer.
                hits = scan_range(
                    self.rpc, max(0, self.base - _NEIGHBOURHOOD),
                    self.base + _NEIGHBOURHOOD, stop_after=8,
                    progress_every=0)
                self._candidates = [h["wram_base"] for h in hits]
            rivals = [b for b in self._candidates if b != self.base]
            if not rivals:
                return self.base
            mine = churn(self.rpc, self.base)
            best_base, best_churn = self.base, mine
            for other in rivals:
                if verify_base(self.rpc, other):
                    c = churn(self.rpc, other)
                    if c > best_churn:
                        best_base, best_churn = other, c
            if best_base != self.base:
                log.warning(
                    f"base {self.base:#010x} churns {mine} but "
                    f"{best_base:#010x} churns {best_churn} — switching to "
                    f"the livelier copy (the old one is a save buffer).")
                self.base = best_base
                _save_cached_base(best_base)
            return self.base

        self._candidates = []
        self.base = locate_wram(self.rpc, self.ranges,
                                out_candidates=self._candidates)
        self._next_live_check = time.monotonic() + self.LIVE_CHECK_EVERY_S
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
