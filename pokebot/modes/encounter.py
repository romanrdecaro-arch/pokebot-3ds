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
import time
from dataclasses import dataclass

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


def _export_caught(ctx, pkm, party_base, party_stride, player_ot,
                   precapture) -> None:
    """Save the legal copy of a target once the game has owned it.

    ``contiguous=False``: a just-caught mon can land in the live battle
    buffer at an address off the save-block grid, and the contiguity
    filter used for the party STRIP would hide it here.
    """
    from ..pk6_export import save_caught_pk6
    from .box_lookup import find_in_boxes
    try:
        party = get_party(ctx, party_base, party_stride, player_ot,
                          contiguous=False)
        records = list(party or ())

        # A catch made with a full party never reaches the party at
        # all -- it goes straight to a PC box. Look there before
        # settling for the pre-capture record.
        if not any(p.pid == pkm.pid and p.species == pkm.species
                   for p in records):
            anchor = min((getattr(p, "source_address", 0) or 0
                          for p in records), default=0)
            if not anchor:
                win = getattr(ctx, "_party_win", None)
                anchor = win[0] if win else 0
            boxed = find_in_boxes(ctx, pkm.pid, pkm.species, player_ot,
                                  anchor)
            if boxed is not None:
                records.append(boxed)

        save_caught_pk6(ctx, pkm, records, "shiny" if pkm.shiny else "wild",
                        supersedes=precapture)
    except Exception as exc:
        # Never let an export problem end a hunt -- the pre-capture
        # file is still on disk and holds every stat.
        log.warning(f"  .pk6 re-export after the catch failed: {exc}")


@dataclass(frozen=True)
class FleePlan:
    """How long to spend getting out of a battle.

    The old numbers were tuned against a 100% emulator and cost 12.7 s
    an encounter. Azahar runs this hunt around 600%, where every one of
    those animations is roughly six times shorter, so most of that was
    the bot waiting for something that had already finished.

    Cutting them risks the RUN touch landing before the menu is drawn,
    which strands the bot in a battle it cannot see -- so this is only
    safe paired with ``stuck_timeout`` below, which notices the stall
    and throws the sequence again.
    """
    delay: float = 1.5           # battle intro, before anything is pressed
    intro_taps: int = 4          # B, to clear "a wild X appeared!"
    intro_gap: float = 0.15
    run_settle: float = 0.3      # command menu draw, before touching RUN
    got_away: float = 0.8        # "Got away!" text
    clear_taps: int = 3          # B, to clear it
    clear_gap: float = 0.25
    tail: float = 0.8            # battle fade + overworld slide-back
    # No new encounter for this long means something is wrong -- almost
    # always a RUN touch that missed, leaving the bot walking into a
    # battle menu forever. Re-run the flee instead of hunting nothing.
    stuck_timeout: float = 60.0

    @classmethod
    def from_config(cls, rcfg: dict | None) -> "FleePlan":
        rcfg = rcfg or {}
        d = cls()

        def num(key: str, default: float) -> float:
            try:
                return float(rcfg.get(key, default))
            except (TypeError, ValueError):
                log.warning(f"  {key}={rcfg.get(key)!r} is not a number; "
                            f"using {default}")
                return default

        return cls(
            delay=num("flee_delay", d.delay),
            intro_taps=max(0, int(num("flee_intro_taps", d.intro_taps))),
            intro_gap=num("flee_intro_gap", d.intro_gap),
            run_settle=num("run_settle", d.run_settle),
            got_away=num("flee_got_away", d.got_away),
            clear_taps=max(0, int(num("flee_clear_taps", d.clear_taps))),
            clear_gap=num("flee_clear_gap", d.clear_gap),
            tail=num("flee_tail", d.tail),
            stuck_timeout=max(0.0, num("stuck_timeout", d.stuck_timeout)),
        )

    @property
    def total(self) -> float:
        """Scripted seconds per flee, for the startup log line."""
        return (self.delay + self.intro_taps * self.intro_gap
                + self.run_settle + self.got_away
                + self.clear_taps * self.clear_gap + self.tail)


