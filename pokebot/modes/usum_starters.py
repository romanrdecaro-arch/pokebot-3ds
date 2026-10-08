"""
Ultra Sun / Ultra Moon starter soft reset.

The loop, as asked for:

    spam A and tap Left until a starter lands in the party (slot 1)
    shiny      -> stop. Every input, for good.
    not shiny  -> soft reset, and immediately spam A and Left again
    repeat

The presses are one continuous stream from the reset to the starter:
A, Left, A, Left... through the boot logos, the title, CONTINUE and the
cutscene, with no wait anywhere. Left is TAPPED, not held as in the X/Y
starter hunt -- a held direction walks the player in the overworld.

**What waits is the reading, never the pressing.** Nothing is read for
``reload_read_grace`` seconds after L+R+Start (reading during Azahar's
relaunch is what crashed it), and the reset itself is the static
encounter hunt's, so the two hunts cannot drift apart on it.

**Detection.** The save is made BEFORE choosing, so the party is empty
and anything that turns up in it carrying the player's OT is the
starter. Two copies are watched, because neither has been confirmed on
Azahar yet: the save-block party straight after the trainer block, and
PKMN-NTR's live party (``party_live``). Once the starter's address is
known, a check is one 232-byte read at it.

A reset leaves RAM alone, so last attempt's starter can still be
sitting in the slot while the title boots. Every key already judged is
remembered and never judged again.

No species check: whichever starter arrives is evaluated, and a shiny
of any of the three is the win.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from ..games import DEFAULT_OT_NAME
from ..pk6_export import ensure_targets_dir, save_target_pk6
from .observe import (_PARTY_HINT_SPAN, _party_ranges, _scan_owned,
                      broadcast_party, gen7_trainer_ot, read_pk6_at)
from .soft_reset import (_PRE_RESET_QUIET_S, _PRESS_HOLD_FLOOR,
                         _PRESS_HOLD_S, _RELOAD_READ_GRACE_S,
                         _RESET_COOLDOWN_S, _broadcast_candidate,
                         _focus_if_needed, _note_path)
from .static_encounter import _MIN_GRACE_S, _MIN_QUIET_S
from .static_encounter import reset as reset_and_resume

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StarterPlan:
    """How to press, how long to wait, how quiet to be."""
    #: The shared ``press_hold``: it is the launcher's Press speed
    #: field, and this is a starter hunt like the one it was made for.
    press_hold: float = _PRESS_HOLD_S
    #: A presses per Left tap. 1 alternates A, Left, A, Left; 0 never
    #: taps Left at all.
    left_every: int = 1
    left_button: str = "DpadLeft"
    #: Window scans while the starter's address is still unknown. Each
    #: is ~32 RPC round trips with the presses paused, so not every
    #: press.
    scan_gap: float = 0.15
    #: Re-scan both windows this often once the address is known, in
    #: case it moved. Between scans a check is one read, made before
    #: every press.
    sweep_every: float = 1.0
    #: Bounds a stuck attempt. The cutscene has its own pace; at 100%
    #: speed boot plus cutscene is well over a minute.
    receive_timeout: float = 180.0
    #: Stop after this many attempts in a row with no starter: the save
    #: is not where the hunt expects, or nothing is reaching Azahar.
    max_misses: int = 3
    #: Shared with every reset hunt: these were paid for in crashes.
    pre_reset_quiet: float = _PRE_RESET_QUIET_S
    reload_grace: float = _RELOAD_READ_GRACE_S
    reset_cooldown: float = _RESET_COOLDOWN_S

    @classmethod
    def from_config(cls, cfg: dict | None) -> "StarterPlan":
        cfg = cfg or {}
        d = cls()

        def num(key: str, default: float) -> float:
            try:
                return float(cfg.get(key, default))
            except (TypeError, ValueError):
                log.warning(f"  {key}={cfg.get(key)!r} is not a number; "
                            f"using {default}")
                return default

        return cls(
            press_hold=max(_PRESS_HOLD_FLOOR,
                           num("press_hold", d.press_hold)),
            left_every=max(0, int(num("usum_left_every", d.left_every))),
            left_button=str(cfg.get("usum_left_button", d.left_button)
                            or d.left_button),
            scan_gap=max(0.0, num("usum_scan_gap", d.scan_gap)),
            sweep_every=max(0.1, num("usum_sweep_every", d.sweep_every)),
            receive_timeout=max(1.0, num("usum_receive_timeout",
                                         d.receive_timeout)),
            max_misses=max(1, int(num("usum_max_misses", d.max_misses))),
            pre_reset_quiet=max(_MIN_QUIET_S,
                                num("pre_reset_quiet", d.pre_reset_quiet)),
            reload_grace=max(_MIN_GRACE_S,
                             num("reload_read_grace", d.reload_grace)),
            reset_cooldown=max(0.0, num("reset_cooldown",
                                        d.reset_cooldown)),
        )

    def button(self, n: int) -> str:
        """The n-th press of the stream: Left after every left_every A."""
        if self.left_every and n % (self.left_every + 1) == self.left_every:
            return self.left_button
        return "A"


def party_windows(o) -> list[tuple[int, int]]:
    """Where the starter can turn up: the save-block party, then the
    live copy, each as a small window -- never a sweep of Gen 7's
    256 MB heap."""
    out = list(_party_ranges(o.party_base)) if o.party_base else []
    live = getattr(o, "party_live", 0)
    if live:
        out.append((live - _PARTY_HINT_SPAN, live + _PARTY_HINT_SPAN))
    return out


class StarterWatch:
    """Is a starter the hunt has not judged yet in the party?"""

    def __init__(self, ctx, windows, player_ot: str, seen: set,
                 plan: StarterPlan):
        self.ctx = ctx
        self.windows = windows
        self.player_ot = player_ot
        #: Keys already judged. Shared with the hunt.
        self.seen = seen
        self.plan = plan
        self.hot = 0
        self._next_scan = 0.0
        self.slot_reads = 0
        self.scans = 0

    def owned(self):
        """Every record in the windows carrying the player's OT."""
        out = []
        for lo, hi in self.windows:
            out.extend(_scan_owned(self.ctx, lo, hi, self.player_ot))
        return out

    def check(self, now: float):
        """(addr, pkm) of a starter not judged yet, or None."""
        if self.hot:
            self.slot_reads += 1
            pkm = read_pk6_at(self.ctx, self.hot)
            if (pkm is not None and pkm.encryption_key not in self.seen
                    and (pkm.ot_name or "") == self.player_ot):
                return self.hot, pkm
        if now < self._next_scan:
            return None
        self._next_scan = now + (self.plan.sweep_every if self.hot
                                 else self.plan.scan_gap)
        self.scans += 1
        for addr, pkm in self.owned():
            if pkm.encryption_key not in self.seen:
                self.hot = addr
                return addr, pkm
        return None


