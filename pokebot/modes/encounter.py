"""
Encounter mode (random encounters).

Walks back-and-forth on a chosen axis (horizontal or vertical), watches
for the in-battle flag to flip, reads the foe Pokémon, broadcasts the
full encounter record, then flees (unless it's a target hit) and
resumes walking.

The movement axis comes from config["random_encounters"]["movement"]
(or the --movement CLI flag) and defaults to "horizontal":

  horizontal  → DpadLeft / DpadRight
  vertical    → DpadUp   / DpadDown

Active inputs require the input_driver (pynput); without it the mode
will run in dry-run and only ever observe (won't actually walk).

Required offsets:
  - foe_base
  - in_battle_flag
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from ..parser import decrypt_pkm, parse_pkm

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent


def run(ctx):
    movement = (ctx.config.get("random_encounters") or {}).get(
        "movement", "horizontal").lower()
    if movement not in ("horizontal", "vertical"):
        log.warning(f"Unknown movement {movement!r}; defaulting to horizontal.")
        movement = "horizontal"
    log.info(f"Mode: encounter ({movement})")

    # Auto-discover foe_base on first run (X/Y / OR/AS / SM/USUM all need
    # a per-version address that PKHeX-Plugins doesn't ship). Caches to
    # config.yaml so subsequent runs skip this entirely.
    if not ctx.game.offsets.foe_base:
        if not _autodiscover_foe_base(ctx, movement):
            log.error("foe_base auto-discovery did not complete. "
                      "Stop, walk somewhere with tall grass, and try again.")
            return

    if not ctx.game.offsets.in_battle_flag:
        log.warning("in_battle_flag is 0 -- falling back to "
                    "polling foe block for changes (less reliable).")

    encounters = 0
    walking = True
    last_foe_key = None

    # primitive walking loop — alternates the two dpad keys for the
    # chosen axis while not in battle.
    walk_dir_iter = _alternating_dirs(movement)

    while not ctx.should_stop():
        in_battle = _check_in_battle(ctx)

        if not in_battle:
            if walking:
                d = next(walk_dir_iter)
                ctx.input.tap(d, hold_s=0.30)
            time.sleep(0.05)
            continue

        # In battle: stop walking, read the foe.
        try:
            raw = ctx.rpc.read(ctx.game.offsets.foe_base, 260)
        except Exception as e:
            log.warning(f"foe read failed: {e}")
            time.sleep(0.5)
            continue
        if int.from_bytes(raw[:4], "little") == 0:
            # The flag flipped but the foe block isn't populated yet.
            time.sleep(0.1)
            continue
        try:
            pkm = parse_pkm(decrypt_pkm(raw))
        except Exception as e:
            log.debug(f"foe parse failed: {e}")
            time.sleep(0.2)
            continue
        if not pkm.checksum_valid:
            time.sleep(0.1)
            continue

        key = (pkm.encryption_key, pkm.pid)
        if key == last_foe_key:
            # Same encounter we already evaluated; wait for it to end.
            time.sleep(0.2)
            continue
        last_foe_key = key
        encounters += 1
        ctx.dashboard.broadcast(
            "encounter",
            count=encounters,
            species=pkm.species, nickname=pkm.nickname,
            shiny=pkm.shiny, nature=pkm.nature, gender=pkm.gender,
            ivs=pkm.ivs, pid=pkm.pid,
            tsv=pkm.tsv, psv=pkm.psv,
            ability_id=pkm.ability_id, ability_num=pkm.ability_num,
            level=pkm.party["level"] if pkm.party else None,
            moves=pkm.moves,
        )

        if ctx.target and ctx.target.matches(pkm):
            log.info(f"TARGET! enc#{encounters}: {ctx.target.describe(pkm)}")
            ctx.dashboard.broadcast(
                "target_hit",
                count=encounters,
                reason=ctx.target.describe(pkm),
                species=pkm.species, shiny=pkm.shiny,
                nature=pkm.nature, ivs=pkm.ivs,
            )
            walking = False
            ctx.request_stop("target hit")
            return

        # Not a hit: flee. Press B+Down repeatedly to navigate the run menu.
        # Real menu navigation will need refinement per game; this is a
        # generic best-effort.
        for _ in range(5):
            ctx.input.tap("B", 0.05)
            time.sleep(0.1)
        # wait for battle flag to clear
        for _ in range(100):
            if not _check_in_battle(ctx):
                break
            time.sleep(0.1)


def _check_in_battle(ctx) -> bool:
    addr = ctx.game.offsets.in_battle_flag
    if not addr:
        # fallback: presence of a non-zero foe encryption key
        try:
            return ctx.rpc.read_u32(ctx.game.offsets.foe_base) != 0
        except Exception:
            return False
    try:
        # read a u8 by default; some games use u32 -- adjust per-game later
        return ctx.rpc.read_u8(addr) != 0
    except Exception:
        return False


def _alternating_dirs(axis: str):
    if axis == "vertical":
        a, b = "DpadUp", "DpadDown"
    else:
        a, b = "DpadLeft", "DpadRight"
    while True:
        yield a
        yield b


# ---------------------------------------------------------------------------
# foe_base auto-discovery
# ---------------------------------------------------------------------------

def _autodiscover_foe_base(ctx, movement: str) -> bool:
    """Walk + scan loop that resolves foe_base from a live battle.

    Step 1: full heap scan to catalogue every valid PK6 record currently
            in RAM (party + boxes). This is the baseline.
    Step 2: walk on the chosen axis, periodically re-scan. The first
            address that wasn't in the baseline is the foe slot.
    Step 3: persist to config.yaml so the next run skips all this.

    The first run is slow (typically 30-90 s on a 64 MB Gen 6 heap),
    but the result is cached.
    """
    from ..find_offsets import scan
    from ..games import heap_range_for

    range_ = heap_range_for(ctx.game.generation)
    log.info(f"foe_base = 0 — auto-discovering. Heap range "
             f"{range_[0]:#x}–{range_[1]:#x}.")
    ctx.dashboard.broadcast("offset_scan", state="started",
                            target="foe_base")

    # ── Baseline ──────────────────────────────────────────────────────
    log.info("Step 1/2: cataloguing baseline PK6 records "
             "(walk somewhere with grass nearby first if you haven't)…")
    t0 = time.monotonic()
    baseline: set[int] = set()
    try:
        for addr, _info in scan(ctx.rpc, range_[0], range_[1],
                                chunk=0x4000, throttle_s=0.005):
            baseline.add(addr)
            if ctx.should_stop():
                return False
    except Exception as e:
        log.error(f"baseline scan failed: {e}")
        return False
    log.info(f"  baseline: {len(baseline)} PK6 records "
             f"({time.monotonic() - t0:.1f}s)")

    # ── Walk + re-scan ───────────────────────────────────────────────
    log.info("Step 2/2: walking until a wild battle starts. The first "
             "new PK6 in RAM is the foe slot.")
    walk_iter = _alternating_dirs(movement)
    taps_per_check = 8        # ~5 s of walking between scans
    tap_count = 0
    rescan_n = 0
    while not ctx.should_stop():
        ctx.input.tap(next(walk_iter), hold_s=0.30)
        time.sleep(0.4)
        tap_count += 1
        if tap_count < taps_per_check:
            continue
        tap_count = 0
        rescan_n += 1
        log.info(f"  rescan #{rescan_n}…")
        new_addrs: set[int] = set()
        try:
            for addr, _info in scan(ctx.rpc, range_[0], range_[1],
                                    chunk=0x4000, throttle_s=0.005):
                if addr not in baseline:
                    new_addrs.add(addr)
                if ctx.should_stop():
                    return False
        except Exception as e:
            log.warning(f"  rescan failed: {e}")
            continue
        if not new_addrs:
            log.info("  no new PK6 yet; keep walking.")
            continue
        # The lowest fresh address is most likely the foe slot itself —
        # higher addresses tend to be battle-engine work copies that get
        # populated downstream.
        foe_addr = min(new_addrs)
        log.info(f"  foe_base = {foe_addr:#010x} "
                 f"(picked from {len(new_addrs)} new PK6 record(s))")
        ctx.game.offsets.foe_base = foe_addr
        ctx.dashboard.broadcast("offset_scan", state="ok",
                                foe_base=foe_addr)
        _persist_foe_base(foe_addr)
        return True
    return False


def _persist_foe_base(addr: int) -> None:
    """Write foe_base into config.yaml's offsets: block (line-aware).

    Preserves comments / formatting; only rewrites the foe_base line.
    """
    cfg = ROOT / "config.yaml"
    if not cfg.exists():
        return
    try:
        text = cfg.read_text(encoding="utf-8")
    except Exception as e:
        log.warning(f"could not read {cfg}: {e}")
        return
    new_line = f"  foe_base: {addr:#010x}"
    out, n = re.subn(r"^( *)foe_base:\s*[^\n]+", new_line, text,
                     count=1, flags=re.MULTILINE)
    if n == 0:
        log.warning(f"foe_base line not found in {cfg.name}; "
                    f"address {addr:#010x} kept in memory only.")
        return
    try:
        cfg.write_text(out, encoding="utf-8")
        log.info(f"  saved foe_base = {addr:#010x} to {cfg.name}")
    except Exception as e:
        log.warning(f"could not write {cfg}: {e}")
