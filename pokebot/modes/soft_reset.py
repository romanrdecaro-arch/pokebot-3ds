"""
Soft reset mode.

For starter / legendary / gift Pokémon. The user saves the game in
the position documented in TUTORIAL.md, then the bot:

  1. Runs the game-specific input sequence to advance dialogue and
     select the chosen starter (or accept the generic gift).
  2. Reads the relevant party slot, decrypts and parses it.
  3. Evaluates against target rules (and a hard species gate when a
     starter is configured).
  4. If target hit: stop. Otherwise: soft reset (L+R+Start) and repeat.

Required offsets:
  - party_base (we read slot N, configurable; default 0 = first slot)
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from ..games import starter_species, starters_for
from ..parser import decrypt_pkm, parse_pkm
from ..platform_utils import focus_azahar

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# First-run offset auto-discovery
# ---------------------------------------------------------------------------
# Starter hunts begin with an empty party, so the user can't run the offset
# finder up-front (no party data to find). We solve the chicken-and-egg by
# running a memory scan after the FIRST starter pickup, when the party has
# been written exactly once. The scan locates party_base, persists it to
# config.yaml, and applies it to ctx.game.offsets so every subsequent reset
# just reads from the known address.

def _discover_offsets_inline(ctx) -> bool:
    """Scan memory for the freshly-populated party block.

    Returns True if party_base was found, applied, and saved. Logs and
    returns False on failure (caller should reset and try again).
    """
    # Imported lazily so importing soft_reset doesn't drag in find_offsets.
    from .. import find_offsets as fo

    log.info("Auto-discovering party_base — first-run scan, ~30-90s. "
             "This only happens once per Azahar session.")
    try:
        hits = list(fo.scan(ctx.rpc))
    except Exception as e:
        log.warning(f"Memory scan failed: {e}")
        return False
    clusters = fo.cluster_hits(hits)
    discovered = fo.derive_offsets_from_clusters(clusters)
    if "party_base" not in discovered:
        log.warning("Scan finished but couldn't isolate the party block. "
                    "Make sure the bot's input sequence actually placed a "
                    "Pokémon in slot 0 — try saving in front of the table "
                    "and re-running.")
        return False

    for k, v in discovered.items():
        if hasattr(ctx.game.offsets, k):
            setattr(ctx.game.offsets, k, v)
    log.info("Discovered: " + ", ".join(
        f"{k}={v:#010x}" for k, v in discovered.items()))

    # Persist so the offset survives across launcher restarts.
    cfg_path = Path(__file__).resolve().parent.parent.parent / "config.yaml"
    if cfg_path.exists():
        try:
            written = fo.write_offsets_to_config(cfg_path, discovered)
            if written:
                log.info(f"Saved offsets to {cfg_path.name}: {written}")
        except Exception as e:
            log.warning(f"Could not write to {cfg_path}: {e}")
    return True


# ---------------------------------------------------------------------------
# Per-game starter input sequences
# ---------------------------------------------------------------------------

def _xy_starter_sequence(ctx, starter: str, gap: float,
                         pre_taps: int, post_taps: int) -> bool:
    """Pokémon X / Y starter selection — fully adaptive.

    Two memory-based detectors replace the old "guess the count" timing:

      1. **Memory-stability detector for the starter menu.** While
         dialogue is rendering, the game's RAM churns constantly
         (text buffers, sprite frames, animations). The moment
         dialogue ends and the cursor sits idle on the menu, that
         memory goes still. We sample a 4KB chunk of FCRAM between
         A presses and stop pressing as soon as the sample is
         identical for ``stable_threshold`` consecutive reads.

      2. **Party-slot poll for the receive phase.** Once the cursor
         is confirmed and we're mashing through the receive dialog,
         we poll party slot 0 (when ``party_base`` is known) and
         exit as soon as a valid PK7 record appears.

    The pre/post tap counts in config.yaml become hard upper bounds
    instead of exact targets — the bot will normally exit each
    phase well before reaching them.

    Returns True when complete; False if a stop was requested mid-run.
    """
    from ..parser import decrypt_pkm, parse_pkm

    starter = (starter or "").lower()

    # Step 1 — face the table (single press only; further d-pad presses
    # would walk the player away if dialogue hasn't started yet).
    ctx.input.tap("DpadLeft", hold_s=0.1)
    time.sleep(0.4)

    # Step 2 — A-mash. Adaptive (memory-stability poll) when we can
    # find a readable sample; falls back to a fixed pre_taps count
    # whenever the RPC can't reach the sample address (some X/Y
    # builds use a different heap layout than USUM, and a 4KB read
    # at an unmapped address can stall for 12s on retries).
    SAMPLE_SIZE = 256          # 1 UDP packet; ~10ms when reachable
    STABLE_THRESHOLD = 3
    # Prefer party_base region when known (definitely mapped on the
    # game we're running). On the first iteration before discovery,
    # try the conservative FCRAM ext-heap default.
    sample_addr = ctx.game.offsets.party_base or 0x33000000

    # Probe once with a hard cap: if the first read fails, skip the
    # adaptive path entirely so we don't burn 30 seconds on retries.
    polling_works = True
    try:
        probe = ctx.rpc.read(sample_addr, SAMPLE_SIZE)
        if not probe:
            polling_works = False
    except Exception:
        polling_works = False

    if polling_works:
        log.info(f"X/Y: A-mashing with stability poll @ {sample_addr:#010x} "
                 f"(cap {pre_taps * 3} presses)")
        last_sample = probe
        stable_runs = 0
        menu_detected_at = -1
        for i in range(pre_taps * 3):
            if ctx.should_stop():
                return False
            ctx.input.tap("A", hold_s=0.05)
            time.sleep(gap)
            try:
                sample = ctx.rpc.read(sample_addr, SAMPLE_SIZE)
            except Exception:
                sample = None
            if sample is not None and sample == last_sample:
                stable_runs += 1
                if stable_runs >= STABLE_THRESHOLD:
                    menu_detected_at = i + 1
                    break
            else:
                stable_runs = 0
            last_sample = sample
        if menu_detected_at > 0:
            log.info(f"X/Y: menu open detected after "
                     f"{menu_detected_at} A presses (stable sample)")
        else:
            log.warning("X/Y: stability never reached cap; proceeding "
                        "to navigation anyway")
    else:
        log.info(f"X/Y: sample addr {sample_addr:#010x} unreadable — "
                 f"falling back to fixed mash of {pre_taps} A presses")
        for _ in range(pre_taps):
            if ctx.should_stop():
                return False
            ctx.input.tap("A", hold_s=0.05)
            time.sleep(gap)

    # Step 3 — cursor navigation now that we know the menu is up.
    if starter == "chespin":
        log.info("X/Y: cursor → Chespin (2× DpadLeft)")
        for _ in range(2):
            if ctx.should_stop(): return False
            ctx.input.tap("DpadLeft", hold_s=0.1)
            time.sleep(0.25)
    elif starter == "froakie":
        log.info("X/Y: cursor → Froakie (2× DpadRight)")
        for _ in range(2):
            if ctx.should_stop(): return False
            ctx.input.tap("DpadRight", hold_s=0.1)
            time.sleep(0.25)
    else:
        log.info("X/Y: cursor stays on Fennekin (no movement)")

    # Step 4 — confirm twice (open Pokéball + 'Yes, take this one').
    for _ in range(2):
        if ctx.should_stop():
            return False
        ctx.input.tap("A", hold_s=0.05)
        time.sleep(0.6)

    # Step 4 — receive phase. Mash B (not A) until the starter is in
    # the party. B advances dialog like A but is safe on the
    # 'Want to nickname?' Yes/No prompt — it cancels with 'No' instead
    # of opening nickname entry, which would trap the bot mid-hunt.
    have_party_addr = bool(ctx.game.offsets.party_base)
    if have_party_addr:
        log.info(f"X/Y: receiving — mashing B until party slot 0 fills "
                 f"(cap {post_taps} presses)")
        addr = ctx.game.offsets.party_base
        for i in range(post_taps):
            if ctx.should_stop():
                return False
            ctx.input.tap("B", hold_s=0.05)
            time.sleep(gap)
            # Poll every 2 presses to keep RPC traffic modest.
            if i % 2 != 0:
                continue
            try:
                raw = ctx.rpc.read(addr, 260)
                pkm = parse_pkm(decrypt_pkm(raw))
                if pkm.checksum_valid and pkm.species:
                    log.info(f"X/Y: starter in party after {i+1} B's "
                             f"(species #{pkm.species})")
                    return True
            except Exception:
                pass
    else:
        log.info(f"X/Y: receiving — first run, mashing B {post_taps}× "
                 "(party_base will be discovered after this)")
        for _ in range(post_taps):
            if ctx.should_stop():
                return False
            ctx.input.tap("B", hold_s=0.05)
            time.sleep(gap)
    return True


# game key -> sequence callable. Other games fall back to generic mash-A.
_SEQUENCES = {
    "X-USA": _xy_starter_sequence,
    "Y-USA": _xy_starter_sequence,
}


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(ctx):
    log.info("Mode: soft_reset")
    # Pull Azahar to the foreground from inside the bot subprocess so
    # pynput's keystrokes (sent from this same process context) land
    # in the right window.
    try:
        if focus_azahar():
            log.info("Azahar window focused.")
    except Exception as e:
        log.warning(f"Couldn't focus Azahar at startup: {e}")
    cfg = ctx.config.get("soft_reset", {})
    slot         = int(cfg.get("read_slot", 0))     # which party slot to read
    advance_taps = int(cfg.get("advance_taps", 60)) # generic-mode mashes
    advance_gap  = float(cfg.get("advance_gap", 0.3))
    post_reset   = float(cfg.get("post_reset_wait", 4.0))
    starter_name = cfg.get("starter")
    # X/Y tunables — empirically tuned with Fast text speed. If text
    # speed is Slow or Medium, bump these proportionally.
    xy_pre_taps  = int(cfg.get("xy_pre_taps", 8))
    xy_post_taps = int(cfg.get("xy_post_taps", 16))

    # No early-exit on missing offsets — starter hunts begin with an empty
    # party, so we discover party_base AFTER the first pickup. Set a flag
    # so the loop below knows to scan once.
    needs_discovery = not ctx.game.offsets.party_base
    if needs_discovery:
        log.info("party_base not configured — will auto-discover after the "
                 "first starter is in the party.")

    starter_id = None
    if starter_name:
        starter_id = starter_species(ctx.game.key, str(starter_name))
        if starter_id:
            log.info(f"Hunting starter: {starter_name} (species #{starter_id})")
        else:
            known = list(starters_for(ctx.game.key).keys())
            log.warning(f"Unknown starter '{starter_name}' for {ctx.game.key}. "
                        f"Known: {known or '(none registered)'}")

    seq_fn = _SEQUENCES.get(ctx.game.key)
    if seq_fn and starter_name:
        log.info(f"Using {ctx.game.key} starter sequence for "
                 f"{starter_name}.")
    elif starter_name:
        log.info(f"No game-specific sequence registered for {ctx.game.key}; "
                 f"falling back to generic A-mash.")

    attempt = 0
    while not ctx.should_stop():
        attempt += 1
        log.info(f"Soft reset attempt #{attempt}")
        ctx.dashboard.broadcast("soft_reset_attempt", count=attempt)

        # Re-assert focus at the start of each iteration in case the user
        # alt-tabbed during the previous one.
        try:
            focus_azahar()
        except Exception:
            pass

        # ------------------------------------------------------------------
        # Phase 1 — drive the game from save screen to populated party slot.
        # ------------------------------------------------------------------
        if seq_fn and starter_name:
            if not seq_fn(ctx, starter_name, advance_gap,
                          xy_pre_taps, xy_post_taps):
                return
        else:
            # Fallback: just mash A until the slot probably exists.
            for _ in range(advance_taps):
                if ctx.should_stop():
                    return
                ctx.input.tap("A", hold_s=0.05)
                time.sleep(advance_gap)

        # ------------------------------------------------------------------
        # Phase 1b (one-time) — discover party_base after the very first
        # starter pickup, when the party block has just been written.
        # ------------------------------------------------------------------
        if needs_discovery:
            ctx.dashboard.broadcast("offset_scan",
                                    state="started", attempt=attempt)
            ok = _discover_offsets_inline(ctx)
            ctx.dashboard.broadcast("offset_scan",
                                    state=("ok" if ok else "fail"),
                                    party_base=ctx.game.offsets.party_base)
            if not ok:
                # Couldn't find a party block. Reset and try again — usually
                # the input sequence didn't actually receive the starter.
                _do_reset(ctx, post_reset)
                continue
            needs_discovery = False

        # ------------------------------------------------------------------
        # Phase 2 — read and parse the resulting party slot.
        # ------------------------------------------------------------------
        addr = (ctx.game.offsets.party_base
                + slot * ctx.game.offsets.party_stride)
        try:
            raw = ctx.rpc.read(addr, 260)
            pkm = parse_pkm(decrypt_pkm(raw))
        except Exception as e:
            log.warning(f"could not read/parse slot {slot}: {e}")
            _do_reset(ctx, post_reset)
            continue

        if not pkm.checksum_valid:
            log.debug("checksum invalid; mashing more then retrying")
            for _ in range(25):
                ctx.input.tap("A", hold_s=0.05)
                time.sleep(0.2)
            try:
                raw = ctx.rpc.read(addr, 260)
                pkm = parse_pkm(decrypt_pkm(raw))
            except Exception:
                _do_reset(ctx, post_reset)
                continue

        ctx.dashboard.broadcast(
            "candidate",
            attempt=attempt,
            species=pkm.species, nickname=pkm.nickname,
            shiny=pkm.shiny, nature=pkm.nature, gender=pkm.gender,
            ivs=pkm.ivs, pid=pkm.pid,
            tsv=pkm.tsv, psv=pkm.psv,
            ability_id=pkm.ability_id, ability_num=pkm.ability_num,
            level=pkm.party["level"] if pkm.party else None,
            moves=pkm.moves,
        )

        # ------------------------------------------------------------------
        # Phase 3 — evaluate. Hard gate on starter species, then target rules.
        # ------------------------------------------------------------------
        if starter_id is not None and pkm.species != starter_id:
            log.info(f"wrong species (#{pkm.species}); resetting")
            _do_reset(ctx, post_reset)
            continue

        target_has_rules = bool(ctx.target and ctx.target.rules)
        is_hit = ctx.target.matches(pkm) if target_has_rules \
                  else (starter_id is not None)
        if is_hit:
            reason = ctx.target.describe(pkm) if target_has_rules \
                else f"starter #{pkm.species}"
            log.info(f"TARGET! attempt {attempt}: {reason}")
            ctx.dashboard.broadcast(
                "target_hit",
                attempt=attempt,
                reason=reason,
                species=pkm.species, shiny=pkm.shiny,
                nature=pkm.nature, ivs=pkm.ivs,
            )
            ctx.request_stop("target hit")
            return

        _do_reset(ctx, post_reset)


def _do_reset(ctx, post_wait: float,
              post_taps: int = 25, post_gap: float = 0.5):
    """Soft-reset and walk the game back to the player-at-save state.

    L+R+Start sends the 3DS to the title screen. From there we have
    to drive the game through:
        Title screen   → press A (or Start)
        Continue menu  → press A on Continue (default cursor)
        Save data flash → A
        any post-load dialog
    until the player sprite is standing on the save tile again. We
    just mash A through everything, which works for X/Y where every
    prompt accepts A.
    """
    ctx.input.soft_reset()
    # Boot logos (Nintendo 3DS, Game Freak) — non-interruptible. About
    # 12s on a typical Azahar config; user-tunable via post_reset_wait.
    time.sleep(post_wait)
    # Make sure Azahar still has focus before we start mashing again —
    # the user may have clicked on the launcher or another window.
    try:
        focus_azahar()
    except Exception:
        pass
    # Mash A: title → continue → save-data confirmation → "welcome
    # back" dialog. Stops as soon as the user requests stop.
    for _ in range(post_taps):
        if ctx.should_stop():
            return
        ctx.input.tap("A", hold_s=0.05)
        time.sleep(post_gap)