@dataclass(frozen=True)
class Received:
    addr: int
    pkm: object
    presses: int
    at: float


def mash_until_starter(ctx, plan: StarterPlan, watch: StarterWatch,
                       reads_from: float) -> Received | None:
    """Press the A/Left stream until a new starter is in the party.

    Presses never wait. Reads wait for ``reads_from`` -- the end of the
    reload grace -- and from then on are checked before every press.
    """
    deadline = max(time.monotonic(), reads_from) + plan.receive_timeout
    presses = 0
    while not ctx.should_stop() and time.monotonic() < deadline:
        now = time.monotonic()
        if now >= reads_from:
            hit = watch.check(now)
            if hit is not None:
                return Received(hit[0], hit[1], presses, now)
        _note_path(ctx, ctx.input.tap(plan.button(presses),
                                      hold_s=plan.press_hold))
        presses += 1
    return None


def run(ctx) -> None:
    cfg = ctx.config.get("soft_reset", {}) or {}
    plan = StarterPlan.from_config(cfg)
    ensure_targets_dir()
    # broadcast_party suppresses identical signatures, so a stale one
    # would freeze the launcher strip on the previous run's team.
    if hasattr(ctx, "_party_sig"):
        ctx._party_sig = None

    log.info("Mode: USUM starter soft reset")
    log.info("  Setup: SAVED before choosing, with an EMPTY party.")
    log.info(f"  Spamming A and tapping {plan.left_button} "
             f"({plan.press_hold * 1000:.0f} ms presses) until a starter "
             f"is in your party. Not shiny: L+R+Start and straight back "
             f"to it. SHINY: the bot STOPS ALL INPUT.")
    watch = _start(ctx, cfg, plan)
    if watch is None:
        return

    attempt = misses = 0
    reset_at = reads_from = 0.0      # nothing reset yet: reading is safe
    while not ctx.should_stop():
        attempt += 1
        log.info(f"USUM starter attempt #{attempt}")
        ctx.dashboard.broadcast("soft_reset_attempt", count=attempt)
        got = mash_until_starter(ctx, plan, watch, reads_from)
        if ctx.should_stop():
            return
        if got is None:
            misses += 1
            if _missed(ctx, plan, attempt, misses):
                return
        else:
            misses = 0
            watch.seen.add(got.pkm.encryption_key)
            _report(ctx, attempt, got, reset_at)
            if got.pkm.shiny or (ctx.target and ctx.target.matches(got.pkm)):
                _stop_on(ctx, got, attempt)
                return
        nxt = reset_and_resume(ctx, plan, reset_at)
        if nxt is None:
            return
        reset_at, reads_from = nxt

    log.info(f"USUM starter hunt stopped after {attempt} attempt(s).")


