"""
Soft-reset mode (starters / gifts).

Save in front of the starter table with an EMPTY party, then per
attempt the bot:

  1. Presses A, fast, until a Pokémon lands in the party.
  2. Detects the received Pokémon by CONTENT — observe.get_party()
     locates the player-owned party in RAM by scanning for
     checksum-valid PK6 whose OT is the trainer (the same
     relocation-proof method the shiny hunt uses). No party_base /
     offset hunting, no "run debug first".
  3. Evaluates: species must be the chosen starter, then the target
     filter (shiny / IVs / nature …).
  4. Hit → stop + alert (it's in your party, go save). Miss → soft
     reset (L+R+Start), wait for the party to empty, and repeat.

There is NO cursor navigation. The old sequence walked the cursor with
DpadLeft/DpadRight on a fixed one-second cadence, and every step of it
assumed the cutscene was exactly where the timings said — a cursor
misfire meant receiving the wrong starter and burning the attempt.
Whichever starter sits under the default cursor is the one taken, so
save in front of the one you want; the ``starter`` setting is now only
used to check what actually arrived.

config.yaml soft_reset.trainer_name MUST match your in-game OT (it's
how the party is found). Defaults to games.DEFAULT_OT_NAME.
"""
from __future__ import annotations

import logging
import time

from ..games import DEFAULT_OT_NAME, starter_species, starters_for
from ..pk6_export import ensure_targets_dir, save_target_pk6
from ..platform_utils import focus_azahar
from .observe import (get_party, broadcast_party, quick_get_party,
                       scan_nonparty, _report_encounter)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-game starter input sequence
# ---------------------------------------------------------------------------

#: A presses, at the cadence the Celebi hunt settled on. The floor is
#: Azahar's: a key posted to its window is picked up when Qt next
#: drains its event queue, so a press held for less than one of those
#: can go down and come up inside a single polled frame and score no
#: press at all. 30 ms clears that and gives ~33 presses/second.
_PRESS_HOLD_S = 0.03
_PRESS_GAP_S = 0.0
_PRESS_HOLD_FLOOR = 0.01

#: Poll the party this often while spamming. The fast party check is a
#: 12 KB window — a dozen RPC round trips, against 30 ms for a press —
#: so checking every press would halve the press rate for nothing.
#: 0.15s bounds the overshoot to about five presses past the moment the
#: starter lands, which is what keeps the spam from running deep into
#: the nickname prompt.
_DETECT_EVERY_S = 0.15


def _spam_a_until_received(ctx, hold: float, gap: float, timeout: float,
                           detect_cb, detect_every: float
                           = _DETECT_EVERY_S) -> bool:
    """Press A until something lands in the party.

    Replaces the old fixed, hand-timed sequence — 1x DpadLeft, 25x A,
    two cursor presses, then up to 30x B on one-second gaps, about a
    minute of pressing whose every step assumed the cutscene was where
    the timings said it would be. There is no cursor navigation here at
    all: whichever starter sits under the default cursor is the one
    taken, so save in front of the one you want.

    Driven by the party rather than by a clock, exactly like the Celebi
    hunt: press, check, stop the moment a Pokemon appears. That is also
    what keeps the spam out of the nickname prompt, which comes AFTER
    the mon is added to the party — so detecting it promptly means the
    presses stop before the prompt matters.
    """
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    next_check = 0.0
    presses = 0
    while not ctx.should_stop() and time.monotonic() < deadline:
        now = time.monotonic()
        if now >= next_check:
            next_check = now + detect_every
            if detect_cb():
                elapsed = now - started
                log.info(f"  received after {presses} A presses "
                         f"({presses / max(elapsed, 1e-9):.0f}/s, "
                         f"{elapsed:.1f}s)")
                return True
        ctx.input.tap("A", hold_s=hold)
        presses += 1
        if gap:
            ctx._stop_evt.wait(gap)
    return False


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------

