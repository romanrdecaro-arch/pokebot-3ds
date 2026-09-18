"""Bot modes. Each mode is a function that runs the per-frame loop logic."""

from .observe import run as run_observe
from .encounter import run as run_encounter
from .sweet_scent import run as run_sweet_scent
from .fishing import run as run_fishing
from .rock_smash import run as run_rock_smash
from .soft_reset import run as run_soft_reset
from .gifts import run as run_gifts
from .debug import run as run_debug
from .livehex import run as run_livehex
from .crystal_observe import run as run_crystal_observe
from .crystal_encounter import run as run_crystal_encounter
from .crystal_celebi import run as run_crystal_celebi

MODES = {
    "observe":     run_observe,
    "encounter":   run_encounter,
    "sweet_scent": run_sweet_scent,
    # Kept so configs and command lines written before the
    # rename keep working. Same mode, older name.
    "horde":       run_sweet_scent,
    "fishing":     run_fishing,
    "rock_smash":  run_rock_smash,
    "soft_reset":  run_soft_reset,
    "gifts":       run_gifts,
    "debug":       run_debug,
    "livehex":     run_livehex,
    # Gen 2 (Virtual Console)
    "crystal_observe": run_crystal_observe,
    "crystal_encounter": run_crystal_encounter,
    "crystal_celebi": run_crystal_celebi,
}
