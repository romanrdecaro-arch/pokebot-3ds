"""Bot modes. Each mode is a function that runs the per-frame loop logic."""

from .observe import run as run_observe
from .encounter import run as run_encounter
from .horde import run as run_horde
from .soft_reset import run as run_soft_reset
from .debug import run as run_debug
from .livehex import run as run_livehex

MODES = {
    "observe":     run_observe,
    "encounter":   run_encounter,
    "horde":       run_horde,
    "soft_reset":  run_soft_reset,
    "debug":       run_debug,
    "livehex":     run_livehex,
}
