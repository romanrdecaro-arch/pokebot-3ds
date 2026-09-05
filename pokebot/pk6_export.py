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

**Ownership matters.** A wild Pokemon in the foe slot is complete in
every stat -- species, PID, IVs, nature -- but has no OWNER: the OT
name, ball, game version, met location and trainer memories are
written by the game at the moment of CAPTURE. Exporting from the foe
slot therefore produces a file PKHeX rejects ("OT Name too short",
"unable to match an encounter from origin game"). A starter export is
legal because it is read from the party, already owned. Callers that
catch what they found should re-export with :func:`save_caught_pk6`.
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

    # The record is re-read from RAM here, but the filename is built
    # from the ALREADY-PARSED pkm. For a wild encounter, `addr` points
    # into a foe buffer the game reuses — if it turned over between
    # detection and this read, the checksum still passes (it is a
    # perfectly valid Pokemon, just a different one) and we would write
    # someone else's record under a "shiny" filename. Refuse instead:
    # a missing file is recoverable, a mislabelled one is a lie.
    found_species = int.from_bytes(plain[0x08:0x0A], "little")
    found_pid = int.from_bytes(plain[0x18:0x1C], "little")
    if found_species != pkm.species or found_pid != pkm.pid:
        log.warning(
            f"  .pk6 save: the record at {addr:#010x} changed under us "
            f"(expected species {pkm.species} PID {pkm.pid:08X}, found "
            f"species {found_species} PID {found_pid:08X}); NOT saving. "
            f"The buffer was reused before the save completed."
        )
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


# Ownership fields the game writes only when a Pokémon is CAUGHT.
# Their absence is what makes a foe-slot export illegal in PKHeX.
_OT_NAME = slice(0xB0, 0xC8)
_BALL = 0xDC
_VERSION = 0xDF


def is_owned_record(plain: bytes) -> bool:
    """Has this record been caught by a trainer yet?

    A wild Pokémon in the foe slot is fully formed — species, PID, IVs,
    nature, everything the hunt cares about — but it has no OWNER. The
    OT name, ball, game version, met location and trainer memories are
    written at the moment of capture. Exporting before that produces a
    file PKHeX rejects with "OT Name too short" and "unable to match an
    encounter from origin game", even though every stat in it is right.
    """
    if len(plain) < 232:
        return False
    has_ot = any(plain[_OT_NAME])
    return bool(has_ot and plain[_VERSION] and plain[_BALL])


def save_caught_pk6(ctx, pkm, party, label: str = "shiny",
                    supersedes: Path | None = None) -> Path | None:
    """Re-export a target from the party AFTER it has been caught.

    The pre-throw export is the only copy guaranteed to exist, but it
    is the pre-capture record and PKHeX will not accept it. Once the
    mon is in the party the game has filled in the ownership fields, so
    re-reading it there is what produces a legal file.

    ``supersedes`` is the pre-capture path; it is removed once a better
    file has been written, so nobody loads the illegal one by mistake.
    """
    match = None
    for p in party or ():
        if (getattr(p, "source_address", 0)
                and p.pid == pkm.pid and p.species == pkm.species):
            match = p
            break
    if match is None:
        log.warning(
            "  .pk6 save: the catch was found in neither the party nor "
            "the PC boxes, so the saved file is the PRE-CAPTURE record "
            "— PKHeX will call it invalid (no OT, no met data). Its "
            "stats are all correct; re-export it from your save with "
            "PKHeX if you need a legal copy.")
        return None

    path = save_target_pk6(ctx, match.source_address, pkm, label)
    if path is None:
        return None

    try:
        if not is_owned_record(path.read_bytes()):
            log.warning("  .pk6 save: re-read from the party still has "
                        "no owner set; keeping it, but PKHeX may "
                        "reject it.")
    except OSError:
        pass

    if supersedes and supersedes != path and supersedes.exists():
        try:
            supersedes.unlink()
            log.info(f"  removed the pre-capture copy "
                     f"({supersedes.name}) — it was not legal")
        except OSError as exc:
            log.debug(f"  could not remove {supersedes}: {exc}")
    return path
