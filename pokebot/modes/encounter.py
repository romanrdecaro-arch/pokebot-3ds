"""
Random-encounter shiny hunt.

Key fact learned live: the foe slot KEEPS the last wild even in the
overworld (it isn't cleared on battle end / bot start). So "a wild is
present" does NOT mean "in a battle" — the previous design saw the
stale pre-bot wild, thought it was forever in battle, and flee-spun
without ever walking.

Correct model — detect by ENCRYPTION-KEY CHANGE:

  * At start, whatever wild is already in the slot is recorded as the
    baseline (added to ``handled``) so it is never reported and never
    mistaken for an active battle.
  * Walk short alternating Left/Right steps continuously.
  * A NEW encounter = the foe slot shows a wild whose encryption key
    we haven't handled yet (every generated Pokémon has a unique key).
    On a new one: report it; shiny/target → STOP + alert; otherwise
    settle, then flee (touch RUN — X/Y has no clean D-pad menu), and
    resume walking. The fled mon's key is now ``handled`` so the
    lingering stale record is ignored and we keep roaming.

Detection (``observe.find_wild``) is shared with manual mode. Tune
``random_encounters.run_touch`` / ``walk_hold`` in config live.
"""
from __future__ import annotations

import logging
import time

from .observe import (find_wild, _read_party, _report_encounter,
                       _level_from_exp)

log = logging.getLogger(__name__)

_BTN = {"horizontal": ("DpadLeft", "DpadRight"),
        "vertical":   ("DpadUp", "DpadDown")}


def _alert(ctx, pkm, addr: int, count: int) -> None:
    bar = "*" * 30
    for line in (
        bar,
        f"  SHINY / TARGET FOUND  —  encounter #{count}",
        f"  #{pkm.species} {pkm.nickname or ''} "
        f"~Lv{_level_from_exp(pkm.exp)} {pkm.gender}  "
        f"PID={pkm.pid:08X}  nature={pkm.nature}",
        f"  IVs {pkm.ivs}  @ {addr:#010x}",
        "  Bot STOPPED — battle left on screen. Catch it!",
        bar,
    ):
        log.info(line)
    ctx.dashboard.broadcast(
        "target_hit", count=count,
        reason=(ctx.target.describe(pkm) if ctx.target else "shiny"),
        species=pkm.species, shiny=pkm.shiny,
        nature=pkm.nature, ivs=pkm.ivs)


def _is_target(ctx, pkm) -> bool:
    return bool(pkm.shiny or (ctx.target and ctx.target.matches(pkm)))


def _flee(ctx, run_xy) -> None:
    """One flee attempt: clear the appearance text so the command
    menu is up, touch RUN, then clear the got-away text."""
    for _ in range(5):                       # "Wild X appeared!" / send-out
        ctx.input.tap("B", hold_s=0.05)
        ctx._stop_evt.wait(0.35)
    ok = ctx.input.tap_touch(run_xy[0], run_xy[1], hold_s=0.08)
    log.info(f"  flee: touch RUN @ ({run_xy[0]:.2f},{run_xy[1]:.2f}) "
             f"-> {'sent' if ok else 'FAILED (touch path unavailable)'}")
    ctx._stop_evt.wait(0.6)
    for _ in range(5):                       # "Got away safely!" etc.
        ctx.input.tap("B", hold_s=0.05)
        ctx._stop_evt.wait(0.3)


def run(ctx) -> None:
    o = ctx.game.offsets
    foe_base = o.foe_base
    foe_len = getattr(o, "foe_scan_len", 0) or 0x8000
    party_base = o.party_base
    party_stride = o.party_stride or 484
    rcfg = ctx.config.get("random_encounters") or {}
    player_ot = (ctx.config.get("soft_reset", {}) or {}).get(
        "trainer_name", "Roman")
    movement = str(rcfg.get("movement", "horizontal")).lower()
    if movement not in _BTN:
        movement = "horizontal"
    walk_hold = float(rcfg.get("walk_hold", 0.35))
    run_xy = rcfg.get("run_touch") or [0.5, 0.92]

    log.info(f"Mode: shiny hunt — random encounters ({movement}, "
             f"{walk_hold:.2f}s steps)")
    log.info(f"  foe window=[{foe_base:#010x},"
             f"{foe_base + foe_len:#010x})  player OT {player_ot!r}  "
             f"RUN touch ({run_xy[0]:.2f},{run_xy[1]:.2f})")
    if not foe_base:
        log.error("foe_base not configured (X/Y: 0x08800000).")
        return

    diag = ctx.input.diagnose()
    log.info(f"  input driver: {diag}")
    dry = bool(diag.get("dry_run"))
    if dry:
        log.warning("  input DRY-RUN — detect/log only (no walk/flee).")
    else:
        try:
            from ..platform_utils import focus_azahar
            focus_azahar()
        except Exception as e:
            log.warning(f"  focus_azahar failed: {e}")

    party_keys = {p.encryption_key
                  for p in _read_party(ctx, party_base, party_stride)} \
        if party_base else set()

    handled: set[int] = set()

    # Baseline: a wild left in the slot from before the bot started is
    # NOT a new encounter and must not trigger a flee. Record it.
    base = find_wild(ctx, foe_base, foe_len, party_keys, player_ot)
    if base is not None:
        handled.add(base[1].encryption_key)
        log.info(f"  baseline: stale wild #{base[1].species} "
                 f"key={base[1].encryption_key:#010x} ignored "
                 f"(pre-bot). Walking…")
    else:
        log.info("  baseline: no wild in slot. Walking…")

    a, b = _BTN[movement]
    step = 0
    encounters = 0
    last_party = time.monotonic()

    while not ctx.should_stop():
        if party_base and time.monotonic() - last_party > 30:
            party_keys = {p.encryption_key for p in
                          _read_party(ctx, party_base, party_stride)}
            last_party = time.monotonic()

        wild = find_wild(ctx, foe_base, foe_len, party_keys, player_ot)
        is_new = wild is not None and wild[1].encryption_key not in handled

        if is_new:
            addr, pkm = wild
            handled.add(pkm.encryption_key)
            if len(handled) > 256:
                handled = {pkm.encryption_key}
            encounters += 1
            _report_encounter(ctx, pkm, addr, encounters, "hunt")
            if _is_target(ctx, pkm):
                _alert(ctx, pkm, addr, encounters)
                ctx.request_stop("shiny / target found")
                return
            if not dry:
                ctx._stop_evt.wait(1.6)       # let the battle UI render
                _flee(ctx, run_xy)
            continue                          # do NOT walk this iter

        # Overworld, or the lingering stale/just-fled record → roam.
        if dry:
            ctx._stop_evt.wait(0.4)
            continue
        ctx.input.tap(a if step % 2 == 0 else b, hold_s=walk_hold)
        step += 1
        ctx._stop_evt.wait(0.12)

    log.info(f"Shiny hunt stopped after {encounters} encounter(s).")
