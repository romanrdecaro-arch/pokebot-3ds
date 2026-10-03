"""
Noibat shaking-spot hunt (X/Y, Terminus Cave).

    reset → hold a direction into the shaking spot → read the wild →
    shiny Noibat stops and catches; anything else resets.

**This mode throws away shinies, on purpose.** Every other hunt here
treats a shiny as the win whatever else was configured — the starter
hunt carries a comment about never soft-resetting a shiny Fennekin
over a species gate, and the gift hunt repeats it. This one was asked
for the other way round: only a shiny *Noibat* is wanted, and a shiny
of any other species in the cave is reset over like any other miss.

That is irreversible, so a discarded shiny is exported to ``targets/``
first. The file is a pre-capture record — no OT, ball or met data, so
PKHeX will not accept it as legal — but it is the only evidence the
encounter ever happened, and writing it costs one read.

**Player setup (one-time, manual):**

1. Stand next to the shaking spot, facing it, close enough that
   holding one direction walks into it.
2. **SAVE there.** Every attempt returns to that save.
3. Pick which way the spot is in the launcher: up, right, down, left.

The direction is HELD rather than tapped, because the spot may be
more than one tile away and a tap moves exactly one.

Noibat is species **714**. That number is from memory rather than from
any table in this repo, so it is configurable: if the hunt reports
finding the wrong thing, ``noibat.species`` is a config edit, not a
code change.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from ..games import DEFAULT_OT_NAME
from ..pk6_export import ensure_targets_dir, save_target_pk6
from . import catch
from .game_reset import ResetPlan, soft_reset_and_wait
from .observe import (broadcast_party, get_party, read_pk6_at,
                      scan_nonparty)

log = logging.getLogger(__name__)

#: National dex number. See the module docstring: configurable because
#: this repo has no Gen 6 species table to check it against.
NOIBAT = 714

_DIRECTIONS = {
    "up": "DpadUp",
    "down": "DpadDown",
    "left": "DpadLeft",
    "right": "DpadRight",
}


def direction_button(word: str | None) -> str:
    """Map a dropdown / CLI / config direction onto a d-pad button.

    Lenient on purpose: this value arrives from three places and a
    typo should not be the difference between a hunt and a crash.
    """
    raw = str(word or "").strip().lower()
    if raw in _DIRECTIONS:
        return _DIRECTIONS[raw]
    for key, btn in _DIRECTIONS.items():
        if raw == btn.lower():
            return btn
    if raw:
        log.warning(f"  direction {word!r} is not up/down/left/right; "
                    f"using up")
    return "DpadUp"


@dataclass(frozen=True)
class NoibatPlan:
    """How to walk into the spot and how long to wait for the wild."""
    button: str = "DpadUp"
    species: int = NOIBAT

    #: How long to keep walking before giving up on this attempt. The
    #: spot is a few tiles away at most; a long silence means the save
    #: is not where the hunt expects, or the spot did not respawn.
    encounter_timeout: float = 20.0
    #: How often to look while walking. One 232-byte read at the last
    #: known wild address (see foe_watch), so it can be tight.
    poll_gap: float = 0.03
    #: Let the battle finish drawing before reading the wild, so the
    #: record is fully written.
    settle: float = 0.4

    @classmethod
    def from_config(cls, cfg: dict | None) -> "NoibatPlan":
        cfg = cfg or {}
        d = cls()

        def num(key: str, default: float) -> float:
            try:
                return float(cfg.get(key, default))
            except (TypeError, ValueError):
                log.warning(f"  {key}={cfg.get(key)!r} is not a number; "
                            f"using {default}")
                return default

        try:
            species = int(cfg.get("species", d.species))
        except (TypeError, ValueError):
            log.warning(f"  species={cfg.get('species')!r} is not a "
                        f"number; using {d.species}")
            species = d.species

        return cls(
            button=direction_button(cfg.get("direction", d.button)),
            species=species,
            encounter_timeout=num("encounter_timeout",
                                  d.encounter_timeout),
            poll_gap=max(0.005, num("poll_gap", d.poll_gap)),
            settle=num("settle", d.settle),
        )


def is_hit(target, pkm, species: int) -> bool:
    """Shiny AND the right species. Both, always.

    Deliberately NOT the usual ``shiny or target.matches``. The ask
    for this mode was that a shiny of any other species is reset over,
    so shiny alone cannot be enough -- and species alone obviously is
    not either.

    A configured filter narrows further on top (IVs, nature). An empty
    filter means "no preference" rather than "match nothing".
    """
    if not pkm.shiny or pkm.species != species:
        return False
    if target is not None and getattr(target, "rules", None):
        try:
            return bool(target.matches(pkm))
        except Exception as exc:
            log.warning(f"  target filter raised ({exc}); "
                        f"treating the shiny as a hit")
            return True
    return True


def worth_saving(pkm, species: int) -> bool:
    """Should this miss be exported before it is reset away?

    Only a shiny. Resetting over one is irreversible and the .pk6 is
    the only record it existed; exporting every ordinary Zubat would
    fill targets/ with thousands of files nobody wants.
    """
    return bool(pkm.shiny and pkm.species != species)


def _describe(pkm) -> str:
    return (f"#{pkm.species} {pkm.nickname or ''} "
            f"{'★SHINY★ ' if pkm.shiny else ''}"
            f"nature={pkm.nature} IVs={pkm.ivs} "
            f"PID={pkm.pid:08X} PSV={pkm.psv} TSV={pkm.tsv}").strip()


def run(ctx) -> None:
    cfg = ctx.config.get("noibat") or {}
    plan = NoibatPlan.from_config(cfg)
    sr_cfg = ctx.config.get("soft_reset", {}) or {}
    player_ot = sr_cfg.get("trainer_name", DEFAULT_OT_NAME)
    reset_plan = ResetPlan.from_config(sr_cfg)
    catch_plan = catch.CatchPlan.from_config(
        ctx.config.get("random_encounters") or {})

    o = ctx.game.offsets
    foe_base = o.foe_base
    foe_len = getattr(o, "foe_scan_len", 0) or 0x20000
    party_base = o.party_base
    party_stride = o.party_stride or 484

    ensure_targets_dir()
    if hasattr(ctx, "_party_sig"):
        ctx._party_sig = None

    log.info("Mode: noibat — shaking-spot soft-reset hunt")
    log.info(f"  holding {plan.button} into the spot, up to "
             f"{plan.encounter_timeout:.0f}s per attempt")
    log.info(f"  STOPS ONLY on a shiny #{plan.species}. A shiny of any "
             f"other species is reset over (its .pk6 is saved first).")
    if ctx.target and getattr(ctx.target, "rules", None):
        log.info("  ...and the configured target filter must also match.")
    log.info("  Setup: SAVED standing next to the spot, facing it.")

    if not foe_base:
        log.error("foe_base not configured (X/Y: 0x08800000). Run Debug.")
        ctx.request_stop("foe_base not configured")
        return

    diag = ctx.input.diagnose()
    log.info(f"  input driver: {diag}")
    if diag.get("dry_run"):
        log.warning("  input DRY-RUN — nothing will be pressed.")

    def party_keys() -> set:
        party = get_party(ctx, party_base, party_stride, player_ot)
        if party:
            broadcast_party(ctx, party)
        return {p.encryption_key for p in (party or ())}

    keys = party_keys()

    def wilds(seen: set):
        """Non-party records in the foe window the hunt has not seen."""
        return [(a, p) for a, p in
                scan_nonparty(ctx, foe_base, foe_len, keys)
                if p.encryption_key not in seen]

    # Whatever is already in the foe window is pre-bot: a stale wild
    # from before the hunt started must not be read as this attempt's
    # encounter, or attempt one evaluates a Pokemon nobody met.
    seen = {p.encryption_key for _, p in
            scan_nonparty(ctx, foe_base, foe_len, keys)}
    log.info(f"  baseline: {len(seen)} pre-existing record(s) ignored.")

    hot = {"addr": 0}
    attempt = 0
    shinies_passed = 0

    def walk_until_encounter():
        """Hold the direction until a new wild appears. Returns it."""
        deadline = time.monotonic() + plan.encounter_timeout
        if not ctx.input.hold(plan.button):
            log.warning(f"  could not hold {plan.button}; the player may "
                        f"not move.")
        try:
            while not ctx.should_stop() and time.monotonic() < deadline:
                # Cheap path first: one read where the last wild was.
                if hot["addr"]:
                    try:
                        p = read_pk6_at(ctx, hot["addr"])
                        if (p is not None
                                and p.encryption_key not in keys
                                and p.encryption_key not in seen):
                            return hot["addr"], p
                    except Exception as exc:
                        log.debug(f"  hot read failed: {exc}")
                found = wilds(seen)
                if found:
                    hot["addr"] = found[0][0]
                    return found[0]
                ctx._stop_evt.wait(plan.poll_gap)
        finally:
            # Always. A latched direction outlives this process -- the
            # player takes back a game that is still walking into a
            # wall, and nothing will ever release it.
            ctx.input.release(plan.button)
        return None

    while not ctx.should_stop():
        attempt += 1
        log.info(f"Noibat attempt #{attempt}")
        ctx.dashboard.broadcast("soft_reset_attempt", count=attempt)

        hit = walk_until_encounter()
        if ctx.should_stop():
            return
        if hit is None:
            log.warning(f"  no encounter in {plan.encounter_timeout:.0f}s "
                        f"of holding {plan.button}. Check the save is "
                        f"beside the spot and the direction is right. "
                        f"Resetting.")
            ctx.dashboard.broadcast("read_failure", attempt=attempt,
                                    reason="no encounter while walking")
            if not _reset(ctx, reset_plan, party_keys, attempt):
                return
            seen = {p.encryption_key for _, p in
                    scan_nonparty(ctx, foe_base, foe_len, keys)}
            hot["addr"] = 0
            continue

        addr, pkm = hit
        seen.add(pkm.encryption_key)
        # Let the battle finish drawing before anything is decided on
        # it -- a record read mid-write can parse but be wrong.
        ctx._stop_evt.wait(plan.settle)
        log.info(f"  encounter: {_describe(pkm)}")
        ctx.dashboard.broadcast(
            "candidate", attempt=attempt, species=pkm.species,
            nickname=pkm.nickname, shiny=pkm.shiny, nature=pkm.nature,
            gender=pkm.gender, ivs=pkm.ivs, pid=pkm.pid, tsv=pkm.tsv,
            psv=pkm.psv, ability_id=pkm.ability_id,
            ability_num=pkm.ability_num,
            level=(pkm.party or {}).get("level"), moves=pkm.moves)

        if is_hit(ctx.target, pkm, plan.species):
            _celebrate(ctx, pkm, addr, attempt)
            result = catch.catch_wild(
                ctx, catch_plan, pkm.encryption_key, party_keys)
            bar = "*" * 30
            for line in (
                bar,
                f"  {'CAUGHT' if result.caught else 'THROW UNCONFIRMED'}"
                f" — {result.detail}",
                "  Bot STOPPED. SAVE THE GAME before anything else: "
                "this hunt resets between attempts, and a reset takes "
                "an unsaved catch back.",
                bar,
            ):
                log.warning(line)
            ctx.dashboard.broadcast(
                "target_caught", species=pkm.species, shiny=True,
                count=attempt, caught=1 if result.caught else 0)
            ctx.request_stop("shiny noibat")
            return

        if worth_saving(pkm, plan.species):
            # Irreversible in a moment. Keep the record.
            shinies_passed += 1
            path = save_target_pk6(ctx, addr, pkm, "shiny-not-noibat")
            bar = "!" * 30
            for line in (
                bar,
                f"  SHINY #{pkm.species} — but this hunt only wants "
                f"#{plan.species}, so it is being RESET OVER.",
                f"  Saved anyway: {path or 'export failed'}",
                f"  ({shinies_passed} shiny miss(es) so far this run.)",
                bar,
            ):
                log.warning(line)
            ctx.dashboard.broadcast(
                "target_hit", attempt=attempt, count=attempt,
                reason=f"shiny #{pkm.species}, not #{plan.species} "
                       f"— reset over",
                species=pkm.species, shiny=True, nature=pkm.nature,
                ivs=pkm.ivs)

        if not _reset(ctx, reset_plan, party_keys, attempt):
            return
        seen = {p.encryption_key for _, p in
                scan_nonparty(ctx, foe_base, foe_len, keys)}
        hot["addr"] = 0

    log.info(f"Noibat hunt stopped after {attempt} attempt(s)"
             + (f", {shinies_passed} shiny miss(es) passed over."
                if shinies_passed else "."))


def _celebrate(ctx, pkm, addr: int, attempt: int) -> None:
    save_target_pk6(ctx, addr, pkm, "shiny")
    bar = "*" * 30
    for line in (
        bar,
        f"  SHINY NOIBAT — attempt #{attempt}",
        f"  {_describe(pkm)}",
        "  Catching it now — do not touch the controls.",
        bar,
    ):
        log.info(line)
    ctx.dashboard.broadcast(
        "target_hit", attempt=attempt, count=attempt,
        reason="shiny Noibat", species=pkm.species, shiny=True,
        nature=pkm.nature, ivs=pkm.ivs)


def _reset(ctx, reset_plan, party_keys, attempt: int) -> bool:
    """Relaunch and wait for the save. False means stop the hunt."""
    ok = soft_reset_and_wait(ctx, lambda: bool(party_keys()), reset_plan)
    if ctx.should_stop():
        return False
    if not ok:
        log.error("  the save never came back after the reset. Check "
                  "Azahar is focused and receiving input "
                  "(scripts/test_input.py). Stopping.")
        ctx.dashboard.broadcast("read_failure", attempt=attempt,
                                reason="soft reset did not return")
        ctx.request_stop("soft reset did not return")
        return False
    return True
