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

from ..games import DEFAULT_OT_NAME
from ..pk6_export import ensure_targets_dir
from . import catch
from .observe import (scan_nonparty, get_party,
                       broadcast_party, _report_encounter,
                       _level_from_exp)

log = logging.getLogger(__name__)

_BTN = {"horizontal": ("DpadLeft", "DpadRight"),
        "vertical":   ("DpadUp", "DpadDown")}


def _alert(ctx, pkm, addr: int, count: int,
           will_catch: bool = False) -> None:
    bar = "*" * 30
    for line in (
        bar,
        f"  SHINY / TARGET FOUND  —  encounter #{count}",
        f"  #{pkm.species} {pkm.nickname or ''} "
        f"~Lv{_level_from_exp(pkm.exp)} {pkm.gender}  "
        f"PID={pkm.pid:08X}  nature={pkm.nature}",
        f"  IVs {pkm.ivs}  @ {addr:#010x}",
        ("  Catching it now — do not touch the controls."
         if will_catch
         else "  Bot STOPPED — battle left on screen. Catch it!"),
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


def _use_fishing_rod(ctx, foe_base: int, foe_len: int,
                     party_keys: set, baseline: set,
                     poll_timeout: float) -> None:
    """One fishing iteration: Y (cast) → poll until "!" → A (hook).

    The "!" bite cue coincides with a freshly-generated PK6 landing
    in the foe window — so polling ``scan_nonparty`` after the cast
    is functionally equivalent to detecting the visual cue. The
    moment a never-baseline key appears we tap A: the press is
    guaranteed to land inside the bite window because we just
    detected its start.

    Returns silently after ``poll_timeout`` if no bite appears — the
    main loop's next iteration will fall back here and recast.

    **Setup:** rod registered to Y (Bag → Key Items → rod → Register),
    player facing fishable water.
    """
    poll_gap = 0.2
    log.info(f"  Fishing cast: Y → poll for bite (≤{poll_timeout:.1f}s)")
    if ctx.should_stop():
        return
    ctx.input.tap("Y", hold_s=0.05)
    iterations = max(1, int(poll_timeout / poll_gap))
    for _ in range(iterations):
        if ctx.should_stop():
            return
        ctx._stop_evt.wait(poll_gap)
        cands = scan_nonparty(ctx, foe_base, foe_len, party_keys)
        if any(p.encryption_key not in baseline for _, p in cands):
            log.info("  Fishing: bite detected → A (hook)")
            ctx.input.tap("A", hold_s=0.05)
            return
    # No bite — the game shows "Not even a nibble..." (or similar).
    # Tap A once to dismiss it, then wait 1.5s for the dialog box to
    # actually close and the player to return to walkable overworld
    # state before the next iteration's Y press lands.
    log.info(f"  Fishing: no bite within {poll_timeout:.1f}s — "
             f"A to clear, recast")
    if ctx.should_stop():
        return
    ctx.input.tap("A", hold_s=0.05)
    ctx._stop_evt.wait(1.5)


def _use_sweet_scent(ctx, gap: float) -> None:
    """Open menu → Pokémon → slot 1 → Sweet Scent.

    Sequence (user-verified in X/Y, ``gap``-second intervals):

      X     — open the main menu
      A     — select "Pokémon"
      A     — select slot 1 (the Sweet Scent user)
      Down  — cursor onto the Sweet Scent field-move entry
      A     — open the field-move list
      A     — confirm Sweet Scent

    Slot 1 must hold a Sweet Scent user (Bulbasaur from Sycamore is
    the easiest in X/Y). On a horde-enabled route this triggers a
    5-mon horde 100% of the time.
    """
    seq = ["X", "A", "A", "DpadDown", "A", "A"]
    log.info(f"  Sweet Scent: {len(seq)} presses × {gap:.1f}s "
             f"({' → '.join(seq)})")
    for btn in seq:
        if ctx.should_stop():
            return
        ctx.input.tap(btn, hold_s=0.05)
        ctx._stop_evt.wait(gap)


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
    # "Got away!" text + battle fade-out + return-to-overworld can
    # take a while, especially after a horde or a fishing battle —
    # if we fire the next action too early it gets eaten by the
    # lingering battle UI. Give the transition real room.
    ctx._stop_evt.wait(2.0)                  # wait for "Got away!"
    for _ in range(3):                       # clear "Got away!" text
        ctx.input.tap("B", hold_s=0.05)
        ctx._stop_evt.wait(0.6)
    ctx._stop_evt.wait(2.0)                  # post-battle slide-back


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
        "trainer_name", DEFAULT_OT_NAME)
    walk_hold = float(rcfg.get("walk_hold", 0.35))
    flee_delay = float(rcfg.get("flee_delay", 5.0))
    run_settle = float(rcfg.get("run_settle", 1.5))
    idle_action = str(rcfg.get("idle_action", "walk")).lower()
    sweet_scent_gap = float(rcfg.get("sweet_scent_gap", 1.0))
    sweet_scent_settle = float(rcfg.get("sweet_scent_settle", 4.0))
    fish_cast_settle = float(rcfg.get("fish_cast_settle", 5.0))
    screen_layout = str(rcfg.get("screen_layout",
                                 "side_by_side")).lower()
    run_local = rcfg.get("run_local") or [0.5, 0.86]
    run_override = rcfg.get("run_touch")     # None ⇒ auto-geometry
    # What to do when the hunt finds what it was hunting for.
    on_target = str(rcfg.get("on_target", "catch")).lower()
    if on_target not in ("catch", "stop"):
        log.warning(f"  on_target={on_target!r} is not 'catch' or 'stop'; "
                    f"defaulting to catch")
        on_target = "catch"
    catch_plan = catch.CatchPlan.from_config(rcfg)
    caught = 0

    ensure_targets_dir()                    # targets/ shows up now
    log.info(f"Mode: shiny hunt — random encounters "
             f"(idle={idle_action}"
             + (f", movement={movement}, {walk_hold:.2f}s steps"
                if idle_action == "walk"
                else f", Sweet Scent gap={sweet_scent_gap:.1f}s, "
                     f"settle={sweet_scent_settle:.1f}s")
             + f", flee_delay {flee_delay:.1f}s, "
             + f"run_settle {run_settle:.1f}s)")
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

    # Named, not "a, b": these live for the whole run loop and used to
    # be clobbered by the "for a, p in ordered" encounter loop below,
    # which rebound `a` to a PK6 *address*. The next walk step then
    # passed an int as a button name and killed the run.
    walk_a, walk_b = _BTN[movement]
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
            for addr, p in ordered:
                seen.add(p.encryption_key)
                encounters += 1
                _report_encounter(ctx, p, addr, encounters, "new-key")
                if target_hit is None and _is_target(ctx, p):
                    target_hit = (addr, p)
            # "and cands" matters: rebuilding from an empty scan wipes
            # `seen` entirely, and the next poll re-reports a wild still
            # lingering in the foe buffer as a brand-new encounter.
            # observe.py has carried this guard for a while; this copy
            # did not.
            if len(seen) > 512 and cands:
                seen = {p.encryption_key for _, p in cands}
            if target_hit is not None:
                addr, p = target_hit
                will_catch = (on_target == "catch") and not dry
                _alert(ctx, p, addr, encounters, will_catch)
                if not will_catch:
                    ctx.request_stop("shiny / target found")
                    return
                result = catch.catch_wild(
                    ctx, catch_plan, p.encryption_key,
                    lambda: _refresh_party(ctx, party_base, party_stride,
                                           player_ot) or set())
                if result.caught:
                    caught += 1
                    log.info(f"  CAUGHT #{caught}: {result.detail}. "
                             f"Back to hunting.")
                    ctx.dashboard.broadcast(
                        "target_caught", species=p.species,
                        shiny=bool(p.shiny), count=encounters,
                        caught=caught)
                    catch.settle_after_battle(ctx)
                    party_keys = _refresh_party(
                        ctx, party_base, party_stride,
                        player_ot) or party_keys
                    continue
                # Unconfirmed. Stopping is the safe end: the wild may
                # still be on screen, and walking away from a shiny to
                # resume hunting is not a recoverable mistake.
                log.error(f"  CATCH FAILED: {result.detail}")
                log.error("  Bot STOPPED with the battle still open — "
                          "finish it by hand.")
                ctx.dashboard.broadcast(
                    "target_hit", count=encounters,
                    reason=f"catch failed — {result.detail}",
                    species=p.species, shiny=bool(p.shiny),
                    nature=p.nature, ivs=p.ivs)
                ctx.request_stop("catch failed")
                return
            if not dry:
                # Wait out the battle intro/animation so the command
                # menu (and the RUN button) is actually on screen.
                ctx._stop_evt.wait(flee_delay)
                _flee(ctx, screen_layout, run_local, run_override,
                      run_settle)
            continue                          # don't walk this iter

        # No new wild → overworld / stale lingering → take the
        # configured idle action: walk (random encounters) or fire a
        # Sweet Scent (horde mode — Slot 1 must be a Sweet Scent user
        # on a horde-enabled route; result is a guaranteed 5-mon horde).
        if dry:
            ctx._stop_evt.wait(0.4)
            continue
        if idle_action == "sweet_scent":
            _use_sweet_scent(ctx, sweet_scent_gap)
            # Wait out the menu close + horde intro animation so the
            # next scan_nonparty sees the 5 newly-generated wilds.
            ctx._stop_evt.wait(sweet_scent_settle)
            continue
        if idle_action == "fish":
            # Pass the live `seen` set as the baseline — the
            # fishing routine watches the foe window for any key
            # NOT in that set (i.e. the "!" just landed); when one
            # appears it taps A. The fresh PK6 stays in the foe
            # window so the main loop's next scan finds it and
            # routes to the standard "if new:" branch above.
            _use_fishing_rod(ctx, foe_base, foe_len, party_keys,
                             baseline=seen,
                             poll_timeout=fish_cast_settle)
            # 1 s breather between attempts. After a hooked bite the
            # battle intro is still cued up when we return — the wait
            # lets the rod-retract / "no nibble" animation clear
            # before the next iteration's scan or recast.
            ctx._stop_evt.wait(1.0)
            continue
        # Hold B while moving so the player RUNS (covers grass faster
        # → more encounters per minute).
        ctx.input.move_running(walk_a if step % 2 == 0 else walk_b,
                               hold_s=walk_hold)
        step += 1
        ctx._stop_evt.wait(0.12)

    log.info(f"Shiny hunt stopped after {encounters} encounter(s).")
