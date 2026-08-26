"""Bot modes. Each mode is a function that runs the per-frame loop logic."""

from .observe import run as run_observe
from .encounter import run as run_encounter
from .horde import run as run_horde
from .fishing import run as run_fishing
from .soft_reset import run as run_soft_reset
from .debug import run as run_debug
from .livehex import run as run_livehex
from .crystal_observe import run as run_crystal_observe
from .crystal_encounter import run as run_crystal_encounter
from .crystal_celebi import run as run_crystal_celebi

MODES = {
    "observe":     run_observe,
    "encounter":   run_encounter,
    "horde":       run_horde,
    "fishing":     run_fishing,
    "soft_reset":  run_soft_reset,
    "debug":       run_debug,
    "livehex":     run_livehex,
    # Gen 2 (Virtual Console)
    "crystal_observe": run_crystal_observe,
    "crystal_encounter": run_crystal_encounter,
    "crystal_celebi": run_crystal_celebi,
}
