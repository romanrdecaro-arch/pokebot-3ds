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
                button, hold_s = next(walk_dir_iter)
                ctx.input.tap(button, hold_s=hold_s)
                time.sleep(0.15)            # brief gap before next hold
            else:
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

        # Not a hit: flee. From the FIGHT cursor (default position
        # when the battle menu opens) the X/Y 2×2 grid navigation is
        # Down → POKEMON, Right → RUN, A to confirm. 0.25 s between
        # presses gives the menu time to redraw.
        log.info(f"  enc#{encounters}: not a target — fleeing "
                 f"(Down, Right, A).")
        for button in ("DpadDown", "DpadRight", "A"):
            ctx.input.tap(button, hold_s=0.05)
            time.sleep(0.25)
        # Dismiss "Got away safely!" / "Couldn't escape!" dialogue.
        for _ in range(6):
            ctx.input.tap("B", 0.05)
            time.sleep(0.25)
        # Wait for battle flag to clear (max ~10 s).
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
    """Yield (button, hold_seconds) tuples for the walking loop.

    Pattern: 2.0 s in direction A to kick things off, then alternate
    3.0 s holds in B, A, B, A, … forever. Long holds mean the bot
    walks several tiles per direction-change instead of just turning
    in place, which is what triggers wild-grass encounters.
    """
    if axis == "vertical":
        a, b = "DpadUp", "DpadDown"
    else:
        a, b = "DpadLeft", "DpadRight"
    yield (a, 2.0)
    while True:
        yield (b, 3.0)
        yield (a, 3.0)


# ---------------------------------------------------------------------------
# foe_base auto-discovery
# ---------------------------------------------------------------------------

def _autodiscover_foe_base(ctx, movement: str) -> bool:
    """Walk + scan in parallel until a battle exposes the foe slot.

    The walking loop runs in the main thread (so the player starts
    moving immediately, with no scan-blocked dead time), and a scanner
    runs alongside on a background thread:

      - Baseline pass catalogues every valid PK6 currently in RAM
        (party + boxes + any stale battle data).
      - Subsequent passes diff against the baseline. The first new
        address is treated as the foe slot, since the foe is the only
        new PK6 the engine allocates when a wild battle starts.

    Inputs go through the input driver (PostMessage on Windows, no
    RPC), the scanner owns the RPC, so there's no contention on the
    socket. Result is persisted to config.yaml.
    """
    import threading

    from ..find_offsets import scan
    from ..games import heap_range_for

    range_ = heap_range_for(ctx.game.generation)
    log.info("foe_base = 0 — auto-discovering. Bot is walking now; "
             "scanner is sniffing memory in parallel.")
    log.info(f"  heap range:    {range_[0]:#x}–{range_[1]:#x}")
    log.info(f"  walk axis:     {movement}")
    log.info(f"  expected time: 30-90 s once you bump into a wild encounter.")
    ctx.dashboard.broadcast("offset_scan", state="started",
                            target="foe_base")

    found = [None]                   # type: list[int | None]
    discovered = threading.Event()

    def _scanner() -> None:
        baseline: set[int] = set()
        t0 = time.monotonic()
        log.info("  scanner: building baseline (overworld) …")
        try:
            for addr, _info in scan(ctx.rpc, range_[0], range_[1],
                                    chunk=0x4000, throttle_s=0.005):
                baseline.add(addr)
                if ctx.should_stop() or discovered.is_set():
                    return
        except Exception as e:
            log.error(f"  scanner: baseline failed: {e}")
            return
        log.info(f"  scanner: baseline = {len(baseline)} PK6 records "
                 f"({time.monotonic() - t0:.1f}s). Watching for new ones.")

        pass_n = 0
        while not ctx.should_stop() and not discovered.is_set():
            pass_n += 1
            t1 = time.monotonic()
            new_here: list[int] = []
            try:
                for addr, _info in scan(ctx.rpc, range_[0], range_[1],
                                        chunk=0x4000, throttle_s=0.005):
                    if addr not in baseline:
                        new_here.append(addr)
                    if ctx.should_stop() or discovered.is_set():
                        return
            except Exception as e:
                log.warning(f"  scanner: pass #{pass_n} failed: {e}")
                time.sleep(1.0)
                continue
            elapsed = time.monotonic() - t1
            if not new_here:
                log.info(f"  scanner: pass #{pass_n} — no new PK6 "
                         f"({elapsed:.1f}s). Keep walking.")
                continue
            # Lowest fresh address is the most likely foe slot;
            # later addresses tend to be battle-engine work copies.
            foe_addr = min(new_here)
            log.info(f"  scanner: pass #{pass_n} found {len(new_here)} "
                     f"new PK6(s); foe_base = {foe_addr:#010x}")
            found[0] = foe_addr
            discovered.set()
            return

    t = threading.Thread(target=_scanner, name="FoeScanner", daemon=True)
    t.start()

    diag = ctx.input.diagnose()
    log.info(f"  input driver: {diag}")
    if diag.get("dry_run"):
        log.warning("  input driver is in DRY-RUN — no keystrokes will "
                    "actually reach Azahar. Check pynput install / "
                    "config.yaml input.dry_run.")
    if (diag.get("platform", "").startswith("win")
            and not diag.get("azahar_hwnd")):
        log.warning("  no Azahar window detected on Windows. "
                    "PostMessage path won't work; bot will fall back "
                    "to pynput which requires Azahar to be FOCUSED.")

    # Focus + activate Azahar right before the first key. The launcher
    # focuses ~750 ms after Start, but in encounter mode the user
    # reported the player not moving despite PostMessage taps reaching
    # the queue — classic Qt symptom of the window not being "active".
    # focus_azahar() ends with a synthetic click into the client area
    # which forces Qt to route key events to the GL widget.
    try:
        from ..platform_utils import focus_azahar
        focus_azahar()
    except Exception as e:
        log.warning(f"  focus_azahar failed: {e}")

    walk_iter = _alternating_dirs(movement)
    holds = 0
    paths_seen: dict[str, int] = {}
    while not ctx.should_stop() and not discovered.is_set():
        button, hold_s = next(walk_iter)
        path = ctx.input.tap(button, hold_s=hold_s)
        paths_seen[path] = paths_seen.get(path, 0) + 1
        time.sleep(0.15)                   # brief gap before next hold
        holds += 1
        if holds == 1:
            log.info(f"  walker: hold #1 → {button} for {hold_s:.1f}s "
                     f"via {path}")
        elif holds == 4:
            log.info(f"  walker: 4 holds done. paths: {paths_seen}")
        elif holds % 10 == 0:               # roughly every ~30 s of walking
            log.info(f"  walker: {holds} holds total. paths: {paths_seen}; "
                     f"scanner still sniffing.")
            # Re-focus periodically in case the user clicked away. Cheap
            # no-op when Azahar is already foreground.
            try:
                from ..platform_utils import focus_azahar as _focus
                _focus()
            except Exception:
                pass

    if not discovered.is_set():
        return False                           # ctx requested stop
    foe_addr = found[0]
    if foe_addr is None:
        return False
    ctx.game.offsets.foe_base = foe_addr
    ctx.dashboard.broadcast("offset_scan", state="ok", foe_base=foe_addr)
    _persist_foe_base(foe_addr)
    return True


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
