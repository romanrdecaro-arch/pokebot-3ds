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


def _read_looks_garbage(pkm) -> bool:
    """True if the parsed slot 0 has obviously-invalid field values
    that imply the underlying buffer isn't actually a Pokémon record
    (e.g. cached party_base is a checksum false-positive).
    """
    if not pkm.species or pkm.species > 1000:
        return True
    if isinstance(pkm.nature, str) and pkm.nature.startswith("?("):
        return True
    if pkm.party:
        lvl = pkm.party.get("level")
        if lvl is None or lvl < 1 or lvl > 100:
            return True
    if pkm.ability_num not in (0, 1, 2, 4):
        return True
    for stat, v in (pkm.ivs or {}).items():
        if v < 0 or v > 31:
            return True
    return False


def _slot_summary(pkm, slot: int) -> dict:
    """Compact dict suitable for the 'party' broadcast.

    Mirrors the shape produced by observe.py's _summary so the
    launcher's _PartyStrip can consume both event sources.
    """
    return {
        "slot":      slot,
        "species":   pkm.species,
        "nickname":  pkm.nickname,
        "level":     pkm.party["level"] if pkm.party else None,
        "shiny":     pkm.shiny,
        "nature":    pkm.nature,
        "gender":    pkm.gender,
        "pid":       pkm.pid,
        "ivs":       pkm.ivs,
    }


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

    Two strategies:
      1. Standard: 5-7 PK7 records spaced ~484 bytes = full party.
      2. Starter-hunt fallback: a single PK7 record matching the
         expected starter species. Starter hunts begin with one
         Pokémon in the party, so the "5+ cluster" requirement of
         ``derive_offsets_from_clusters`` never matches — that's why
         the user kept seeing "scan failed" even after a successful
         pickup.
    """
    # Imported lazily so importing soft_reset doesn't drag in find_offsets.
    from .. import find_offsets as fo
    from ..games import heap_range_for, starter_species

    # Single tight scan — no auto-fallback to wider ranges. Earlier
    # versions cascaded primary → secondary → full-heap which works
    # in theory but generated enough RPC traffic to crash Azahar's
    # log subsystem on real X/Y sessions. If the primary range
    # misses, surface that clearly and let the user decide whether
    # to widen via a manual `python -m pokebot.find_offsets` run.
    gen = getattr(ctx.game, "generation", 7) or 7
    primary_start, primary_end = heap_range_for(gen)
    span_mb = (primary_end - primary_start) // (1024 * 1024)
    log.info(f"Auto-discovering party_base for Gen {gen}. "
             f"Scanning {primary_start:#010x}-{primary_end:#010x} "
             f"({span_mb} MB). First-run only.")
    try:
        hits = list(fo.scan(ctx.rpc, start=primary_start, end=primary_end))
    except Exception as e:
        log.warning(f"Scan failed: {e}")
        return False
    if not hits:
        log.warning("No PK7 records in the gen-primary heap range. "
                    "Either slot 0 is empty (input sequence didn't "
                    "actually receive a starter) or party data lives "
                    "outside the default range. To rule out the latter, "
                    "run a manual full-heap scan: "
                    "python -m pokebot.find_offsets --full-heap "
                    "--save-config config.yaml")
        return False

    clusters = fo.cluster_hits(hits)
    log.info(f"Scan hits: {len(hits)} record(s), grouped into "
             f"{len(clusters)} cluster(s).")
    # Top-level cluster summary so the user can see what we're dealing with.
    for c in clusters[:8]:
        n = len(c["members"])
        if n >= 2:
            log.info(f"  cluster: start={c['start']:#010x} "
                     f"stride={c['stride']} members={n}")
        else:
            addr, info = c["members"][0]
            log.info(f"  loner:   {addr:#010x}  species=#{info['species']}")
    if len(clusters) > 8:
        log.info(f"  ... + {len(clusters) - 8} more clusters")

    discovered = fo.derive_offsets_from_clusters(clusters)

    # Fallback: starter hunt — look for a single PK7 record matching
    # the chosen starter's species.
    if "party_base" not in discovered:
        starter_name = (ctx.config.get("soft_reset", {}) or {}).get("starter")
        expected_id = (starter_species(ctx.game.key, str(starter_name))
                       if starter_name else None)
        registered = set()
        try:
            from ..games import starters_for
            registered = set(starters_for(ctx.game.key).values())
        except Exception:
            pass

        chosen_addr = None
        chosen_species = None
        # First pass: exact starter match.
        if expected_id:
            for addr, info in hits:
                if info.get("species") == expected_id:
                    chosen_addr, chosen_species = addr, expected_id
                    log.info(f"Fallback: found expected starter "
                             f"#{expected_id} at {addr:#010x}.")
                    break
        # Second pass: any registered starter species.
        if chosen_addr is None and registered:
            for addr, info in hits:
                sp = info.get("species")
                if sp in registered:
                    chosen_addr, chosen_species = addr, sp
                    log.info(f"Fallback: found starter species #{sp} at "
                             f"{addr:#010x} (not the one targeted, but a "
                             f"valid party slot 0).")
                    break
        # Third pass: a single loner anywhere — best guess for slot 0.
        if chosen_addr is None and len(hits) == 1:
            addr, info = hits[0]
            chosen_addr, chosen_species = addr, info.get("species")
            log.info(f"Fallback: only one PK7 record in memory "
                     f"({chosen_addr:#010x}, species #{chosen_species}); "
                     "treating it as party_base.")
        if chosen_addr is not None:
            discovered["party_base"] = chosen_addr
            discovered["party_stride"] = 484
            # If derive_offsets_from_clusters auto-classified the same
            # loner as foe_base, drop it — it's slot 0, not a foe slot.
            if discovered.get("foe_base") == chosen_addr:
                del discovered["foe_base"]
        else:
            log.warning(
                f"Scan completed with {len(hits)} hit(s) but none looked "
                f"like a party slot. Make sure the bot's input sequence "
                f"actually placed a Pokémon in slot 0.")
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
                         pre_taps: int, post_taps: int,
                         receive_gap: float | None = None) -> bool:
    """Pokémon X / Y starter sequence — manually counted, fixed timing.

    The pre-menu and navigation phases use ``gap`` seconds between
    presses (default 2.5s in config). The receive phase uses
    ``receive_gap`` (default 1.0s) since the post-confirm dialogue
    advances faster with B.

    Sequence per attempt:

        1× DpadLeft — kicks off Tierno's cutscene
        25× A — clears Tierno's setup        (gap)
        cursor + confirm:                    (gap)
            Chespin   → 1× A, 2× DpadLeft, 2× A
            Fennekin  → 3× A   (default cursor)
            Froakie   → 1× A, 2× DpadRight, 2× A
        40× B — receives starter             (receive_gap)
                (B avoids opening nickname entry)

    Returns True when complete; False if a stop was requested mid-run.
    """
    starter = (starter or "").lower()
    if receive_gap is None:
        receive_gap = gap

    def _tap(button: str, sleep_for: float) -> bool:
        if ctx.should_stop():
            return False
        ctx.input.tap(button, hold_s=0.05)
        time.sleep(sleep_for)
        return True

    # Step 1 — DpadLeft kicks off Tierno's cutscene. Required even
    # though there's no walking-to-table interaction afterwards.
    if not _tap("DpadLeft", gap):
        return False

    # Step 2 — clear Tierno's setup dialogue.
    log.info(f"X/Y: {pre_taps}× A to clear Tierno's dialogue (gap {gap}s)")
    for _ in range(pre_taps):
        if not _tap("A", gap):
            return False

    # Step 3 — cursor navigation + confirm.
    # For Chespin/Froakie we press A once first to wake the cursor —
    # the d-pad presses don't always register on the very first frame
    # the menu opens. Fennekin doesn't need it because the cursor
    # starts on it by default.
    if starter == "chespin":
        log.info("X/Y: cursor → Chespin (1× A wake, 2× DpadLeft, 2× A)")
        if not _tap("A", gap):
            return False
        for _ in range(2):
            if not _tap("DpadLeft", gap):
                return False
        for _ in range(2):
            if not _tap("A", gap):
                return False
    elif starter == "froakie":
        log.info("X/Y: cursor → Froakie (1× A wake, 2× DpadRight, 2× A)")
        if not _tap("A", gap):
            return False
        for _ in range(2):
            if not _tap("DpadRight", gap):
                return False
        for _ in range(2):
            if not _tap("A", gap):
                return False
    else:  # fennekin (default cursor)
        log.info("X/Y: cursor on Fennekin (3× A)")
        for _ in range(3):
            if not _tap("A", gap):
                return False

    # Step 4 — receive starter. B (not A) so the 'Want to nickname?'
    # prompt is auto-answered No instead of opening name entry.
    #
    # When party_base is known we poll slot 0 between presses and exit
    # the moment a valid PK6 record appears. The game writes the slot
    # during the 'received' animation — well before the nickname
    # prompt — so most iterations exit at ~5-15 B presses instead of
    # the full cap. The cap itself stays as a safety floor for the
    # very first run where party_base hasn't been discovered yet.
    log.info(f"X/Y: up to {post_taps}× B to receive starter "
             f"(gap {receive_gap}s)")
    party_base = ctx.game.offsets.party_base
    if party_base:
        from ..parser import decrypt_pkm, parse_pkm
        for i in range(post_taps):
            if not _tap("B", receive_gap):
                return False
            # Poll every press once a few have fired (skip the first
            # 3 since slot 0 can't possibly be ready yet).
            if i < 3:
                continue
            try:
                raw = ctx.rpc.read(party_base, 260)
                pkm = parse_pkm(decrypt_pkm(raw))
                if pkm.checksum_valid and pkm.species:
                    log.info(f"X/Y: slot 0 written after {i+1} B presses "
                             f"(species #{pkm.species}) — done.")
                    return True
            except Exception:
                pass
    else:
        for _ in range(post_taps):
            if not _tap("B", receive_gap):
                return False
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
    advance_gap  = float(cfg.get("advance_gap", 1.0))
    post_reset   = float(cfg.get("post_reset_wait", 12.0))
    post_reset_taps = int(cfg.get("post_reset_taps", 6))
    post_reset_gap  = float(cfg.get("post_reset_gap", 1.0))
    starter_name = cfg.get("starter")
    # X/Y tunables — manually counted on real hardware.
    xy_pre_taps    = int(cfg.get("xy_pre_taps", 25))
    xy_post_taps   = int(cfg.get("xy_post_taps", 40))
    xy_receive_gap = float(cfg.get("xy_receive_gap", 1.0))

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
                          xy_pre_taps, xy_post_taps,
                          xy_receive_gap):
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
                log.warning(f"Attempt {attempt}: discovery failed — slot 0 "
                            f"isn't populated yet. Likely the cursor never "
                            f"activated and no starter was picked. Resetting.")
                ctx.dashboard.broadcast(
                    "read_failure",
                    attempt=attempt,
                    reason="party_base discovery failed; slot 0 empty.")
                _do_reset(ctx, post_reset, post_reset_taps, post_reset_gap)
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
            ctx.dashboard.broadcast(
                "read_failure",
                attempt=attempt,
                reason=f"RPC read at {addr:#010x} failed: {e}")
            _do_reset(ctx, post_reset, post_reset_taps, post_reset_gap)
            continue

        # Runtime sanity check: a cached party_base from a previous
        # session might point at a buffer whose decrypted bytes pass
        # the checksum but contain garbage values (level > 100 etc.).
        # If the read looks bogus, drop the offset and re-discover.
        if _read_looks_garbage(pkm):
            log.warning(
                f"Attempt {attempt}: slot 0 read at {addr:#010x} returned "
                f"obviously invalid data (species={pkm.species}, "
                f"level={pkm.party.get('level') if pkm.party else 'N/A'}, "
                f"nature={pkm.nature}). Cached party_base is a false "
                f"positive — clearing and rediscovering on next attempt.")
            ctx.game.offsets.party_base = 0
            needs_discovery = True
            ctx.dashboard.broadcast(
                "read_failure", attempt=attempt,
                reason=f"Cached party_base {addr:#010x} is bogus; "
                       "rediscovering.")
            _do_reset(ctx, post_reset, post_reset_taps, post_reset_gap)
            continue

        if not pkm.checksum_valid:
            log.warning(f"Attempt {attempt}: slot 0 checksum invalid "
                        f"(starter probably not in party yet). "
                        f"Mashing more A's and retrying.")
            for _ in range(25):
                ctx.input.tap("A", hold_s=0.05)
                time.sleep(0.2)
            try:
                raw = ctx.rpc.read(addr, 260)
                pkm = parse_pkm(decrypt_pkm(raw))
            except Exception:
                _do_reset(ctx, post_reset, post_reset_taps, post_reset_gap)
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

        # Read slots 1-5 too so the launcher's Party strip can show
        # the full party. Mostly empty during a starter hunt, but
        # populated for legendary / gift hunts on a played-through
        # save.
        party_data = [_slot_summary(pkm, 0)]
        stride = ctx.game.offsets.party_stride
        for i in range(1, 6):
            saddr = ctx.game.offsets.party_base + i * stride
            try:
                sraw = ctx.rpc.read(saddr, 260)
            except Exception:
                continue
            if int.from_bytes(sraw[:4], "little") == 0:
                continue
            try:
                spkm = parse_pkm(decrypt_pkm(sraw))
            except Exception:
                continue
            if spkm.checksum_valid:
                party_data.append(_slot_summary(spkm, i))
        ctx.dashboard.broadcast("party", slots=party_data)

        # ------------------------------------------------------------------
        # Phase 3 — evaluate. Hard gate on starter species, then target rules.
        # ------------------------------------------------------------------
        if starter_id is not None and pkm.species != starter_id:
            log.info(f"wrong species (#{pkm.species}); resetting")
            _do_reset(ctx, post_reset, post_reset_taps, post_reset_gap)
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

        _do_reset(ctx, post_reset, post_reset_taps, post_reset_gap)


def _do_reset(ctx, post_wait: float,
              post_taps: int = 6, post_gap: float = 1.0):
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
