"""
Show where the next shiny sits in X/Y's live RNG.

Reads the main MT19937 from a running X or Y in Azahar twice, a second
apart, to measure how fast it is advancing, then lists the first windows
of offsets at which a Pokemon generated right there would be shiny.

    python scripts/rng_preview.py --tid 31182 --sid 25810 --rolls 3

--rolls is 1 normally and 3 with the Shiny Charm. Offsets count MT
advances from the second read; the ETA assumes the measured rate holds,
which it only roughly does (the rate changes with what is on screen).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pokebot.citra_rpc import POKEMON_TITLE_IDS, CitraRPC, RPCError  # noqa: E402
from pokebot.parser import NATURES  # noqa: E402
from pokebot.rng6 import (GenSpec, RNGReadError, advances_between,  # noqa: E402
                          read_main_mt, shiny_hits)

XY_TITLES = {tid: name for tid, (name, _) in POKEMON_TITLE_IDS.items()
             if name in ("X", "Y")}
#: Upper bound on advances the rate probe will step through.
_RATE_LIMIT = 100_000


def measure(rpc, interval: float = 1.0, sleep=time.sleep, clock=time.monotonic):
    """(fresh MT state, advances per second).

    Two reads that no stepping links mean the pointer is not the MT, or
    the game reset in between, so that is an error rather than a guess."""
    first = read_main_mt(rpc)
    t0 = clock()
    sleep(interval)
    second = read_main_mt(rpc)
    elapsed = clock() - t0
    n = advances_between(first, second, _RATE_LIMIT)
    if n is None:
        raise RNGReadError(f"the second MT read is not within {_RATE_LIMIT} "
                           f"advances of the first: the game reset, or the "
                           f"pointer does not lead to the MT object")
    return second, (n / elapsed if elapsed > 0 else None)


def shiny_windows(mt, spec: GenSpec, tid: int, sid: int, advances: int):
    """Yield (first offset, last offset, Prediction) per shiny PID.

    With several PID rolls one shiny draw is reached from up to that many
    adjacent start offsets (as roll 1, 2, 3...), and the reroll stops on
    it, so everything after the PID is identical: one window, one row."""
    window = None
    for offset, p in shiny_hits(mt, spec, tid, sid, advances):
        if window and offset == window[1] + 1 and p.pid == window[2].pid:
            window = (window[0], offset, window[2])
            continue
        if window:
            yield window
        window = (offset, offset, p)
    if window:
        yield window


def _rate_text(rate) -> str:
    if rate is None:
        return "advance rate unknown"
    if rate == 0:
        return "not advancing (paused?)"
    return f"advancing {rate:.0f}/s"


def report(mt, rate, spec: GenSpec, tid: int, sid: int,
           advances: int, show: int) -> list[str]:
    lines = [f"MT index {mt.index}; {_rate_text(rate)}"]
    found = 0
    for first, last, p in shiny_windows(mt, spec, tid, sid, advances):
        span = f"+{first}" if first == last else f"+{first}..{last}"
        eta = f" (~{first / rate:5.1f}s)" if rate else ""
        ivs = "/".join(str(v) for v in p.ivs)
        lines.append(f"  {span:>14}{eta}  PID {p.pid:08X}  {NATURES[p.nature]:<7} "
                     f"{p.gender}  IVs {ivs}  ability slot {p.ability_slot}")
        found += 1
        if found >= show:
            break
    if not found:
        lines.append(f"  no shiny in the next {advances} advances")
    return lines


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--tid", type=int, required=True, help="trainer ID (0-65535)")
    ap.add_argument("--sid", type=int, required=True, help="secret ID (0-65535)")
    ap.add_argument("--rolls", type=int, default=1,
                    help="PID rolls: 1, or 3 with the Shiny Charm")
    ap.add_argument("--perfect-ivs", type=int, default=0,
                    help="guaranteed 31 IVs (3 for most legendaries)")
    ap.add_argument("--hidden-ability", action="store_true",
                    help="the encounter can roll its hidden ability")
    ap.add_argument("--nature", type=int, default=None,
                    help="only for a nature known to be fixed: id 0-24 "
                         "(0 Hardy ... 24 Quirky)")
    ap.add_argument("--gender-ratio", type=int, default=127,
                    help="species gender byte: 0 male only, 254 female only, "
                         "255 genderless, 1-253 the female threshold (127 = 50/50)")
    ap.add_argument("--advances", type=int, default=20000, help="how far ahead to look")
    ap.add_argument("--show", type=int, default=5, help="how many shiny windows to list")
    args = ap.parse_args(argv)
    for name in ("tid", "sid"):
        if not 0 <= getattr(args, name) <= 0xFFFF:
            ap.error(f"--{name} must be 0..65535")
    if not 1 <= args.advances <= 2_000_000:
        ap.error("--advances must be 1..2000000")
    if args.show < 1:
        ap.error("--show must be at least 1")
    try:
        args.spec = GenSpec(pid_rolls=args.rolls, perfect_ivs=args.perfect_ivs,
                            hidden_ability=args.hidden_ability, nature=args.nature,
                            gender_ratio=args.gender_ratio)
    except ValueError as exc:
        ap.error(str(exc))
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    kwargs = {"host": args.host}
    if args.port:
        kwargs["port"] = args.port
    rpc = None
    try:
        rpc = CitraRPC(**kwargs)
        _, title_id, game = rpc.attach_to_pokemon_game()
        if title_id not in XY_TITLES:
            print(f"error: {game} is running; this reads X/Y only", file=sys.stderr)
            return 1
        mt, rate = measure(rpc)
    except (RPCError, RNGReadError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if rpc is not None:
            rpc.close()
    print(f"Pokemon {XY_TITLES[title_id]}, TID {args.tid} SID {args.sid}, "
          f"{args.rolls} PID roll(s)")
    print("\n".join(report(mt, rate, args.spec, args.tid, args.sid,
                           args.advances, args.show)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