def _do_reset(ctx, post_wait: float, post_taps: int, post_gap: float):
    """L+R+Start to the title, wait out the boot logos, then mash A
    (title → Continue → save-data confirm → welcome dialog)."""
    ctx.input.soft_reset()
    ctx._stop_evt.wait(post_wait)
    try:
        focus_azahar()
    except Exception:
        pass
    for _ in range(post_taps):
        if ctx.should_stop():
            return
        ctx.input.tap("A", hold_s=0.05)
        ctx._stop_evt.wait(post_gap)


def _reset_until_party_empty(ctx, detect_cb, post_wait: float,
                             timeout: float) -> bool:
    """Soft-reset, and wait for the received starter to disappear.

    The party emptying is the proof that L+R+Start actually landed. It
    matters more now than it did under the old fixed sequence: the A
    spam that follows is 33 presses a second, and if the reset silently
    did nothing those presses go into whatever is still on screen — the
    nickname keyboard, most likely — while the bot waits for a starter
    it already has. Checking costs one fast party read.

    Nothing is pressed while waiting, for the same reason.
    """
    ctx.input.soft_reset()
    ctx._stop_evt.wait(post_wait)
    try:
        focus_azahar()
    except Exception:
        pass
    deadline = time.monotonic() + timeout
    while not ctx.should_stop() and time.monotonic() < deadline:
        if not detect_cb():
            return True
        ctx._stop_evt.wait(0.25)
    return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(ctx):
    """Soft-reset entry point.

    Dispatches on ``config.soft_reset.target`` (set by the launcher's
    Target sub-dropdown or ``--soft-reset-target``):

      ``starters`` (default) — the full starter sequence below.
      ``snorlax``  — Route 7 sleeping Snorlax (foe-window detect).
      ``lapras``   — Route 12 Hiker gift Lapras (party-slot detect).

    Korrina's Lucario at the Tower of Mastery is SHINY-LOCKED in
    X/Y (its PID is set by the cutscene script, not rolled), so
    there's no point soft-resetting it — that target has been
    removed from the dropdown.
    """
    ensure_targets_dir()
    # Wipe any cached party signature so the FIRST broadcast_party
    # call inside the chosen sub-mode pushes the current in-game
    # party to the launcher's strip. Without this, the strip can
    # stay frozen on whatever party the previous run last broadcast
    # (broadcast_party suppresses identical-sig writes).
    if hasattr(ctx, "_party_sig"):
        ctx._party_sig = None
    cfg = ctx.config.get("soft_reset", {}) or {}
    target = str(cfg.get("target", "starters")).lower()
    if target == "starters":
        return _run_starters(ctx, cfg)
    if target == "snorlax":
        return _run_snorlax(ctx, cfg)
    if target == "lapras":
        return _run_lapras(ctx, cfg)
    return _run_stub(ctx, target)


def _run_stub(ctx, name: str) -> None:
    """Placeholder for the X/Y legendary / gift soft-resets. The button
    sequence will be filled in per target as it's user-verified; for
    now we just stop cleanly with a clear log line so the launcher
    doesn't sit there appearing to run."""
    log.info(f"Mode: soft_reset → {name}")
    log.warning(f"  Soft-reset sequence for {name.title()} is not "
                f"implemented yet.")
    log.info("  Add the per-target sequence in pokebot/modes/"
             "soft_reset.py to enable. Stopping.")
    ctx.request_stop(f"{name} stub")


