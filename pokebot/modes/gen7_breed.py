"""
Gen 7 shiny breeding by egg seed (Ultra Sun / Ultra Moon).

Leave two Pokemon that make eggs in the Nursery (Paniola Ranch) -- from
games of different languages (the Masuda method), or with the Shiny
Charm in the bag and breeding.shiny_charm: true, or both -- keep a party
slot free (or set Party/Box to Automatic in Options, so a full party
sends the egg to the PC without asking), and stand facing the Nursery
lady outside it. The loop:

    the egg on offer is shiny  -> accept it, check it, and STOP: save
                                  the game yourself
    anything else              -> decline it, which moves the egg seed
                                  one step, and look again

so only the shiny egg is ever taken: nothing to hatch, and no boxes of
eggs to clear (eggs cannot be released).

How, from source (2026-11-01; the prediction is pokebot/egg7.py):
* PKMN-NTR's breeding bot (Bot_Breeding7.cs) and 3DSRNGTool read the
  Nursery at USUM 0x3307B010, laid out as PKHeX's Daycare7 (the RAM
  blocks are packed differently from the save file): +0x000 / +0x0E9 a
  slot is in use, +0x001 / +0x0EA the parents (PK7), +0x1D8 an egg is
  waiting, +0x1DC the egg seed (four little-endian u32, s0 first).
* Instead of walking, the egg-waiting flag is set to 1, as PKMN-NTR's
  bot did: it is what enough steps would set, and the egg is still
  drawn by the game from the seed.
* The seed says what the buttons did: one step means declined, the
  predicted egg's length means accepted. So the dialogue is driven
  loosely (A to talk, then A to accept or B to decline until the seed
  moves) and every egg is judged afresh from the seed it starts from.

Not run against the game yet: the addresses on Azahar and the dialogue
timing are unconfirmed. Nothing is written until both Nursery records
decode with valid checksums.
"""
from __future__ import annotations

import logging
import struct
import time
from dataclasses import dataclass

from ..citra_rpc import RPCError
from ..egg7 import Parents, Stream, advance, eggs_until_shiny, generate
from ..games import LIVEHEX_REFERENCES
from ..parser import NATURES
from .observe import _decode, gen7_trainer_ot
from .soft_reset import _focus_if_needed

log = logging.getLogger(__name__)

#: The Nursery block (PKMN-NTR's USUM egg flag 0x3307B1E8, less 0x1D8).
_NURSERY = {"USUM-USA-1.2": 0x3307B010}
_SLOTS = ((0x000, 0x001), (0x0E9, 0x0EA))      # (in use, PK7)
_FLAG, _SEED, _LEN = 0x1D8, 0x1DC, 0x1EC
_LIVE_STRIDE = 484
_GAP = 0.4               # seconds between presses and reads
_ANSWER_TIMEOUT = 15.0   # seconds one talk has to move the seed
_TRIES = 3               # talks in a row that change nothing, then stop
_CLEAR = 4               # B taps after an answer, for her last line
_LIMIT = 20000           # eggs looked ahead for a shiny one
_PROGRESS = 25           # declines between progress lines


@dataclass(frozen=True)
class Nursery:
    parents: tuple
    ready: bool
    seed: tuple


def read_nursery(rpc, base: int) -> Nursery | None:
    """The Nursery, or None unless both slots hold a valid record."""
    raw = bytes(rpc.read(base, _LEN))
    parents = []
    for used, rec in _SLOTS:
        pkm = (_decode(raw[rec:rec + 0xE8], max_species=807)
               if raw[used] == 1 else None)
        if pkm is None:
            return None
        parents.append(pkm)
    return Nursery(tuple(parents), raw[_FLAG] == 1,
                   struct.unpack_from("<4I", raw, _SEED))


def _tsv(ctx) -> int | None:
    if gen7_trainer_ot(ctx) is None:            # logs why
        return None
    tb = LIVEHEX_REFERENCES[ctx.game.key]["trainer_block"]
    tid, sid = struct.unpack("<HH", bytes(ctx.rpc.read(tb, 4)))
    return (tid ^ sid) >> 4


def _answer(ctx, base: int, seed: tuple, accept: bool) -> tuple | None:
    """Talk to the Nursery lady, then A (Yes) or B (moves the text along,
    and is No at her question) until the egg seed moves. The new seed,
    or None if it never moved."""
    ctx.input.tap("A", hold_s=0.06)
    end = time.monotonic() + _ANSWER_TIMEOUT
    while not ctx.should_stop() and time.monotonic() < end:
        ctx._stop_evt.wait(_GAP)
        try:
            now = read_nursery(ctx.rpc, base)
        except (RPCError, OSError):
            continue
        if now is not None and now.seed != seed:
            for _ in range(_CLEAR):             # B never starts a new talk
                ctx.input.tap("B", hold_s=0.06)
                ctx._stop_evt.wait(_GAP)
            return now.seed
        ctx.input.tap("A" if accept else "B", hold_s=0.06)
    return None


