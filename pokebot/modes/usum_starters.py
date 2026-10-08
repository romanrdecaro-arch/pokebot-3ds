"""
Ultra Sun / Ultra Moon starter soft reset.

The loop, as asked for:

    spam A and tap Left until a starter is in hand -- read at the
      nickname screen
    shiny      -> stop. Every input, for good.
    not shiny  -> soft reset, and immediately spam A and Left again
    repeat

What the game actually does, found by driving it live (Ultra Moon,
2026-10-08):

* The save sits in Route 1's tall grass. Nothing happens until the
  player steps LEFT into it -- which is what the Left taps are for.
* **The D-pad does not walk in USUM.** Held for 0.6 s it did nothing;
  the Circle Pad stepped into the grass first time. So Left is
  CircleLeft, held a little longer than an A press.
* After "You chose Rowlet!" comes "Would you like to give Rowlet a
  nickname?" (Yes/No, cursor on Yes); one more A opens the keyboard,
  where every A types a letter. The starter is only "added to your
  party" once a name is confirmed.
* From the fade into that question, the starter waits at
  ``received_slot`` (0x329C2C74): the same address over every reset
  tried, written once, empty after a reload. That is the read the hunt
  stops on -- in the live runs, every starter was judged with the
  nickname question on screen and unanswered. The live party copy is
  watched too, as a fallback.

The presses are one continuous stream from the reset to the starter,
through the boot logos, the title and the cutscene, with no wait.
Reading waits for ``reload_read_grace`` after L+R+Start (reading during
Azahar's relaunch is what crashed it); the reset itself is the static
encounter hunt's, so the two cannot drift apart on it.

Every key already judged is remembered and never judged again, so a
starter a reset left in RAM cannot come back as a new one.

No species check: whichever starter arrives is judged, and a shiny of
any of the three is the win.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from ..games import DEFAULT_OT_NAME
from ..pk6_export import ensure_targets_dir, save_target_pk6
from .observe import (_PARTY_HINT_SPAN, _scan_owned, broadcast_party,
                      gen7_trainer_ot, read_pk6_at)
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
    #: A presses. The shared ``press_hold``: it is the launcher's Press
    #: speed field, and this is a starter hunt like the one it was
    #: made for.
    press_hold: float = _PRESS_HOLD_S
    #: A presses per Left tap. 1 alternates A, Left, A, Left; 0 never
    #: taps Left -- and then the event never starts.
    left_every: int = 1
    #: The Circle Pad. The D-pad does not walk in USUM.
    left_button: str = "CircleLeft"
    #: Longer than an A press, so a tap is a step and not just a turn.
    left_hold: float = 0.1
    #: Window scans while the starter's address is still unknown. Each
    #: is ~32 RPC round trips with the presses paused, so not every
    #: press.
    scan_gap: float = 0.15
    #: Re-scan the windows this often once the address is known, in
    #: case it moved. Between scans a check is one read, made before
    #: every press.
    sweep_every: float = 1.0
    #: Bounds a stuck attempt. Live at ~900% speed the starter came
    #: ~20 s after the reset; at 100% that is a few minutes.
    receive_timeout: float = 180.0
    #: Stop after this many attempts in a row with no starter.
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
            left_hold=max(_PRESS_HOLD_FLOOR,
                          num("usum_left_hold", d.left_hold)),
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

    def hold(self, button: str) -> float:
        return self.left_hold if button == self.left_button else self.press_hold


def watch_windows(o) -> list[tuple[int, int]]:
    """Where the starter can turn up: the nickname-screen slot first,
    then the live party copy -- each a small window, never a sweep of
    Gen 7's 256 MB heap. The save-block party is not watched: it did
    not change until the game was saved."""
    out = []
    for addr in (getattr(o, "received_slot", 0), getattr(o, "party_live", 0)):
        if addr:
            out.append((addr - _PARTY_HINT_SPAN, addr + _PARTY_HINT_SPAN))
    return out


class StarterWatch:
    """Is a starter the hunt has not judged yet in hand?"""

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
    """Press the A/Left stream until a new starter is in hand.

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
        btn = plan.button(presses)
        _note_path(ctx, ctx.input.tap(btn, hold_s=plan.hold(btn)))
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
    log.info("  Setup: SAVED in the tall grass before choosing, with an "
             "EMPTY party.")
    log.info(f"  Spamming A and tapping {plan.left_button} (A "
             f"{plan.press_hold * 1000:.0f} ms, Left "
             f"{plan.left_hold * 1000:.0f} ms) until a starter is in "
             f"hand -- read at the nickname screen. Not shiny: L+R+Start "
             f"and straight back to it. SHINY: the bot STOPS ALL INPUT.")
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
    """Name the trainer and find the windows.

    A starter already in hand when the bot starts is not refused: it is
    the game's real state, so attempt 1 simply judges it.
    """
    windows = watch_windows(ctx.game.offsets)
    if not windows:
        log.error("This game has no starter slot or live party address "
                  "configured; aborting.")
        ctx.request_stop("no starter addresses")
        return None
    _focus_if_needed(ctx)
    configured = cfg.get("trainer_name", DEFAULT_OT_NAME)
    # The game's own name is the truth; config.yaml's is the fallback.
    player_ot = gen7_trainer_ot(ctx) or configured
    if player_ot != configured:
        log.info(f"  using the game's trainer name {player_ot!r} "
                 f"(config.yaml says {configured!r})")
    log.info("  watching for the starter in " + ", ".join(
        f"{lo:#010x}-{hi:#010x}" for lo, hi in windows))
    return StarterWatch(ctx, windows, player_ot, set(), plan)


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
        log.error(f"  no new starter in {misses} attempts in a row. Either "
                  f"the save is not in the grass before choosing (a save "
                  f"made AFTER choosing brings the same starter back every "
                  f"time), or Azahar is not receiving input "
                  f"(scripts/test_input.py). Stopping.")
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
        "  Bot STOPPED — no further input. It is waiting at \"Would you",
        "  like to give it a nickname?\": answer it yourself, and do NOT",
        "  reset. Save as soon as the game lets you.",
        bar,
    ):
        log.info(line)
    ctx.dashboard.broadcast(
        "target_hit", attempt=attempt, count=attempt, reason=reason,
        species=pkm.species, shiny=pkm.shiny, nature=pkm.nature,
        ivs=pkm.ivs)
