"""
Audit the event log for missed shinies and detection gaps.

    python scripts/audit_log.py
    python scripts/audit_log.py --path logs/events-pid1234.jsonl

The important check is the RECOMPUTE: for every logged encounter this
recalculates shininess from the raw PID/TID/SID and compares it with
the flag the bot recorded at the time. A disagreement means a shiny
went past undetected, which is the one failure that cannot be undone.

It also reports encounters that were flagged shiny but never followed
by a target_hit (detected but not acted on), and the gaps — read
failures and session restarts — where an encounter could have happened
without ever being logged at all.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pokebot.event_log import read_events, current_path  # noqa: E402

SHINY_THRESHOLD = 16


def _psv(evt: dict):
    psv = evt.get("psv")
    if psv is None:
        pid = evt.get("pid")
        if pid is None:
            return None
        pid = int(pid)
        psv = (pid >> 16) ^ (pid & 0xFFFF)
    return int(psv)


def _distance(evt: dict):
    """psv ^ tsv — under 16 is shiny. None when the log lacks the IDs."""
    psv = _psv(evt)
    tsv = evt.get("tsv")
    if psv is None or tsv is None:
        return None
    return psv ^ int(tsv)


def audit(events: list[dict]) -> dict:
    encounters = [e for e in events
                  if e.get("type") in ("encounter", "candidate")]
    hits = [e for e in events if e.get("type") == "target_hit"]
    failures = [e for e in events if e.get("type") == "read_failure"]
    sessions = [e for e in events if e.get("type") == "session_start"]

    missed, flagged, closest, undecidable = [], [], [], 0
    for e in encounters:
        dist = _distance(e)
        recorded = bool(e.get("shiny"))
        if dist is None:
            undecidable += 1
            if recorded:
                flagged.append(e)
            continue
        truly_shiny = dist < SHINY_THRESHOLD
        if truly_shiny and not recorded:
            missed.append((dist, e))       # the bad one
        if recorded:
            flagged.append(e)
        closest.append((dist, e))

    closest.sort(key=lambda p: p[0])
    return {
        "events": len(events),
        "encounters": len(encounters),
        "flagged": flagged,
        "missed": missed,
        "hits": hits,
        "failures": failures,
        "sessions": sessions,
        "closest": closest[:5],
        "undecidable": undecidable,
    }


def _describe(e: dict) -> str:
    pid = e.get("pid")
    pid_s = f"{int(pid):08X}" if pid is not None else "????????"
    return (f"#{e.get('species', '?')} {e.get('nickname') or ''} "
            f"PID={pid_s} psv={_psv(e)} tsv={e.get('tsv')}").strip()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--path", default=None,
                    help="log file (default: logs/events.jsonl)")
    ap.add_argument("--no-rotated", action="store_true",
                    help="read only the active file, not the .1/.2 history")
    args = ap.parse_args(argv)

    path = Path(args.path) if args.path else current_path()
    events = read_events(path, include_rotated=not args.no_rotated)
    if not events:
        print(f"No events found at {path}")
        print("The log is written as the bot runs; a hunt started before")
        print("this feature existed will have nothing here yet.")
        return 1

    r = audit(events)
    print(f"log            : {path}")
    print(f"events         : {r['events']}")
    print(f"encounters     : {r['encounters']}")
    print(f"sessions       : {len(r['sessions'])}")
    print(f"read failures  : {len(r['failures'])}")
    print(f"target hits    : {len(r['hits'])}")
    print(f"flagged shiny  : {len(r['flagged'])}")
    if r["undecidable"]:
        print(f"unverifiable   : {r['undecidable']} "
              f"(no tsv recorded; cannot recompute)")

    print()
    if r["missed"]:
        print(f"!! {len(r['missed'])} MISSED SHINY "
              f"{'ENCOUNTER' if len(r['missed']) == 1 else 'ENCOUNTERS'} !!")
        print("   Recomputed from PID/TID/SID as shiny, but the bot did")
        print("   not flag it at the time:")
        for dist, e in r["missed"]:
            print(f"     psv^tsv={dist:<5} {_describe(e)}")
    else:
        print("No missed shinies: every logged encounter's recorded flag")
        print("matches shininess recomputed from its PID/TID/SID.")

    stranded = len(r["flagged"]) - len(r["hits"])
    if stranded > 0:
        print()
        print(f"NOTE: {stranded} encounter(s) flagged shiny with no matching")
        print("      target_hit — detected but possibly not acted on.")

    if r["closest"]:
        print()
        print("Closest approaches (psv^tsv, under 16 would be shiny):")
        for dist, e in r["closest"]:
            print(f"   {dist:<6} {_describe(e)}")

    if r["failures"]:
        print()
        print(f"WARNING: {len(r['failures'])} read failure(s) logged. An")
        print("         encounter during one of those may never have been")
        print("         scanned, so it would not appear here at all.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
