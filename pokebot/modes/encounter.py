"""
Random-encounter shiny hunt.

Walks back-and-forth in tall grass. On every NEW wild encounter it
reads the opponent via the proven foe-window scan (the SAME detection
manual mode uses — ``observe.find_wild`` / ``select_wild``), then:

  - shiny / target-filter match  → STOP + loud alert, battle left on
    screen for you to catch it manually.
  - anything else                → flee and resume walking.

No ``in_battle_flag`` and no offset auto-discovery needed: a wild is
simply "an empty-OT, non-party, checksum-valid PK6 in the foe
window" (X/Y WildOffset1 0x08800000). Each distinct encounter has a
unique encryption key, so a freshly-spawned wild is detected even
though the engine reuses the same RAM slot.

Movement axis: config["random_encounters"]["movement"] ("horizontal"
| "vertical", default horizontal). Active inputs need the input
driver; without it the loop still detects/logs but can't walk or
flee (it degrades to manual mode).
"""
from __future__ import annotations

import logging
import time

from .observe import (find_wild, _read_party, _report_encounter,
                       _level_from_exp)

log = logging.getLogger(__name__)


def _alternating_dirs(axis: str):
    """(button, hold_seconds) for the walking loop: a 2 s kick then
    alternating 3 s holds so the player crosses several grass tiles
    per turn (that's what rolls encounters)."""
    a, b = (("DpadUp", "DpadDown") if axis == "vertical"
            else ("DpadLeft", "DpadRight"))
    yield (a, 2.0)
    while True:
        yield (b, 3.0)
        yield (a, 3.0)


def _flee(ctx) -> None:
    """X/Y battle menu: Left→Right→A reaches RUN from the FIGHT
    cursor; then mash B to clear the got-away dialogue."""
    log.info("  not a target — fleeing.")
    for button in ("DpadLeft", "DpadRight", "A"):
        ctx.input.tap(button, hold_s=0.05)
        time.sleep(0.5)
    for _ in range(6):
        ctx.input.tap("B", hold_s=0.05)
        time.sleep(0.25)


def _alert_shiny(ctx, pkm, addr: int, count: int) -> None:
    lvl = _level_from_exp(pkm.exp)
    bar = "★" * 28
    log.info(bar)
    log.info(f"  SHINY / TARGET FOUND  —  encounter #{count}")
    log.info(f"  #{pkm.species} {pkm.nickname or ''} ~Lv{lvl} "
             f"{pkm.gender}  PID={pkm.pid:08X}  nature={pkm.nature}")
    log.info(f"  IVs {pkm.ivs}  @ {addr:#010x}")
    log.info("  Bot STOPPED — battle left on screen. Catch it!")
    log.info(bar)
    ctx.dashboard.broadcast(
        "target_hit", count=count,
        reason=(ctx.target.describe(pkm) if ctx.target else "shiny"),
        species=pkm.species, shiny=pkm.shiny,
        nature=pkm.nature, ivs=pkm.ivs)


def _is_target(ctx, pkm) -> bool:
    if pkm.shiny:
        return True
    return bool(ctx.target and ctx.target.matches(pkm))


def run(ctx) -> None:
    o = ctx.game.offsets
    foe_base = o.foe_base
    foe_len = getattr(o, "foe_scan_len", 0) or 0x20000
    party_base = o.party_base
    party_stride = o.party_stride or 484
    player_ot = (ctx.config.get("soft_reset", {}) or {}).get(
        "trainer_name", "Roman")
    movement = (ctx.config.get("random_encounters") or {}).get(
        "movement", "horizontal").lower()
    if movement not in ("horizontal", "vertical"):
        movement = "horizontal"

    log.info(f"Mode: shiny hunt — random encounters ({movement})")
    log.info(f"  foe window=[{foe_base:#010x},"
             f"{foe_base + foe_len:#010x})  player OT {player_ot!r}")
    if not foe_base:
        log.error("foe_base not configured (X/Y: 0x08800000). Set it "
                  "in config.yaml [offsets:].")
        return

    diag = ctx.input.diagnose()
    log.info(f"  input driver: {diag}")
    dry = bool(diag.get("dry_run"))
    if dry:
        log.warning("  input driver DRY-RUN — cannot walk/flee; this "
                    "will only detect & log (like manual mode).")
    else:
        try:
            from ..platform_utils import focus_azahar
            focus_azahar()
        except Exception as e:
            log.warning(f"  focus_azahar failed: {e}")

    # Party keys exclude the player's own mons from the wild scan.
    party_keys = {p.encryption_key
                  for p in _read_party(ctx, party_base, party_stride)} \
        if party_base else set()

    handled: set[int] = set()       # enc_keys already evaluated
    walk = _alternating_dirs(movement)
    encounters = 0
    last_party_refresh = time.monotonic()

    while not ctx.should_stop():
        # Refresh party occasionally (cheap insurance if it changes).
        if party_base and time.monotonic() - last_party_refresh > 30:
            party_keys = {p.encryption_key for p in
                          _read_party(ctx, party_base, party_stride)}
            last_party_refresh = time.monotonic()

        wild = find_wild(ctx, foe_base, foe_len, party_keys, player_ot)

        if wild is None:
            # Overworld — take a walking step (skip if no input driver).
            if dry:
                ctx._stop_evt.wait(0.8)
            else:
                button, hold_s = next(walk)
                ctx.input.tap(button, hold_s=hold_s)
                ctx._stop_evt.wait(0.15)
            continue

        addr, pkm = wild
        if pkm.encryption_key in handled:
            # Same battle we already dealt with (mid-flee / lingering
            # stale record). Nudge forward to clear dialogue and roll
            # the next encounter; don't re-evaluate it.
            if not dry:
                button, _ = next(walk)
                ctx.input.tap(button, hold_s=0.1)
            ctx._stop_evt.wait(0.25)
            continue

        handled.add(pkm.encryption_key)
        if len(handled) > 256:
            handled = {pkm.encryption_key}
        encounters += 1
        _report_encounter(ctx, pkm, addr, encounters, "hunt")

        if _is_target(ctx, pkm):
            _alert_shiny(ctx, pkm, addr, encounters)
            ctx.request_stop("shiny / target found")
            return

        if dry:
            continue                # can't flee without inputs

        # Let the battle intro animation finish and the FIGHT/BAG/
        # POKéMON/RUN menu render before mashing the flee inputs.
        ctx._stop_evt.wait(2.0)
        _flee(ctx)

    log.info(f"Shiny hunt stopped after {encounters} encounter(s).")