def _start(ctx, cfg: dict, plan: StarterPlan) -> StarterWatch | None:
    """Name the trainer, find the windows, refuse a save with a party."""
    windows = party_windows(ctx.game.offsets)
    if not windows:
        log.error("party_base not configured for this game; aborting.")
        ctx.request_stop("party_base not configured")
        return None
    _focus_if_needed(ctx)
    configured = cfg.get("trainer_name", DEFAULT_OT_NAME)
    # The game's own name is the truth; config.yaml's is the fallback.
    player_ot = gen7_trainer_ot(ctx) or configured
    if player_ot != configured:
        log.info(f"  using the game's trainer name {player_ot!r} "
                 f"(config.yaml says {configured!r})")
    watch = StarterWatch(ctx, windows, player_ot, set(), plan)
    log.info("  watching for the starter in " + ", ".join(
        f"{lo:#010x}-{hi:#010x}" for lo, hi in windows))
    already = watch.owned()
    if already:
        log.error("Your party already has a Pokémon. Save BEFORE choosing "
                  "a starter, with an empty party, or reset to that save "
                  "and start the bot again — otherwise it cannot tell a "
                  "new starter from the one already there. Stopping.")
        ctx.dashboard.broadcast("read_failure",
                                reason="party not empty at start")
        ctx.request_stop("party not empty at start")
        return None
    return watch


def _report(ctx, attempt: int, got: Received, reset_at: float) -> None:
    pkm = got.pkm
    _broadcast_candidate(ctx, attempt, pkm)
    broadcast_party(ctx, [pkm])
    after = (f"{got.at - reset_at:.1f}s after the reset, " if reset_at
             else "")
    log.info(f"  starter: #{pkm.species} {pkm.nickname or ''} "
             f"{'★SHINY★ ' if pkm.shiny else ''}nature={pkm.nature} "
             f"IVs={pkm.ivs} PID={pkm.pid:08X} PSV={pkm.psv} "
             f"TSV={pkm.tsv} — {after}{got.presses} presses, "
             f"@{got.addr:#010x}")


def _missed(ctx, plan: StarterPlan, attempt: int, misses: int) -> bool:
    """Say so; True when it is time to stop rather than reset again."""
    ctx.dashboard.broadcast("read_failure", attempt=attempt,
                            reason="no starter received")
    if misses >= plan.max_misses:
        log.error(f"  no starter in {misses} attempts in a row. Either the "
                  f"save is not just before the choice, or Azahar is not "
                  f"receiving input (scripts/test_input.py). Stopping.")
        ctx.request_stop("no starter received")
        return True
    log.warning(f"  attempt {attempt}: no starter in "
                f"{plan.receive_timeout:.0f}s. Resetting.")
    return False


def _stop_on(ctx, got: Received, attempt: int) -> None:
    """Stop, then say so. Nothing in here sends input."""
    pkm = got.pkm
    # The stop is the instruction, so it goes first.
    ctx.request_stop("shiny usum starter")
    # A read, not an input: the record goes to targets/ as a .pk7.
    save_target_pk6(ctx, got.addr, pkm, "shiny" if pkm.shiny else "starter")
    reason = ("shiny" if pkm.shiny else ctx.target.describe(pkm))
    bar = "*" * 30
    for line in (
        bar,
        f"  {reason.upper()} STARTER #{pkm.species} — attempt #{attempt}",
        "  Bot STOPPED — no further input. It is in your party:",
        "  do NOT reset. Decline the nickname if asked, and save as",
        "  soon as the game lets you.",
        bar,
    ):
        log.info(line)
    ctx.dashboard.broadcast(
        "target_hit", attempt=attempt, count=attempt, reason=reason,
        species=pkm.species, shiny=pkm.shiny, nature=pkm.nature,
        ivs=pkm.ivs)