def _received(ctx, egg, as_predicted: bool, tsv: int, declined: int) -> None:
    """The shiny egg was accepted: find it and say so, then stop."""
    live = ctx.game.offsets.party_live
    party = [_decode(bytes(ctx.rpc.read(live + i * _LIVE_STRIDE, 260)),
                     party=True, max_species=807) for i in range(6)]
    got = next((p for p in party if p and p.encryption_key == egg.ec), None)
    if got is not None:
        shiny = ((got.pid >> 16) ^ (got.pid & 0xFFFF)) >> 4 == tsv
        where = f"party slot {party.index(got) + 1}"
    else:                   # sent to the PC: the seed is the evidence
        shiny, where = as_predicted, "the PC (the party was full)"
    if not shiny:
        log.error(f"  the egg taken (now in {where}) is not the predicted "
                  f"shiny one. Is breeding.shiny_charm right? Bot STOPPED.")
        ctx.request_stop("egg not shiny")
        return
    nature = NATURES[egg.nature]
    bar = "*" * 30
    for line in (bar,
                 f"  SHINY EGG ({nature}) after {declined} declined; it is "
                 f"in {where}",
                 "  Bot STOPPED. SAVE THE GAME now to keep it.",
                 bar):
        log.info(line)
    ctx.dashboard.broadcast("target_hit", count=declined + 1,
                            reason=f"shiny egg ({nature}) in {where}",
                            species=got.species if got else None,
                            shiny=True, nature=nature)
    ctx.request_stop("shiny egg")


def run(ctx) -> None:
    log.info("Mode: Gen 7 breeding (egg seed)")
    base = _NURSERY.get(getattr(ctx.game, "key", ""))
    if not base:
        log.error("  breeding is written for Ultra Sun / Ultra Moon.")
        ctx.request_stop("not Ultra Sun / Ultra Moon")
        return
    tsv = _tsv(ctx)
    nursery = read_nursery(ctx.rpc, base) if tsv is not None else None
    if nursery is None:
        if tsv is not None:
            log.error(f"  no two Pokemon found in the Nursery "
                      f"@{base:#010x}. Leave two that make eggs there; if "
                      f"they are there, Azahar keeps it elsewhere: paste "
                      f"this log.")
        ctx.request_stop("nursery not found")
        return
    cfg = ctx.config.get("breeding") or {}
    parents = Parents.from_nursery(*nursery.parents,
                                   bool(cfg.get("shiny_charm", False)))
    a, b = nursery.parents
    log.info(f"  Nursery: #{a.species} {a.gender} (item {a.held_item}) + "
             f"#{b.species} {b.gender} (item {b.held_item}); Masuda "
             f"{'yes' if parents.masuda else 'no'}, Shiny Charm "
             f"{'yes' if parents.shiny_charm else 'no'}")
    if not parents.pid_rolls:
        log.error("  needs the Masuda method (parents from games of "
                  "different languages) or the Shiny Charm "
                  "(breeding.shiny_charm: true). Without either the PID is "
                  "drawn as the egg is accepted, so the seed cannot tell.")
        ctx.request_stop("no Masuda / Shiny Charm")
        return
    ahead = eggs_until_shiny(nursery.seed, parents, tsv, _LIMIT)
    if ahead is None:
        log.error(f"  no shiny egg in the next {_LIMIT}; stopping.")
        ctx.request_stop("no shiny egg ahead")
        return
    log.info(f"  the first shiny egg is {ahead} decline(s) away")
    _focus_if_needed(ctx)
    declined = misses = 0
    started = time.monotonic()
    while not ctx.should_stop():
        try:
            nursery = read_nursery(ctx.rpc, base)
        except (RPCError, OSError):
            nursery = None
        new = None
        if nursery is not None:
            egg = generate(Stream(nursery.seed), 0, parents, tsv)
            if egg.shiny:
                log.info(f"  the egg on offer is SHINY ({NATURES[egg.nature]}):"
                         f" accepting it")
            if not nursery.ready:
                ctx.rpc.write(base + _FLAG, b"\x01")
            new = _answer(ctx, base, nursery.seed, accept=egg.shiny)
        if ctx.should_stop():
            return
        if new is None:
            misses += 1
            if misses >= _TRIES:
                log.error("  the Nursery lady is not answering (or the "
                          "Nursery stopped reading). Stand right below her, "
                          "facing her, and start again.")
                ctx.request_stop("nursery lady not answering")
                return
            continue
        misses = 0
        if new == advance(nursery.seed, 1):     # declined
            if egg.shiny:
                log.error("  the shiny egg was DECLINED. Do not save: soft "
                          "reset to your save and start again, the seed "
                          "goes back with it. Bot STOPPED.")
                ctx.request_stop("shiny egg declined")
                return
            declined += 1
            if declined % _PROGRESS == 0:
                left = eggs_until_shiny(new, parents, tsv, _LIMIT)
                per = (time.monotonic() - started) / declined
                log.info(f"  {declined} declined; shiny egg in {left} "
                         f"(~{(left or 0) * per / 60:.0f} min)")
            continue
        if not egg.shiny:
            log.warning("  an egg was accepted, not declined (it is in your "
                        "party or PC); judging the next one from the seed")
            continue
        _received(ctx, egg, new == advance(nursery.seed, egg.used), tsv,
                  declined)
        return
