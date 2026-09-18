"""
Gift Pokémon soft-reset hunt.

    reset → mash A → a new Pokémon lands in the party → evaluate it →
    shiny stops, anything else resets.

Mechanically the starter hunt, with one difference that changes almost
everything about how it detects: the starter hunt is saved with an
EMPTY party, so "is there anything in the party?" is enough. A gift
hunt is saved mid-game, with a team already there — so the question
becomes "is there anything in the party that was not there when we
saved?", answered by encryption key. Every generated Pokémon gets a
unique one, and the saved team's keys come back unchanged on every
reload, so anything else is the gift by construction.

That also means this mode does not care WHICH gift. Lapras from the
Route 12 Hiker, the Lumiose bike-shop Eevee, a fossil revival, the
Kanto starter at Lumiose Station — whatever the dialog hands over is
what gets evaluated. Nothing here is species-specific.

**In-game trades are the exception, and they will NOT work.** The party
is located by scanning for records whose OT is the player, and a traded
Pokémon keeps the other trainer's OT — so it is invisible to every read
this mode makes. It is not that the hunt evaluates it wrongly; it never
sees it, and times out saying nothing arrived.

**Player setup (one-time, manual):**

1. **Leave at least one party slot open.** A full party sends the gift
   straight to a PC box, where no party read will ever see it, and the
   hunt would sit there pressing A at nothing.
2. Stand in front of the NPC / item that gives the Pokémon, facing it,
   with the dialog not yet started.
3. **SAVE there.** Every attempt returns to that save.

**Why A is safe to mash.** In X/Y the nickname prompt comes AFTER the
Pokémon is added to the party, and A at that prompt opens the naming
keyboard — which nothing in this bot knows how to get back out of. The
presses are stopped by DETECTION rather than by a count, so they end
within one poll of the gift landing, before the prompt is reachable.
That is why ``detect_every`` is a correctness setting here and not a
performance one.

When it stops on a shiny, the game is left at (or just before) that
prompt. Decline the nickname with B and SAVE — the catch is not yours
until the game is saved.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ..games import DEFAULT_OT_NAME
from ..pk6_export import ensure_targets_dir, save_target_pk6
from .game_reset import ResetPlan, soft_reset_and_wait
from .observe import broadcast_party, get_party
from .soft_reset import (_DETECT_EVERY_S, _PRESS_GAP_S, _PRESS_HOLD_FLOOR,
                         _PRESS_HOLD_S, _PRE_RESET_QUIET_S,
                         _RELOAD_READ_GRACE_S, _RESET_COOLDOWN_S,
                         _focus_if_needed, _reset_until_party_empty,
                         _spam_a_until_received)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GiftPlan:
    """How hard to press, and how long to wait before giving up."""
    #: A presses. 30 ms hold with no gap is ~33/s, which is as fast as
    #: Azahar reliably registers: below about 10 ms it can see the key
    #: go down and up inside one polled frame and score no press at
    #: all, so "as fast as possible" has a floor rather than a zero.
    press_hold: float = _PRESS_HOLD_S
    press_gap: float = _PRESS_GAP_S
    #: How often the party is checked while mashing. This BOUNDS the
    #: overshoot past the moment the gift lands -- at 0.15 s and 33
    #: presses/s, about five presses -- which is what keeps the mash
    #: out of the nickname keyboard.
    detect_every: float = _DETECT_EVERY_S
    #: Nothing is held -- and this deliberately does NOT read the
    #: shared ``hold_button``.
    #:
    #: That key belongs to the STARTER hunt, where a held direction
    #: steers the cursor along the table, and config.yaml ships it as
    #: DpadLeft. This mode reads the same soft_reset section, so it
    #: inherited it: a direction held through a gift's Yes/No prompt
    #: moves the cursor onto "No", the gift is declined, and the hunt
    #: then presses A forever at a Pokemon it just refused -- failing
    #: in the one way that looks exactly like "the save is wrong".
    #: Its own key, so the starter's setting cannot reach it.
    hold_button: str = ""

    #: Reset once before taking the baseline, so it is the party as
    #: SAVED rather than whatever happened to be on screen when the
    #: bot was started. Costs one relaunch; buys a baseline that
    #: cannot silently contain the gift.
    initial_reset: bool = True

    #: How many cheap polls between broad sweeps. The cheap poll only
    #: sees the cached window around the party as it was before the
    #: gift existed; the sweep re-locates and sees everything. 12
    #: polls at the default detect_every is a sweep roughly every two
    #: seconds.
    sweep_every: int = 12

    #: The dialog has unskippable animations, so this bounds a STUCK
    #: attempt rather than pacing a working one.
    receive_timeout: float = 180.0
    reset_timeout: float = 30.0

    #: Silences around the relaunch. Reading Azahar's memory while it
    #: tears the title down is what has been crashing it; these come
    #: from soft_reset rather than being restated, because two copies
    #: of a number bought in crashes drift.
    pre_reset_quiet: float = _PRE_RESET_QUIET_S
    reload_grace: float = _RELOAD_READ_GRACE_S
    reset_cooldown: float = _RESET_COOLDOWN_S

    @classmethod
    def from_config(cls, cfg: dict | None) -> "GiftPlan":
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
            press_hold=max(_PRESS_HOLD_FLOOR, num("press_hold",
                                                  d.press_hold)),
            press_gap=max(0.0, num("press_interval", d.press_gap)),
            detect_every=max(0.02, num("detect_every", d.detect_every)),
            hold_button=str(cfg.get("gift_hold_button",
                                    d.hold_button) or ""),
            initial_reset=bool(cfg.get("initial_reset", d.initial_reset)),
            sweep_every=max(1, int(num("sweep_every", d.sweep_every))),
            receive_timeout=num("receive_timeout", d.receive_timeout),
            reset_timeout=num("reset_timeout", d.reset_timeout),
            pre_reset_quiet=num("pre_reset_quiet", d.pre_reset_quiet),
            reload_grace=num("reload_read_grace", d.reload_grace),
            reset_cooldown=num("reset_cooldown", d.reset_cooldown),
        )


def new_arrivals(party, baseline_keys) -> list:
    """Party members whose key was not there when we saved.

    By construction that is the gift: the saved team's keys reload
    unchanged, and every generated Pokémon brings a brand-new one.
    """
    return [p for p in (party or ())
            if p.encryption_key not in baseline_keys]


def is_target(target, pkm) -> bool:
    """Shiny, or whatever the configured filter asks for.

    Shiny FIRST and always, whatever else is configured. The starter
    hunt learned this the hard way: a species gate that ran ahead of
    the target check would soft-reset a shiny of the wrong species --
    throwing away the one outcome the hunt exists for, over a
    preference PKHeX fixes in ten seconds.
    """
    return bool(pkm.shiny or (target and target.matches(pkm)))


def _describe(target, pkm) -> str:
    if pkm.shiny:
        return "shiny"
    try:
        return target.describe(pkm) if target else "target"
    except Exception:
        return "target"


def run(ctx) -> None:
    cfg = ctx.config.get("soft_reset", {}) or {}
    plan = GiftPlan.from_config(cfg)
    player_ot = cfg.get("trainer_name", DEFAULT_OT_NAME)
    party_base = ctx.game.offsets.party_base
    party_stride = ctx.game.offsets.party_stride or 484

    ensure_targets_dir()
    # Same reason soft_reset.run does it: broadcast_party suppresses
    # identical signatures, so a stale one freezes the launcher strip.
    if hasattr(ctx, "_party_sig"):
        ctx._party_sig = None

    log.info("Mode: gifts — soft-reset hunt for any gift Pokémon")
    log.info("  Setup: at least one PARTY SLOT OPEN, standing in front "
             "of the giver, game SAVED there.")
    log.info(f"  A presses: {plan.press_hold * 1000:.0f}ms hold"
             + (f" + {plan.press_gap * 1000:.0f}ms gap"
                if plan.press_gap else "")
             + f"  (~{1 / max(plan.press_hold + plan.press_gap, 1e-9):.0f}"
               f"/s), stopped by detection every "
               f"{plan.detect_every:.2f}s")
    log.info("  Stopping on: " + ("the configured target filter"
                                  if (ctx.target and ctx.target.rules)
                                  else "SHINY"))
    _focus_if_needed(ctx)

    # Two reads, because two different questions are being asked and
    # one read cannot answer both.
    #
    # get_party(contiguous=True) keeps only the save-block party
    # cluster -- the actual six slots. get_party(contiguous=False)
    # keeps every owned PK6 in the window, which is the party AND the
    # PC boxes, and exists so a gift written to a live buffer off the
    # save-block grid is not hidden.
    #
    # Using the broad one to COUNT party slots is what shipped, and it
    # told a player with 80 boxed Pokemon that their party was full
    # (82/6) before pressing a single button. Boxes are not party
    # slots.
    def relocate():
        """Drop the cached window so the next read re-finds the party.

        Deliberately NOT done per read. get_party caches a tight window
        around the owned cluster and re-scans only that; clearing it
        sends the next read through the full 15 MB relocation scan
        instead, and at 1 KB per RPC round trip that is thousands of
        them. Once per attempt is fine. Every 150 ms, while mashing A,
        is not -- it would make the poll slower than the thing it is
        polling for.

        A relaunch moves everything, but get_party already notices
        that on its own (the cached window comes back empty and it
        re-locates). This is belt and braces at a price worth paying
        once.
        """
        if hasattr(ctx, "_party_win"):
            ctx._party_win = None

    def read_team():
        """The six slots. For counting them, and for the strip."""
        return get_party(ctx, party_base, party_stride, player_ot,
                         contiguous=True) or []

    def read_all():
        """Everything owned in the window -- party and boxes.

        Only ever used to ask "is this key one I had already seen?",
        which is a question boxed Pokemon should be included in: they
        are in the baseline, so they are never mistaken for the gift.
        """
        return get_party(ctx, party_base, party_stride, player_ot,
                         contiguous=False) or []

    def check_party(party) -> bool:
        """Refuse a save this hunt cannot work from, before pressing."""
        if not party:
            log.error("No party found. This mode tells the gift apart "
                      "from your team by comparing against the party as "
                      "saved, so it has to read that team first. Check "
                      "soft_reset.trainer_name matches your in-game OT, "
                      "and that party_base is configured (run Debug if "
                      "not).")
            ctx.dashboard.broadcast("read_failure",
                                    reason="no party at start")
            ctx.request_stop("no party at start")
            return False
        if len(party) >= 6:
            log.error(f"Your party is FULL ({len(party)}/6). A gift given "
                      f"to a full party goes straight to a PC box, where "
                      f"no party read can see it — the hunt would press A "
                      f"at nothing until it timed out. Free a slot, save "
                      f"again, and restart.")
            ctx.dashboard.broadcast("read_failure", reason="party full")
            ctx.request_stop("party full")
            return False
        return True

    relocate()
    if not check_party(read_team()):
        return

    # Reset once before taking the baseline.
    #
    # The baseline has to be the party AS SAVED, and the party on
    # screen right now is only that if the bot happened to be started
    # in a clean state. Start it with the gift already taken, or a
    # dialog half-open, and the baseline silently absorbs the gift --
    # after which nothing is ever "new" and the hunt presses A forever
    # at a gift it cannot see. There is no read that tells those apart
    # (a taken gift looks exactly like a sixth party member), so rather
    # than guess, put the game in the state the baseline assumes.
    if plan.initial_reset:
        log.info("  resetting once before starting, so the baseline is "
                 "the party as SAVED rather than whatever is on screen.")
        reset_plan = ResetPlan(quiet=plan.pre_reset_quiet,
                               grace=plan.reload_grace,
                               boot_timeout=plan.receive_timeout,
                               press_hold=plan.press_hold,
                               press_gap=plan.press_gap,
                               read_every=plan.detect_every)
        if not soft_reset_and_wait(ctx, lambda: bool(read_team()),
                                   reset_plan, last_reset=None):
            if ctx.should_stop():
                return
            log.error("  the save never came back after the opening "
                      "reset. Check Azahar is focused and receiving "
                      "input (scripts/test_input.py).")
            ctx.dashboard.broadcast("read_failure",
                                    reason="opening reset did not return")
            ctx.request_stop("opening reset did not return")
            return

    relocate()
    baseline_team = read_team()
    if not check_party(baseline_team):
        return

    broadcast_party(ctx, baseline_team)
    # Keys come from the BROAD read so every boxed Pokemon is in the
    # baseline too. Leave them out and all of them read as arrivals
    # the first time the gift check runs.
    baseline_keys = {p.encryption_key for p in read_all()}
    log.info(f"  party as saved: {len(baseline_team)}/6 — "
             + ", ".join(f"#{p.species}" for p in baseline_team)
             + f" ({len(baseline_keys)} owned PK6 including boxes). "
             + "Anything else that appears is the gift.")

    # Polling costs, and the two ways of paying are both wrong on
    # their own.
    #
    # Clearing the cached window every poll is correct -- it finds a
    # gift wherever it landed -- but sends each read through the full
    # 15 MB relocation scan, thousands of 1 KB round trips, on a path
    # that runs every detect_every while A is being mashed.
    #
    # Never clearing it is cheap but blind: the window is anchored on
    # the owned cluster as it was BEFORE the gift existed, so a gift
    # written outside it is missed for the whole attempt, which looks
    # exactly like "nothing ever arrived".
    #
    # So: cheap poll against the cached window, and a broad sweep
    # every full_every polls to catch what the window cannot see. The
    # blind spot is then bounded by how often the sweep runs rather
    # than lasting the attempt.
    sweep_every = max(1, int(plan.sweep_every))
    since_sweep = {"n": 0}

    def gift_present() -> bool:
        if new_arrivals(read_all(), baseline_keys):
            return True
        since_sweep["n"] += 1
        if since_sweep["n"] < sweep_every:
            return False
        since_sweep["n"] = 0
        relocate()
        return bool(new_arrivals(read_all(), baseline_keys))

    last_reset: list = [0.0]

    def reset_or_stop() -> bool:
        """Reset, and CONFIRM the gift left the party.

        Unconfirmed is not a cosmetic worry. If L+R+Start does not
        land, last attempt's gift is still sitting there -- so the next
        attempt reads it as a fresh arrival, evaluates the same
        Pokemon again, and the hunt rolls forever on one result while
        looking perfectly busy.
        """
        if _reset_until_party_empty(
                ctx, gift_present, plan.reset_timeout, plan.press_hold,
                plan.press_gap, plan.reset_cooldown, last_reset,
                plan.reload_grace, plan.detect_every,
                plan.pre_reset_quiet):
            return True
        if ctx.should_stop():
            return False
        log.error(f"  the gift never left the party, so L+R+Start did not "
                  f"register within {plan.reset_timeout:.0f}s. Stopping "
                  f"rather than re-evaluating the same Pokémon forever.")
        ctx.dashboard.broadcast("read_failure",
                                reason="soft reset did not register")
        ctx.request_stop("soft reset did not register")
        return False

    attempt = 0
    while not ctx.should_stop():
        attempt += 1
        log.info(f"Gift attempt #{attempt}")
        ctx.dashboard.broadcast("soft_reset_attempt", count=attempt)
        _focus_if_needed(ctx)
        # Re-find the party after the relaunch, once, before the poll
        # loop starts leaning on the cached window.
        relocate()

        # 1. Mash A until something new is in the party. Detection is
        #    what ends the pressing -- see the module docstring: the
        #    nickname prompt is one A press past this point.
        if not _spam_a_until_received(ctx, plan.press_hold, plan.press_gap,
                                      plan.receive_timeout, gift_present,
                                      plan.detect_every, plan.hold_button):
            if ctx.should_stop():
                return
            # Say what was actually seen. "Nothing arrived" has
            # several very different causes and they are trivial to
            # tell apart from the numbers, but impossible from the
            # sentence alone.
            relocate()
            everything = read_all()
            team = read_team()
            log.error(f"  nothing reached the party in "
                      f"{plan.receive_timeout:.0f}s of A presses.")
            log.error(f"  broad scan now sees {len(everything)} owned "
                      f"PK6 ({len(baseline_keys)} at baseline), party "
                      f"{len(team)}/6, "
                      f"{len(new_arrivals(everything, baseline_keys))} "
                      f"with a key not in the baseline.")
            log.error("  If that count has not moved, nothing was added: "
                      "the save is not in front of the giver, or Azahar "
                      "is not receiving input (run "
                      "scripts/test_input.py).")
            log.error(f"  If a Pokémon DID appear in game, it is not "
                      f"OT {player_ot!r} — an in-game TRADE keeps the "
                      f"other trainer's OT and this mode cannot see it "
                      f"at all. Check soft_reset.trainer_name matches "
                      f"your in-game OT exactly.")
            ctx.dashboard.broadcast(
                "read_failure", attempt=attempt,
                reason="no gift received from A presses")
            ctx.request_stop("no gift received — check input and save")
            return

        # 2. Read it properly. The detect above is a yes/no; this is
        #    the record we report and decide on.
        relocate()
        arrivals = new_arrivals(read_all(), baseline_keys)
        if not arrivals:
            # Detection said yes and the re-read says no: the gift was
            # mid-write. Treat as a miss and reset rather than guess.
            log.warning(f"  attempt {attempt}: the new Pokémon vanished "
                        f"between checks. Resetting.")
            ctx.dashboard.broadcast(
                "read_failure", attempt=attempt,
                reason="arrival vanished between checks")
            if not reset_or_stop():
                return
            continue

        pkm = arrivals[0]
        # Strip = the saved team plus whatever just arrived, wherever
        # in RAM it landed.
        broadcast_party(ctx, (list(baseline_team)
                              + arrivals)[:6])
        ctx.dashboard.broadcast(
            "candidate", attempt=attempt,
            species=pkm.species, nickname=pkm.nickname,
            shiny=pkm.shiny, nature=pkm.nature, gender=pkm.gender,
            ivs=pkm.ivs, pid=pkm.pid, tsv=pkm.tsv, psv=pkm.psv,
            ability_id=pkm.ability_id, ability_num=pkm.ability_num,
            level=(pkm.party or {}).get("level"), moves=pkm.moves)
        log.info(f"  gift: #{pkm.species} {pkm.nickname or ''} "
                 f"{'★SHINY★ ' if pkm.shiny else ''}"
                 f"nature={pkm.nature} IVs={pkm.ivs} "
                 f"PID={pkm.pid:08X} PSV={pkm.psv} TSV={pkm.tsv}")

        if is_target(ctx.target, pkm):
            addr = getattr(pkm, "source_address", None)
            if addr:
                save_target_pk6(ctx, addr, pkm,
                                "shiny" if pkm.shiny else "gift")
            reason = _describe(ctx.target, pkm)
            bar = "*" * 30
            for line in (
                bar,
                f"  TARGET — attempt #{attempt}: {reason}",
                f"  #{pkm.species} nature={pkm.nature} IVs={pkm.ivs}",
                "  Bot STOPPED — it is in your party.",
                "  Decline the nickname (B), then SAVE. Until you do, "
                "a reset takes it back.",
                bar,
            ):
                log.info(line)
            ctx.dashboard.broadcast(
                "target_hit", attempt=attempt, count=attempt,
                reason=reason, species=pkm.species, shiny=pkm.shiny,
                nature=pkm.nature, ivs=pkm.ivs)
            ctx.request_stop("target hit")
            return

        if not reset_or_stop():
            return

    log.info(f"Gift soft-reset stopped after {attempt} attempt(s).")
