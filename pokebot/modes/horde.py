"""
Horde encounters — shiny hunting in groups of 5, Sweet Scent triggered.

A **horde** is a battle where **5 wild Pokémon** appear on screen at
once, each rolled independently (own PID / IVs / shiny check). Effective
shiny rate ≈ 5× a single encounter. Fleeing is one RUN press and ends
the whole battle.

In Gen 6 the move **Sweet Scent** pulls 100% from the horde table on
any horde-enabled route — so this mode skips the random-walk approach
entirely and just fires Sweet Scent each iteration:

  1. Tap **X, A, A, Down, A** (1-second intervals): open menu, pick
     Pokémon, pick slot 1 (the Sweet Scent user), select Sweet Scent.
  2. Wait for the horde intro animation to settle.
  3. Detect — every checksum-valid non-party PK6 in the foe window is
     reported (5 Recently Seen rows per battle) and target-checked.
  4. If ANY of the 5 is shiny / on-target: STOP + alert.
     Otherwise: flee (one RUN press ends the whole horde).

Detection + flee are the same engine as `encounter.run`; only the idle
action differs (Sweet Scent instead of walking).

**Requirements (in-game):**

- Slot 1 must hold a Sweet Scent user (Bulbasaur from Sycamore is the
  easiest in X/Y — free, learns the move, never has to evolve).
- Player must be standing on a route with a horde encounter table (the
  early routes 1-3 have none; Route 5+ is fine).
- **Smoke Ball** held by the slot-1 mon is strongly recommended — it
  guarantees the flee succeeds even against fast hordes.
"""
from __future__ import annotations

import logging

from .encounter import run as _encounter_run

log = logging.getLogger(__name__)


def run(ctx):
    rcfg = ctx.config.setdefault("random_encounters", {})
    # Horde intro is longer than a single encounter — bump default
    # flee_delay so the command menu is actually drawn before RUN.
    rcfg.setdefault("flee_delay", 7.0)
    # Switch the encounter loop's idle action to Sweet Scent.
    rcfg.setdefault("idle_action", "sweet_scent")
    rcfg.setdefault("sweet_scent_gap", 1.0)
    rcfg.setdefault("sweet_scent_settle", 4.0)
    log.info("Mode: horde encounters (Sweet Scent → guaranteed 5-mon "
             "horde; stops on ANY shiny / target in the horde)")
    log.info("  Slot 1 MUST hold a Sweet Scent user; Smoke Ball "
             "recommended for guaranteed flee.")
    _encounter_run(ctx)
