"""
Observe mode — manual control with live encounter detection.

The bot sends NO inputs; the user plays the game normally. The bot
watches RAM for valid PK6/PK7 records and reports any *new* Pokémon
it sees (wild battle foes, gifts, hatched eggs, caught Pokémon
landing in the party).

Strategy: an adaptive scanner.

  Phase 1 — Baseline:
    One full-heap pass to catalogue every valid PK6 currently in RAM
    (the player's party + any box slots). Their encryption keys go
    into ``seen_keys`` so we don't re-announce them.

  Phase 2 — Hot-poll + periodic rescan:
    Tight loop reads each ``known_addr`` (every PK6 the baseline
    found) every 0.6 s. If a hot-poll reads an unfamiliar enc_key,
    that address is now hosting a different Pokémon (a fresh wild
    foe, the next box slot user clicked into, etc.) — report it
    immediately. Hot-poll is instant.

    Periodically (every ~30 s of wallclock) trigger a full-heap
    rescan in a background thread to find Pokémon that landed at
    new addresses. Hot-poll keeps running while the rescan goes,
    so wild encounters happening DURING the rescan still get
    caught the moment their address joins ``known_addrs``.

No offsets required. Works in dry-run, never sends keys.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from ..parser import decrypt_pkm, parse_pkm

log = logging.getLogger(__name__)


_HOT_POLL_INTERVAL_S    = 0.6     # how often to re-read each known addr
_FULL_RESCAN_INTERVAL_S = 30.0    # min seconds between full-heap rescans


def run(ctx) -> None:
    log.info("Mode: observe (manual control + live detection)")

    seen_keys: set[int] = set()      # enc_keys we've already announced
    known_addrs: set[int] = set()    # addresses that hold a valid PK6
    addr_lock = threading.Lock()
    last_full_scan = [0.0]
    rescan_in_flight = [False]

    # ── Baseline ────────────────────────────────────────────────────────
    log.info("Baseline scan — cataloguing existing party + box Pokémon "
             "(can take 30-90 s).")
    initial = _full_scan(ctx)
    if initial is None:
        log.error("Baseline scan failed; observe can't continue without "
                  "a starting picture. Stop.")
        return
    for addr, key in initial:
        seen_keys.add(key)
        known_addrs.add(addr)
    log.info(f"Baseline: {len(known_addrs)} Pokémon catalogued. "
             f"Watching for new encounters now.")
    last_full_scan[0] = time.monotonic()

    # ── Watch loop ──────────────────────────────────────────────────────
    while not ctx.should_stop():
        # Hot-poll every known address. Cheap (one 260-byte RPC read each).
        # If the data changed (different enc_key), we caught a new
        # Pokémon at an existing slot — typical for the foe slot during
        # a wild battle, or a box slot the user clicked into.
        with addr_lock:
            addrs = list(known_addrs)
        for addr in addrs:
            if ctx.should_stop():
                return
            try:
                raw = ctx.rpc.read(addr, 260)
            except Exception:
                continue
            enc_key = int.from_bytes(raw[:4], "little")
            if enc_key == 0 or enc_key in seen_keys:
                continue
            # Fresh enc_key at a known address. Decrypt and report.
            try:
                pkm = parse_pkm(decrypt_pkm(raw))
            except Exception:
                continue
            if not pkm.checksum_valid:
                continue
            seen_keys.add(enc_key)
            _report(ctx, pkm, addr, source="hot-poll")

        # Trigger a background full-heap rescan if it's time and one
        # isn't already running.
        now = time.monotonic()
        if (now - last_full_scan[0] > _FULL_RESCAN_INTERVAL_S
                and not rescan_in_flight[0]):
            rescan_in_flight[0] = True
            last_full_scan[0] = now
            threading.Thread(
                target=_background_rescan,
                args=(ctx, seen_keys, known_addrs, addr_lock,
                      rescan_in_flight),
                name="ObserveRescan", daemon=True
            ).start()

        time.sleep(_HOT_POLL_INTERVAL_S)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _full_scan(ctx) -> Optional[list[tuple[int, int]]]:
    """One full-heap pass. Returns [(addr, enc_key), ...] or None on error.

    Walks every heap range in priority order (hot save-block region
    first, then wider fallbacks) looking for Pokemon Accessor structs.
    Stops early as soon as a range yields hits — the ranges that
    follow are fallbacks for when the priority range is empty.
    """
    from ..find_offsets import scan_accessors
    from ..games import scan_ranges_for
    out: list[tuple[int, int]] = []
    seen: set[int] = set()
    for r_lo, r_hi in scan_ranges_for(ctx.game.generation):
        log.info(f"  scan range {r_lo:#x}-{r_hi:#x}…")
        before = len(out)
        try:
            for data_addr, info in scan_accessors(ctx.rpc, r_lo, r_hi,
                                                  chunk=0x4000, throttle_s=0.005):
                if ctx.should_stop():
                    return out
                k = info["enc_key"]
                if k in seen:
                    continue
                seen.add(k)
                out.append((data_addr, k))
        except Exception as e:
            log.warning(f"  range {r_lo:#x} failed: {e}")
            continue
        new_in_range = len(out) - before
        log.info(f"  range {r_lo:#x}-{r_hi:#x}: +{new_in_range} accessor(s)")
        if new_in_range > 0:
            # Found data here; subsequent ranges are fallbacks. Stop.
            break
    return out if out else None


def _background_rescan(ctx, seen_keys: set[int], known_addrs: set[int],
                       lock: threading.Lock, flag: list) -> None:
    """Run a full-heap pass off the hot-poll thread, merge results."""
    try:
        results = _full_scan(ctx)
        if results is None:
            return
        new_addr_count = 0
        new_pkm_count = 0
        for addr, enc_key in results:
            if enc_key in seen_keys:
                # Already announced — but the address might be new
                # (Pokémon got moved). Track it for hot-polling.
                with lock:
                    if addr not in known_addrs:
                        known_addrs.add(addr)
                        new_addr_count += 1
                continue
            # New enc_key — read the full record and report it.
            try:
                raw = ctx.rpc.read(addr, 260)
                pkm = parse_pkm(decrypt_pkm(raw))
            except Exception:
                continue
            if not pkm.checksum_valid:
                continue
            seen_keys.add(enc_key)
            with lock:
                known_addrs.add(addr)
            new_pkm_count += 1
            _report(ctx, pkm, addr, source="rescan")
        if new_pkm_count or new_addr_count:
            log.info(f"  rescan: {new_pkm_count} new Pokémon, "
                     f"{new_addr_count} relocated address(es).")
    finally:
        flag[0] = False


def _report(ctx, pkm, addr: int, source: str) -> None:
    """Broadcast one encounter event for a freshly-seen Pokémon."""
    log.info(f"NEW Pokémon @ {addr:#010x} ({source}): "
             f"#{pkm.species} {pkm.nickname or ''} "
             f"{'★ SHINY ' if pkm.shiny else ''}"
             f"PID={pkm.pid:08X}")
    from ..parser import encounter_payload
    ctx.dashboard.broadcast(
        "encounter",
        source=source, address=f"{addr:#010x}",
        **encounter_payload(pkm))
    if ctx.target and ctx.target.matches(pkm):
        ctx.dashboard.broadcast(
            "target_hit",
            reason=ctx.target.describe(pkm),
            species=pkm.species, shiny=pkm.shiny,
            nature=pkm.nature, ivs=pkm.ivs,
        )
        log.info(f"TARGET HIT: {ctx.target.describe(pkm)}")
