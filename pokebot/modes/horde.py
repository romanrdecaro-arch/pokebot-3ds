"""
Horde encounters — shiny hunting in groups of 5.

Horde battles put **5 wild Pokémon** on screen at once, each rolled
independently (own PID/IVs/shiny check). Effective shiny rate ~5× a
normal encounter. Fleeing is one RUN press and ends the whole battle.

Mechanically this is just the random-encounters hunt with multi-mon
evaluation, which `encounter.run` already does (every checksum-valid
non-party PK6 in the foe window is reported + target-checked, so a
horde of 5 produces 5 Recently Seen rows and stops on the FIRST
shiny among them). This mode reuses that engine; the only difference
is a longer default `flee_delay` because the horde intro animation
takes longer than a single encounter.

Triggering hordes in X/Y: random ~5 % rate in tall grass / flowers
(clear weather), OR guaranteed via the move Sweet Scent / item Honey.
The bot just walks; it doesn't auto-use Sweet Scent yet.
"""
from __future__ import annotations

import logging

from .encounter import run as _encounter_run

log = logging.getLogger(__name__)


def run(ctx):
    # Horde intro is longer than a single encounter — bump the
    # default flee_delay if the user/launcher didn't override it.
    # (The launcher's slider passes --flee-delay which overrides
    # config.yaml; setdefault only kicks in when the slider wasn't
    # used or the config didn't set one.)
    rcfg = ctx.config.setdefault("random_encounters", {})
    rcfg.setdefault("flee_delay", 7.0)
    log.info("Mode: horde encounters (multi-mon evaluation — stops "
             "on ANY shiny in the horde)")
    _encounter_run(ctx)