class Walker:
    """Alternates the two movement buttons.

    Every second a wild battle is on screen is a second the player is
    not in the grass, so the flee sequence spends its waits walking
    instead of sleeping: by the time the battle fades out the player is
    already moving and the next encounter is already being rolled.

    Direction presses in a battle only slide the command-menu cursor,
    which is harmless here because RUN is reached by touch, not by the
    cursor. Movement stops the moment a target is found -- from then on
    nothing should be moving the cursor under the catch sequence.
    """

    def __init__(self, ctx, buttons, hold_s: float, gap: float):
        self.ctx = ctx
        self.buttons = buttons
        self.hold_s = max(0.01, float(hold_s))
        self.gap = max(0.0, float(gap))
        self.step = 0

    def one_step(self) -> None:
        """One direction press, alternating each call."""
        # B is held for the press, so the player runs -- and in a
        # battle that same B press clears a text box.
        self.ctx.input.move_running(self.buttons[self.step % 2],
                                    hold_s=self.hold_s)
        self.step += 1
        if self.gap:
            self.ctx._stop_evt.wait(self.gap)

    def wait(self, seconds: float) -> None:
        """Spend ``seconds`` walking rather than standing still."""
        if seconds <= 0:
            return
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.ctx.should_stop():
                return
            self.one_step()

    def idle(self, seconds: float) -> None:
        """A plain wait, for when moving would be wrong."""
        self.ctx._stop_evt.wait(seconds)


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


