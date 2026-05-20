"""
Random-encounter shiny hunt.

Detection model (shared with manual mode, see observe.scan_nonparty):
the foe slot keeps the last wild even in the overworld, and a stale
wild can linger at a low address masking the new one — so we DON'T
key off address/OT. Every generated Pokémon has a unique encryption
key; the player's battle copy keeps its fixed key, a fresh wild
ALWAYS brings a brand-new key. So:

  * Baseline at start: every non-party PK6 already in the window is
    recorded → never reported, never mistaken for a battle (kills the
    "detects whatever was encountered before the bot started" and the
    flee-spin-at-launch bugs).
  * Walk short alternating Left/Right steps continuously.
  * NEW encounter = a non-party key not seen before appears anywhere
    in the window (robust no matter which slot the engine used).
    Report it; shiny/target → STOP + alert; otherwise wait
    ``flee_delay`` s for the battle intro to finish, then flee
    (touch RUN — X/Y has no clean D-pad menu), and resume walking.

Tune ``random_encounters`` in config live: movement, walk_hold,
flee_delay, run_touch.
"""
from __future__ import annotations

import logging

from .observe import (scan_nonparty, pick_opponent, get_party,
                       broadcast_party, _report_encounter,
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


def _refresh_party(ctx, party_base, party_stride, player_ot):
    """Read the live party, push it to the launcher's (always-visible)
    party strip, and return the set of party encryption keys (used to
    exclude the player's own mons from wild detection). Returns an
    empty set when party_base isn't configured."""
    party = get_party(ctx, party_base, party_stride, player_ot)
    return broadcast_party(ctx, party)   # broadcasts only on change


def _run_fraction(layout, run_local, override):
    """Window fractions to touch for RUN. Explicit ``override`` wins;
    otherwise compute from Azahar's live client size + screen layout
    so it's correct at ANY window size."""
    if override:
        return float(override[0]), float(override[1]), "override"
    try:
        from ..platform_utils import (find_azahar_hwnd, get_client_size,
                                       bottom_screen_fraction)
        hwnd = find_azahar_hwnd()
        wh = get_client_size(hwnd) if hwnd else None
        if wh:
            fx, fy = bottom_screen_fraction(
                wh[0], wh[1], layout, run_local[0], run_local[1])
            return fx, fy, f"{layout} {wh[0]}x{wh[1]}"
    except Exception as e:
        log.warning(f"  run-position geometry failed: {e}")
    return 0.5, 0.92, "fallback"


def _flee(ctx, layout, run_local, override, run_settle: float) -> None:
    """Clear the appearance text so the command menu is up, wait for
    it to render, touch RUN, then clear the got-away text."""
    for _ in range(4):
        ctx.input.tap("B", hold_s=0.05)
        ctx._stop_evt.wait(0.35)
    ctx._stop_evt.wait(run_settle)            # let the menu draw
    fx, fy, how = _run_fraction(layout, run_local, override)
    ok = ctx.input.tap_touch(fx, fy, hold_s=0.08)
    log.info(f"  flee: touch RUN @ ({fx:.3f},{fy:.3f}) [{how}] "
             f"after {run_settle:.1f}s settle "
             f"-> {'sent' if ok else 'FAILED (touch unavailable)'}")
    ctx._stop_evt.wait(0.3)
    for _ in range(3):                       # clear "Got away!" fast
        ctx.input.tap("B", hold_s=0.05)
        ctx._stop_evt.wait(0.12)


def run(ctx) -> None:
    o = ctx.game.offsets
    foe_base = o.foe_base
    foe_len = getattr(o, "foe_scan_len", 0) or 0x20000
    party_base = o.party_base
    party_stride = o.party_stride or 484
    rcfg = ctx.config.get("random_encounters") or {}
    movement = str(rcfg.get("movement", "horizontal")).lower()
    if movement not in _BTN:
        movement = "horizontal"
    player_ot = (ctx.config.get("soft_reset", {}) or {}).get(
        "trainer_name", "Roman")
    walk_hold = float(rcfg.get("walk_hold", 0.35))
    flee_delay = float(rcfg.get("flee_delay", 5.0))
    run_settle = float(rcfg.get("run_settle", 1.5))
    screen_layout = str(rcfg.get("screen_layout",
                                 "side_by_side")).lower()
    run_local = rcfg.get("run_local") or [0.5, 0.86]
    run_override = rcfg.get("run_touch")     # None ⇒ auto-geometry

    log.info(f"Mode: shiny hunt — random encounters ({movement}, "
             f"{walk_hold:.2f}s steps, flee_delay {flee_delay:.1f}s, "
             f"run_settle {run_settle:.1f}s)")
    log.info(f"  foe window=[{foe_base:#010x},"
             f"{foe_base + foe_len:#010x})  layout={screen_layout} "
             f"run_local={run_local}"
             + (f" run_touch override={run_override}"
                if run_override else " (RUN auto-positioned)"))
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

    # Read + show the party up front so the strip is populated the
    # moment the hunt starts.
    party_keys = _refresh_party(ctx, party_base, party_stride, player_ot)

    # Baseline: pre-existing non-party PK6 (stale pre-bot wild +
    # player's battle copy) are NOT new encounters.
    seen: set[int] = {p.encryption_key for _, p in
                      scan_nonparty(ctx, foe_base, foe_len, party_keys)}
    log.info(f"  baseline: {len(seen)} pre-existing non-party PK6 "
             f"ignored. Walking…")

    a, b = _BTN[movement]
    step = 0
    encounters = 0

    while not ctx.should_stop():
        # Re-read the party every loop (cheap once the window is
        # cached); broadcast only when it changes, so the strip
        # updates the moment a battle ends / a catch happens.
        party_keys = _refresh_party(ctx, party_base, party_stride,
                                    player_ot) or party_keys

        cands = scan_nonparty(ctx, foe_base, foe_len, party_keys)
        new = [(addr, p) for addr, p in cands
               if p.encryption_key not in seen]

        if new:
            # Horde-aware: a horde battle drops 5 unseen non-party
            # PK6 into the foe window at once. Report each (so
            # Recently Seen gets 5 rows / 5x the data) and stop on
            # the FIRST target match anywhere in the horde. Fleeing
            # ends the whole battle in one RUN press either way.
            ordered = sorted(new, key=lambda ap: ap[0])
            n = len(ordered)
            log.info(f"  encounter: {n} new wild "
                     f"{'(horde)' if n > 1 else '(single)'}")
            target_hit = None
            for a, p in ordered:
                seen.add(p.encryption_key)
                encounters += 1
                _report_encounter(ctx, p, a, encounters, "new-key")
                if target_hit is None and _is_target(ctx, p):
                    target_hit = (a, p)
            if len(seen) > 512:
                seen = {p.encryption_key for _, p in cands}
            if target_hit is not None:
                a, p = target_hit
                _alert(ctx, p, a, encounters)
                ctx.request_stop("shiny / target found")
                return
            if not dry:
                # Wait out the battle intro/animation so the command
                # menu (and the RUN button) is actually on screen.
                ctx._stop_evt.wait(flee_delay)
                _flee(ctx, screen_layout, run_local, run_override,
                      run_settle)
            continue                          # don't walk this iter

        # No new wild → overworld / stale lingering → roam.
        if dry:
            ctx._stop_evt.wait(0.4)
            continue
        # Hold B while moving so the player RUNS (covers grass faster
        # → more encounters per minute).
        ctx.input.move_running(a if step % 2 == 0 else b,
                               hold_s=walk_hold)
        step += 1
        ctx._stop_evt.wait(0.12)

    log.info(f"Shiny hunt stopped after {encounters} encounter(s).")
