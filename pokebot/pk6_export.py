"""
Export hit targets to PKHeX-compatible .pk6 files in ``targets/``.

The folder lives at the project root and is created lazily the first
time a mode's ``run()`` calls :func:`ensure_targets_dir` — so it only
shows up after the user actually starts the bot. Each saved file is a
232-byte BOX-format DECRYPTED record (the canonical .pk6 layout PKHeX
imports natively).

A hit save reads ``raw = ctx.rpc.read(addr, 232)`` then runs the same
decrypt + checksum-validate path the live scanner uses, so partial
party-slot reads (body encrypted, party stats plaintext — irrelevant
to the box record) and plain wild-foe records both work.

Filename pattern:
  ``<label>_<species:03d>_<safe-nickname>_PID<PID:08X>_<unixtime>.pk6``

``label`` is a short tag the caller passes (``"starter"`` /
``"wild"`` / ``"shiny"``) so attempts are easy to skim at a glance.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from .parser import calc_checksum, decrypt_pkm

log = logging.getLogger(__name__)

# pokebot/pk6_export.py lives one level inside the project root.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
TARGETS_DIR = _PROJECT_ROOT / "targets"

_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


def ensure_targets_dir() -> Path:
    """Create ``targets/`` if it doesn't exist; return its path. Cheap
    and idempotent — each mode calls this on entry so the folder
    appears the instant a hunt starts."""
    TARGETS_DIR.mkdir(parents=True, exist_ok=True)
    return TARGETS_DIR


def _safe(name: str, fallback: str) -> str:
    cleaned = _SAFE_RE.sub("", name or "")[:24]
    return cleaned or fallback


def save_target_pk6(ctx, addr: int, pkm, label: str) -> Path | None:
    """Read the 232-byte BOX record at ``addr``, decrypt to PKHeX
    plaintext, and write it to ``targets/``. Returns the saved path,
    or None on read/decrypt/write failure (logged, never raises)."""
    try:
        raw = ctx.rpc.read(addr, 232)
    except Exception as e:
        log.warning(f"  .pk6 save: read {addr:#010x} failed: {e}")
        return None

    plain = None
    try:
        dec = decrypt_pkm(raw)
        if calc_checksum(dec) == int.from_bytes(dec[6:8], "little"):
            plain = dec
    except Exception:
        pass
    if plain is None:
        # Some records are already plaintext — fall through to that.
        try:
            if (len(raw) >= 232 and
                    calc_checksum(raw[:232])
                    == int.from_bytes(raw[6:8], "little")):
                plain = bytes(raw[:232])
        except Exception:
            pass
    if plain is None:
        log.warning(f"  .pk6 save: checksum mismatch at {addr:#010x}; "
                    f"not saving.")
        return None

    ensure_targets_dir()
    nick = _safe(pkm.nickname, f"sp{pkm.species}")
    fname = (f"{label}_{pkm.species:03d}_{nick}"
             f"_PID{pkm.pid:08X}_{int(time.time())}.pk6")
    path = TARGETS_DIR / fname
    try:
        path.write_bytes(plain)
    except Exception as e:
        log.warning(f"  .pk6 save: write {path} failed: {e}")
        return None
    log.info(f"  saved target → targets/{fname}")
    return path