def _flee(ctx, layout, run_local, override, plan: FleePlan,
          walker: "Walker | None" = None) -> None:
    """Clear the appearance text so the command menu is up, wait for
    it to render, touch RUN, then clear the got-away text.

    Given a ``walker``, every wait here is spent moving instead of
    standing still. move_running holds B for each step, so the walking
    also does the text-clearing these taps used to do on their own.
    """
    def pause(seconds: float) -> None:
        if walker is not None:
            walker.wait(seconds)
        else:
            ctx._stop_evt.wait(seconds)

    for _ in range(plan.intro_taps):
        ctx.input.tap("B", hold_s=0.05)
        pause(plan.intro_gap)
    pause(plan.run_settle)                    # let the menu draw
    fx, fy, how = _run_fraction(layout, run_local, override)
    ok = ctx.input.tap_touch(fx, fy, hold_s=0.08)
    log.info(f"  flee: touch RUN @ ({fx:.3f},{fy:.3f}) [{how}] "
             f"after {plan.run_settle:.2f}s settle "
             f"-> {'sent' if ok else 'FAILED (touch unavailable)'}")
    # "Got away!" text + battle fade-out + return-to-overworld. Short
    # because the emulator is running well above 100% — a stall here
    # is caught by the stuck-timeout watchdog rather than by padding
    # every single encounter against the worst case.
    pause(plan.got_away)
    for _ in range(plan.clear_taps):          # clear "Got away!" text
        ctx.input.tap("B", hold_s=0.05)
        pause(plan.clear_gap)
    pause(plan.tail)                          # post-battle slide-back


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
    walk_hold = float(rcfg.get("walk_hold", 0.10))
    walk_gap = float(rcfg.get("walk_gap", 0.05))
    flee_plan = FleePlan.from_config(rcfg)
    idle_action = str(rcfg.get("idle_action", "walk")).lower()
    sweet_scent_gap = float(rcfg.get("sweet_scent_gap", 1.0))
    sweet_scent_settle = float(rcfg.get("sweet_scent_settle", 4.0))
    fish_cast_settle = float(rcfg.get("fish_cast_settle", 5.0))
    screen_layout = str(rcfg.get("screen_layout",
                                 "side_by_side")).lower()
    run_local = rcfg.get("run_local") or [0.5, 0.86]
    run_override = rcfg.get("run_touch")     # None ⇒ auto-geometry
    # What to do when the hunt finds what it was hunting for.
    # What to do when a throw cannot be confirmed from the party.
    #
    # "resume" by default: an unconfirmed catch is usually a catch that
    # worked and went somewhere the party scan cannot see — a PC box,
    # or under a key that read back differently. Halting the hunt on
    # that is the more common mistake, and the .pk6 is exported before
    # the throw either way, so nothing is lost by carrying on. "stop"
    # leaves the battle on screen instead.
    on_catch_fail = str(rcfg.get("on_catch_fail", "resume")).lower()
    if on_catch_fail not in ("stop", "resume"):
        log.warning(f"  on_catch_fail={on_catch_fail!r} is not 'stop' or "
                    f"'resume'; using 'resume'")
        on_catch_fail = "resume"
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
             + f", flee ~{flee_plan.total:.1f}s/encounter"
             + (f", stall watchdog {flee_plan.stuck_timeout:.0f}s)"
                if flee_plan.stuck_timeout else ", no stall watchdog)"))
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

    # The Walker owns the alternating step and the button pair. It
    # used to be two loose locals plus a counter, which the encounter
    # loop below clobbered by rebinding `a` to a PK6 *address* — the
    # next walk step then passed an int as a button name and killed
    # the run. Keeping the state inside the object makes that
    # impossible to repeat.
    walker = Walker(ctx, _BTN[movement], walk_hold, walk_gap)
    encounters = 0
    stalls = 0
    last_progress = time.monotonic()

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
            precapture = None
            for addr, p in ordered:
                seen.add(p.encryption_key)
                encounters += 1
                saved = _report_encounter(ctx, p, addr, encounters,
                                          "new-key")
                if target_hit is None and _is_target(ctx, p):
                    target_hit = (addr, p)
                    # The pre-capture record: correct in every stat but
                    # unowned, so PKHeX rejects it. Kept only until the
                    # catch lets us re-export a legal one over it.
                    precapture = saved
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
                    # With verification off the B burst has already
                    # done the clearing, and the point of that mode is
                    # to get straight back to walking -- so don't sit
                    # through a second settle.
                    if catch_plan.confirm:
                        catch.settle_after_battle(ctx)
                    # Re-export now that it has an owner. A wild record
                    # has no OT, ball, version or met data -- the game
                    # writes those at the moment of capture -- so the
                    # file saved before the throw is one PKHeX will not
                    # accept. This is the copy worth keeping.
                    _export_caught(ctx, p, party_base, party_stride,
                                   player_ot, precapture)
                    party_keys = _refresh_party(
                        ctx, party_base, party_stride,
                        player_ot) or party_keys
                    last_progress = time.monotonic()
                    continue
                # Unconfirmed. Two very different situations wear this
                # same label, so they get different endings.
                #
                # A full party sends the catch to a PC box, where no
                # party read can ever see it — the throw very likely
                # worked and stopping the hunt would be wrong. Anything
                # else means the ball may genuinely have failed, and
                # walking away from a shiny is not recoverable.
                log.error(f"  CATCH UNCONFIRMED: {result.detail}")
                resume = (on_catch_fail == "resume") or result.party_full
                ctx.dashboard.broadcast(
                    "target_hit", count=encounters,
                    reason=f"catch unconfirmed — {result.detail}",
                    species=p.species, shiny=bool(p.shiny),
                    nature=p.nature, ivs=p.ivs)
                if not resume:
                    log.error("  Bot STOPPED with the battle still open "
                              "— finish it by hand. Set "
                              "on_catch_fail: resume to keep hunting "
                              "instead.")
                    ctx.request_stop("catch failed")
                    return
                log.warning(
                    "  Resuming the hunt. Its .pk6 is already saved in "
                    "targets/ and the encounter is in the log — check "
                    "your party and PC box when you get a moment.")
                # Flee rather than just settling. If the ball landed,
                # the battle is already over and this is B presses
                # around a harmless touch; if it did not, this is what
                # gets us out of the battle instead of walking into a
                # menu until the stall watchdog notices.
                _flee(ctx, screen_layout, run_local, run_override,
                      flee_plan, walker)
                party_keys = _refresh_party(
                    ctx, party_base, party_stride,
                    player_ot) or party_keys
                last_progress = time.monotonic()
                continue
            if not dry:
                # Wait out the battle intro/animation so the command
                # menu (and the RUN button) is actually on screen —
                # walking through it, so the player is already moving
                # when the battle lets go.
                walker.wait(flee_plan.delay)
                _flee(ctx, screen_layout, run_local, run_override,
                      flee_plan, walker)
            last_progress = time.monotonic()
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
        # Watchdog. Walking produces an encounter every few seconds, so
        # a long silence does not mean bad luck — it almost always
        # means the RUN touch missed and the bot is stepping into a
        # battle menu it cannot see, forever. Throw the flee sequence
        # again rather than hunting nothing.
        #
        # Safe to fire in the overworld too: it is B presses either
        # side of one touch, and the trailing B presses close anything
        # the touch happened to open.
        if (flee_plan.stuck_timeout
                and time.monotonic() - last_progress
                > flee_plan.stuck_timeout):
            stalls += 1
            log.warning(
                f"  no encounter for {flee_plan.stuck_timeout:.0f}s "
                f"(stall #{stalls}) — re-sending the RUN sequence in "
                f"case a battle is still open")
            if not dry:
                _flee(ctx, screen_layout, run_local, run_override,
                      flee_plan, walker)
            last_progress = time.monotonic()
            continue

        # Hold B while moving so the player RUNS (covers grass faster
        # → more encounters per minute).
        walker.one_step()

    log.info(f"Shiny hunt stopped after {encounters} encounter(s)"
             + (f", {stalls} stall(s) recovered." if stalls else "."))