def _run_snorlax(ctx, cfg):
    """Snorlax soft-reset (X/Y, Route 7 bridge).

    Player setup (one-time, before starting the bot):
      1. Retrieve the Poké Flute from the Pokémon Café in Lumiose,
         say NO when the woman offers the choice (so the flute is
         in your bag).
      2. Stand directly south of the sleeping Snorlax on the wooden
         bridge of Route 7, facing it. SAVE the game here — each
         soft-reset returns the player to the save spot.

    Per attempt the bot:
      1. Mashes A — playing the Poké Flute wakes Snorlax and starts
         the wild battle.
      2. Polls the foe window between A presses; the moment a new
         (never-baseline) PK6 appears it's the wild Snorlax.
      3. Target hit (shiny / matches target rules) → stop + alert +
         save .pk6. Miss → L+R+Start and repeat.
    """
    log.info("Mode: soft_reset → Snorlax (Route 7 bridge)")
    log.info("  Setup: Poké Flute in bag · standing on bridge facing "
             "the sleeping Snorlax · game saved here.")

    o = ctx.game.offsets
    foe_base = o.foe_base
    foe_len = getattr(o, "foe_scan_len", 0) or 0x20000
    party_base = o.party_base
    party_stride = o.party_stride or 484
    player_ot = cfg.get("trainer_name", DEFAULT_OT_NAME)
    post_reset = float(cfg.get("post_reset_wait", 3.5))
    post_reset_taps = int(cfg.get("post_reset_taps", 4))
    post_reset_gap = float(cfg.get("post_reset_gap", 0.6))
    a_gap = float(cfg.get("snorlax_a_gap", 0.4))
    a_max = int(cfg.get("snorlax_a_max", 60))
    if not foe_base:
        log.error("foe_base not configured (X/Y: 0x08800000); aborting.")
        return

    try:
        focus_azahar()
    except Exception:
        pass

    attempt = 0
    while not ctx.should_stop():
        attempt += 1
        log.info(f"Snorlax attempt #{attempt}")
        ctx.dashboard.broadcast("soft_reset_attempt", count=attempt)
        try:
            focus_azahar()
        except Exception:
            pass

        # Refresh party so the baseline scan excludes the player's
        # own mons from "new wild" detection.
        party = get_party(ctx, party_base, party_stride, player_ot)
        party_keys = broadcast_party(ctx, party)

        # Baseline non-party PK6 — a wild left over from the last
        # attempt (or a stale battle copy) lives here; only a KEY not
        # in this set counts as the newly-woken Snorlax.
        baseline = {p.encryption_key for _, p in
                    scan_nonparty(ctx, foe_base, foe_len, party_keys)}

        # Mash A, poll the foe window between each press.
        wild = None
        for i in range(a_max):
            if ctx.should_stop():
                return
            ctx.input.tap("A", hold_s=0.05)
            ctx._stop_evt.wait(a_gap)
            cands = scan_nonparty(ctx, foe_base, foe_len, party_keys)
            new = sorted(
                ((a, p) for a, p in cands
                 if p.encryption_key not in baseline),
                key=lambda ap: ap[0])
            if new:
                wild = new[0]
                log.info(f"  Snorlax detected after {i + 1} A "
                         f"press(es).")
                break

        if wild is None:
            log.warning(f"  attempt {attempt}: no encounter after "
                        f"{a_max} A presses. Check you're on the "
                        f"bridge facing Snorlax. Resetting.")
            ctx.dashboard.broadcast(
                "read_failure", attempt=attempt,
                reason="no foe after A-mash")
            _do_reset(ctx, post_reset, post_reset_taps, post_reset_gap)
            continue

        addr, pkm = wild
        _report_encounter(ctx, pkm, addr, attempt, "snorlax-A")

        is_target = bool(
            pkm.shiny or (ctx.target and ctx.target.matches(pkm)))
        if is_target:
            save_target_pk6(ctx, addr, pkm,
                            "shiny" if pkm.shiny else "snorlax")
            reason = (ctx.target.describe(pkm) if (ctx.target
                      and ctx.target.matches(pkm)) else "shiny Snorlax")
            bar = "*" * 30
            for line in (
                bar, f"  TARGET — attempt #{attempt}: {reason}",
                "  Bot STOPPED — battle left on screen. Catch it!",
                bar):
                log.info(line)
            ctx.dashboard.broadcast(
                "target_hit", attempt=attempt, count=attempt,
                reason=reason, species=pkm.species, shiny=pkm.shiny,
                nature=pkm.nature, ivs=pkm.ivs)
            ctx.request_stop("target hit")
            return

        _do_reset(ctx, post_reset, post_reset_taps, post_reset_gap)

    log.info(f"Snorlax soft-reset stopped after {attempt} attempt(s).")


def _run_lapras(ctx, cfg):
    """Lapras soft-reset (X/Y, Route 12 Hiker gift).

    Player setup (one-time, before starting the bot):
      1. Make sure your party has at least one OPEN slot.
      2. Walk up to the Hiker NPC on Route 12 (the one standing by
         the route sign — see the screenshot in the tutorial). Stand
         directly in front of him, facing him.
      3. SAVE the game here. Every reset returns the player to this
         spot with the Hiker still waiting to offer Lapras.

    Per attempt the bot:
      1. Logs the party AT START (baseline) — raw scan with
         addresses so any off-grid records show up too.
      2. Runs the full 6 A → 6 B sequence (1-second intervals) end
         to end without mid-sequence polling. A's accept Lapras and
         clear "received" text; B's decline the nickname prompt and
         dismiss the trailing dialog.
      3. Waits a short settle so the final dialog frame resolves
         and the new PK6 finishes writing to RAM.
      4. Logs the party AFTER (raw scan with addresses).
      5. Finds the new-key record (the gift mon by construction)
         and evaluates: target hit → stop + save .pk6; miss → reset.
    """
    LAPRAS_SPECIES = 131
    log.info("Mode: soft_reset → Lapras (Route 12 Hiker)")
    log.info("  Setup: party has open slot · standing in front of "
             "the Route 12 Hiker · game saved here.")

    player_ot = cfg.get("trainer_name", DEFAULT_OT_NAME)
    post_reset = float(cfg.get("post_reset_wait", 3.5))
    post_reset_taps = int(cfg.get("post_reset_taps", 4))
    post_reset_gap = float(cfg.get("post_reset_gap", 0.6))
    press_gap = float(cfg.get("lapras_press_gap", 1.0))
    settle = float(cfg.get("lapras_settle", 1.5))
    PATTERN = ["A"] * 6 + ["B"] * 6
    party_base = ctx.game.offsets.party_base
    party_stride = ctx.game.offsets.party_stride or 484

    try:
        focus_azahar()
    except Exception:
        pass

    attempt = 0
    while not ctx.should_stop():
        attempt += 1
        log.info(f"Lapras attempt #{attempt}")
        ctx.dashboard.broadcast("soft_reset_attempt", count=attempt)
        try:
            focus_azahar()
        except Exception:
            pass

        # 1. BASELINE — snapshot + log the party BEFORE the sequence.
        # Invalidate the cached party window first so the scan
        # re-locates the party from scratch each attempt. A gift mon
        # can land in a live-party buffer that doesn't overlap the
        # save-block cluster the cache was anchored on; the END scan
        # below needs the broader view to spot it, and starting from
        # a fresh cache here keeps START and END comparable.
        if hasattr(ctx, "_party_win"):
            ctx._party_win = None
        baseline_disp = get_party(ctx, party_base, party_stride,
                                  player_ot)
        if baseline_disp:
            broadcast_party(ctx, baseline_disp)
        if hasattr(ctx, "_party_win"):
            ctx._party_win = None
        baseline = get_party(ctx, party_base, party_stride, player_ot,
                             contiguous=False)
        if baseline:
            log.info(
                f"  party at sequence START ({len(baseline)} PK6): "
                + ", ".join(
                    f"#{p.species}@"
                    f"{getattr(p, 'source_address', 0):#010x}"
                    for p in baseline))
        else:
            log.warning("  baseline scan returned 0 PK6 — "
                        "trainer_name mismatch or party_base "
                        "unlocated.")
        baseline_keys = {p.encryption_key for p in baseline}

        # 2. SEQUENCE — full 6 A + 6 B at press_gap intervals, end to
        # end. No mid-sequence polling: we want the dialog to finish
        # cleanly, then read the result once.
        for btn in PATTERN:
            if ctx.should_stop():
                return
            ctx.input.tap(btn, hold_s=0.05)
            ctx._stop_evt.wait(press_gap)

        # 3. SETTLE — let the final B-clear text finish and any
        # remaining PK6 writes flush before we read.
        ctx._stop_evt.wait(settle)

        # 4. FINAL — snapshot + log the party AFTER the sequence.
        # Invalidate the cached window AGAIN before re-scanning so a
        # broad scan runs (gift mons can land outside the baseline
        # cache).
        if hasattr(ctx, "_party_win"):
            ctx._party_win = None
        final = get_party(ctx, party_base, party_stride, player_ot,
                          contiguous=False)
        log.info(
            f"  party at sequence END ({len(final)} PK6): "
            + (", ".join(
                f"#{p.species}@"
                f"{getattr(p, 'source_address', 0):#010x}"
                for p in final) if final else "(empty)"))
        # New mons = anything in the final scan whose key wasn't in
        # the baseline. By construction that's just the gift Lapras.
        new_mons = [p for p in final
                    if p.encryption_key not in baseline_keys]

        # Gift Lapras is fixed at Lv30 — override unconditionally
        # so the strip + candidate broadcast both show 30 regardless
        # of whether the live record we found has party stats yet
        # (the box-format scan finds the gift before the party-stats
        # block is initialized, leaving p.party=None and the UI
        # displaying "?"). Synthesize a minimal party dict in that
        # case; the other party fields (HP/Atk/etc.) don't matter
        # for soft-reset reporting.
        LAPRAS_GIFT_LEVEL = 30
        for p in new_mons:
            if p.species != LAPRAS_SPECIES:
                continue
            base = p.party or {}
            p.party = {**base, "level": LAPRAS_GIFT_LEVEL}

        # Strip = filtered save-block party (clean, no box ghosts)
        # PLUS any new-key record (the gift mon, wherever it lives
        # in RAM).
        if hasattr(ctx, "_party_win"):
            ctx._party_win = None
        clean_party = get_party(ctx, party_base, party_stride,
                                player_ot)
        strip = (list(clean_party) + new_mons)[:6]
        if strip:
            broadcast_party(ctx, strip)

        # 5. EVALUATE — first new-key record is the gift mon. By
        # construction (the Hiker's dialog is the only source of a
        # new key during the sequence), this is the Lapras.
        new_pkm = new_mons[0] if new_mons else None

        if new_pkm is None:
            log.warning(f"  attempt {attempt}: no new Pokémon in "
                        f"party after sequence. Check that you're "
                        f"in front of the Hiker with an open slot. "
                        f"Resetting.")
            ctx.dashboard.broadcast(
                "read_failure", attempt=attempt,
                reason="no new mon in party after sequence")
            _do_reset(ctx, post_reset, post_reset_taps, post_reset_gap)
            continue
        if new_pkm.species != LAPRAS_SPECIES:
            log.info(f"  new Pokémon is #{new_pkm.species}, not "
                     f"Lapras (#131) — evaluating anyway.")

        pkm = new_pkm
        ctx.dashboard.broadcast(
            "candidate", attempt=attempt,
            species=pkm.species, nickname=pkm.nickname,
            shiny=pkm.shiny, nature=pkm.nature, gender=pkm.gender,
            ivs=pkm.ivs, pid=pkm.pid, tsv=pkm.tsv, psv=pkm.psv,
            ability_id=pkm.ability_id, ability_num=pkm.ability_num,
            level=(pkm.party or {}).get("level"),
            moves=pkm.moves)
        log.info(f"  new mon: #{pkm.species} {pkm.nickname or ''} "
                 f"{'★SHINY★ ' if pkm.shiny else ''}"
                 f"nature={pkm.nature} IVs={pkm.ivs} "
                 f"PID={pkm.pid:08X} PSV={pkm.psv} TSV={pkm.tsv}")
        # Diagnostic — if two consecutive attempts log the SAME
        # source_address + SAME enc_key, the bot's reading the
        # exact same RAM bytes both times and the game's gift
        # generator is reusing the buffer (so the IV pattern
        # really is fixed). If addresses differ but IVs match,
        # the game has a fixed IV spread for this gift. If
        # addresses match but PIDs/IVs differ, the buffer is being
        # rewritten between reads.
        addr_log = getattr(pkm, "source_address", 0)
        log.info(f"  diag: addr={addr_log:#010x} "
                 f"enc_key={pkm.encryption_key:08X} "
                 f"nature_id={pkm.nature_id} "
                 f"ivs_word={(pkm.ivs['HP'] | pkm.ivs['Atk']<<5 | pkm.ivs['Def']<<10 | pkm.ivs['Spe']<<15 | pkm.ivs['SpA']<<20 | pkm.ivs['SpD']<<25):08X}")

        is_target = bool(
            pkm.shiny or (ctx.target and ctx.target.matches(pkm)))
        if is_target:
            addr = getattr(pkm, "source_address", None)
            if addr is not None:
                save_target_pk6(ctx, addr, pkm,
                                "shiny" if pkm.shiny else "lapras")
            reason = (ctx.target.describe(pkm) if (ctx.target
                      and ctx.target.matches(pkm))
                      else "shiny Lapras")
            bar = "*" * 30
            for line in (
                bar, f"  TARGET — attempt #{attempt}: {reason}",
                "  Bot STOPPED — it's in your party. Decline the "
                "nickname (B) and go SAVE!",
                bar):
                log.info(line)
            ctx.dashboard.broadcast(
                "target_hit", attempt=attempt, count=attempt,
                reason=reason, species=pkm.species, shiny=pkm.shiny,
                nature=pkm.nature, ivs=pkm.ivs)
            ctx.request_stop("target hit")
            return

        _do_reset(ctx, post_reset, post_reset_taps, post_reset_gap)

    log.info(f"Lapras soft-reset stopped after {attempt} attempt(s).")


def _run_starters(ctx, cfg):
    log.info("Mode: soft_reset (starter)")
    try:
        if focus_azahar():
            log.info("  Azahar window focused.")
    except Exception as e:
        log.warning(f"  couldn't focus Azahar: {e}")

    player_ot = cfg.get("trainer_name", DEFAULT_OT_NAME)
    post_reset = float(cfg.get("post_reset_wait", 3.5))
    press_hold = float(cfg.get("press_hold", _PRESS_HOLD_S))
    press_gap = float(cfg.get("press_interval", _PRESS_GAP_S))
    detect_every = float(cfg.get("detect_every", _DETECT_EVERY_S))
    # The cutscene has its own unskippable animations, so this bounds
    # a stuck attempt rather than pacing a working one.
    receive_timeout = float(cfg.get("receive_timeout", 180.0))
    reset_timeout = float(cfg.get("reset_timeout", 30.0))
    starter_name = cfg.get("starter")
    # How long to wait for the starter to show up in the party after
    # the input sequence before declaring the attempt a miss.
    detect_tries = int(cfg.get("detect_tries", 12))
    detect_gap = float(cfg.get("detect_gap", 1.5))

    if press_hold < _PRESS_HOLD_FLOOR:
        log.warning(f"  press_hold {press_hold:.3f}s is below "
                    f"{_PRESS_HOLD_FLOOR:.3f}s — Azahar may see the key go "
                    f"down and up inside one polled frame and score no "
                    f"press at all. Raise it if attempts start timing out.")
    log.info(f"  A presses: {press_hold * 1000:.0f}ms hold"
             + (f" + {press_gap * 1000:.0f}ms gap" if press_gap else "")
             + f"  (~{1 / max(press_hold + press_gap, 1e-9):.0f}/s)")
    party_base = ctx.game.offsets.party_base
    party_stride = ctx.game.offsets.party_stride or 484

    # Map ALL three starters for the chosen game — used for the
    # species gate, and logged so it's clear the script handles
    # Chespin / Fennekin / Froakie (not just Fennekin).
    all_starters = starters_for(ctx.game.key) or {}
    log.info(f"  {ctx.game.key} starters: " + ", ".join(
        f"{n.title()}=#{i}" for n, i in all_starters.items()) or "(none)")

    starter_id = None
    if starter_name:
        starter_id = starter_species(ctx.game.key, str(starter_name))
        if starter_id:
            log.info(f"  hunting {starter_name.title()} "
                     f"(species #{starter_id}); OT {player_ot!r}")
        else:
            log.warning(f"  unknown starter {starter_name!r} for "
                        f"{ctx.game.key}; known: "
                        f"{list(all_starters)}")
    log.info("  No cursor navigation: whichever starter is under the "
             "default cursor is the one taken, so save in front of the "
             "one you want. The starter setting is only used to check "
             "what arrived.")

    # An attempt is "party empty -> press A -> party has one". Starting
    # with a mon already there means the save is not where the hunt
    # expects, and the bot would call it a fresh catch every attempt
    # without pressing anything.
    def _party_has_mon() -> bool:
        return bool(quick_get_party(ctx, player_ot))

    if _party_has_mon():
        log.error("Your party already has a Pokemon. This mode expects a "
                  "save made in front of the starter table with an EMPTY "
                  "party — otherwise it cannot tell a new starter from "
                  "the one already there. Stopping.")
        ctx.dashboard.broadcast("read_failure",
                                reason="party not empty at start")
        ctx.request_stop("party not empty at start")
        return

    def _reset_or_stop() -> bool:
        """Reset and confirm it took, or stop the hunt saying so."""
        if _reset_until_party_empty(ctx, _party_has_mon, post_reset,
                                    reset_timeout):
            return True
        if ctx.should_stop():
            return False
        log.error(f"  the starter never left the party, so L+R+Start did "
                  f"not register within {reset_timeout:.0f}s. Stopping "
                  f"rather than spamming A into whatever is still on "
                  f"screen while waiting for a starter that is already "
                  f"there.")
        ctx.dashboard.broadcast("read_failure",
                                reason="soft reset did not register")
        ctx.request_stop("soft reset did not register")
        return False

    attempt = 0
    while not ctx.should_stop():
        attempt += 1
        log.info(f"Soft reset attempt #{attempt}")
        ctx.dashboard.broadcast("soft_reset_attempt", count=attempt)
        try:
            focus_azahar()
        except Exception:
            pass

        # 1. Press A until something lands in the party.
        #
        # Deliberately NOT gated on species: the wrong starter still
        # ends the pressing, and the species check below reports it
        # properly. Waiting for the right one instead would keep
        # mashing A deep into the nickname keyboard.
        if not _spam_a_until_received(ctx, press_hold, press_gap,
                                      receive_timeout, _party_has_mon,
                                      detect_every):
            if ctx.should_stop():
                return
            log.error(f"  nothing reached the party in "
                      f"{receive_timeout:.0f}s of A presses. Either the "
                      f"save is not in front of the starter table, or "
                      f"Azahar is not receiving input — run "
                      f"scripts/test_input.py to tell those apart.")
            ctx.dashboard.broadcast(
                "read_failure", attempt=attempt,
                reason="no starter received from A presses")
            ctx.request_stop("no starter received — check input and save")
            return

        # 2. Detect the received starter by content (relocation-proof;
        #    no offsets needed). Exit as soon as ANY mon lands in the
        #    party — the species check happens next so a wrong-cursor
        #    pickup is logged clearly instead of timing out.
        pkm = None
        for _ in range(detect_tries):
            if ctx.should_stop():
                return
            party = get_party(ctx, party_base, party_stride, player_ot)
            if party:
                broadcast_party(ctx, party)
                pkm = party[0]
                break
            ctx._stop_evt.wait(detect_gap)

        if pkm is None:
            log.warning(f"  attempt {attempt}: nothing in the party "
                        f"after the sequence (cursor likely missed or "
                        f"save isn't in front of the table). Resetting.")
            ctx.dashboard.broadcast(
                "read_failure", attempt=attempt,
                reason="no party member after sequence")
            if not _reset_or_stop():
                return
            continue

        # Species gate — accepts the chosen starter; logs+resets if
        # the cursor landed on the wrong one (e.g. picked Fennekin
        # when Chespin was requested).
        if starter_id is not None and pkm.species != starter_id:
            got = next((n for n, i in all_starters.items()
                        if i == pkm.species), f"#{pkm.species}")
            log.warning(f"  attempt {attempt}: WRONG starter — got "
                        f"{got.title()} (#{pkm.species}), wanted "
                        f"{starter_name.title()} (#{starter_id}). "
                        f"Cursor misfire; resetting.")
            ctx.dashboard.broadcast(
                "read_failure", attempt=attempt,
                reason=f"wrong starter (got #{pkm.species}, "
                       f"wanted #{starter_id})")
            if not _reset_or_stop():
                return
            continue

        # 3. Report + evaluate. X/Y starters are ALWAYS received at
        # Lv5 — the byte at 0xEC of the live party slot right after
        # the cutscene isn't yet the level field (transitional
        # state), so hardcode 5 for display rather than show a wrong
        # value like 84/48.
        STARTER_LEVEL = 5
        if pkm.party:
            pkm.party = {**pkm.party, "level": STARTER_LEVEL}
        # Re-broadcast the strip with the corrected level too.
        try:
            broadcast_party(ctx, [pkm])
        except Exception:
            pass
        ctx.dashboard.broadcast(
            "candidate", attempt=attempt,
            species=pkm.species, nickname=pkm.nickname,
            shiny=pkm.shiny, nature=pkm.nature, gender=pkm.gender,
            ivs=pkm.ivs, pid=pkm.pid, tsv=pkm.tsv, psv=pkm.psv,
            ability_id=pkm.ability_id, ability_num=pkm.ability_num,
            level=STARTER_LEVEL,
            moves=pkm.moves)
        log.info(f"  starter: #{pkm.species} {pkm.nickname or ''} "
                 f"Lv{STARTER_LEVEL} "
                 f"{'★SHINY★ ' if pkm.shiny else ''}"
                 f"nature={pkm.nature} IVs={pkm.ivs} "
                 f"PID={pkm.pid:08X} PSV={pkm.psv} TSV={pkm.tsv}")

        has_rules = bool(ctx.target and ctx.target.rules)
        hit = (ctx.target.matches(pkm) if has_rules
               else starter_id is not None)
        if hit:
            reason = (ctx.target.describe(pkm) if has_rules
                      else f"starter #{pkm.species}")
            addr = getattr(pkm, "source_address", None)
            if addr is not None:
                save_target_pk6(ctx, addr, pkm,
                                "shiny" if pkm.shiny else "starter")
            bar = "*" * 30
            for line in (bar, f"  TARGET — attempt #{attempt}: {reason}",
                         "  Bot STOPPED — it's in your party. Go SAVE!",
                         bar):
                log.info(line)
            ctx.dashboard.broadcast(
                "target_hit", attempt=attempt, count=attempt,
                reason=reason, species=pkm.species, shiny=pkm.shiny,
                nature=pkm.nature, ivs=pkm.ivs)
            ctx.request_stop("target hit")
            return

        if not _reset_or_stop():
                return
