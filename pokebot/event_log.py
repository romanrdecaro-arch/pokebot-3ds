"""
Durable, append-only event log.

The launcher's Recently Seen table holds the last 30 encounters in
memory and forgets everything when it closes, so a question like "did
the bot miss a shiny 4,000 encounters ago?" had no evidence to answer
it from — the only thing on disk was a counter file.

This writes one JSON object per line (JSON Lines) as each event is
broadcast, from the bot process, so it works for CLI runs as well as
the GUI. Every record carries the writing process id, so the logs of
two instances hunting side by side stay tellable apart.

Rotation is by size: at ``MAX_BYTES`` the file becomes ``.1``, the old
``.1`` becomes ``.2``, and anything past ``KEEP`` is dropped. An
encounter line is roughly 400 bytes, so the default keeps on the order
of a hundred thousand encounters before the oldest is discarded.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

#: Rotate once the active file passes this size.
MAX_BYTES = 5 * 1024 * 1024
#: How many rotated files to keep alongside the active one.
KEEP = 3

_DEFAULT_DIR = Path(__file__).resolve().parent.parent / "logs"

_lock = threading.Lock()
_path: Path | None = None
_enabled = True
_warned = False


def configure(path: str | Path | None = None, enabled: bool = True) -> None:
    """Point the log at a file. Call once at startup."""
    global _path, _enabled, _warned
    _enabled = bool(enabled)
    _warned = False
    if path is None:
        _path = _DEFAULT_DIR / "events.jsonl"
    else:
        _path = Path(path)
    if _enabled:
        log.info(f"event log: {_path}")


def current_path() -> Path:
    if _path is None:
        configure()
    assert _path is not None
    return _path


def _rotate(path: Path) -> None:
    """Shift path -> .1 -> .2 ... dropping anything past KEEP."""
    oldest = path.with_suffix(path.suffix + f".{KEEP}")
    if oldest.exists():
        oldest.unlink(missing_ok=True)
    for n in range(KEEP - 1, 0, -1):
        src = path.with_suffix(path.suffix + f".{n}")
        if src.exists():
            src.replace(path.with_suffix(path.suffix + f".{n + 1}"))
    path.replace(path.with_suffix(path.suffix + ".1"))


def append(record: dict) -> None:
    """Append one event. Never raises — logging must not kill a hunt."""
    if not _enabled:
        return
    global _warned
    path = current_path()
    try:
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if path.stat().st_size >= MAX_BYTES:
                    _rotate(path)
            except FileNotFoundError:
                pass
            # "proc", NOT "pid": an encounter record's own "pid" is the
            # Pokemon's Personality Value. Naming this one "pid" let
            # the record overwrite it, silently losing the process
            # attribution on exactly the events worth attributing.
            line = json.dumps(
                {"proc": os.getpid(), **record},
                default=str, ensure_ascii=False)
            # One short line in append mode: the OS keeps concurrent
            # appends from two instances from interleaving mid-line.
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as exc:
        if not _warned:
            _warned = True
            log.warning(f"event log disabled ({type(exc).__name__}: {exc})")


def read_events(path: str | Path | None = None,
                include_rotated: bool = True) -> list[dict]:
    """Read the log back, oldest first. Bad lines are skipped.

    Used by the audit tooling; a truncated final line (killed
    mid-write) must not make the whole log unreadable.
    """
    base = Path(path) if path is not None else current_path()
    files: list[Path] = []
    if include_rotated:
        for n in range(KEEP, 0, -1):
            rotated = base.with_suffix(base.suffix + f".{n}")
            if rotated.exists():
                files.append(rotated)
    if base.exists():
        files.append(base)

    out: list[dict] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue          # truncated / interleaved line
            if isinstance(obj, dict):
                out.append(obj)
    return out


def session_marker(mode: str, extra: dict | None = None) -> None:
    """Record that a run started, so phases can be bounded in the log."""
    append({"type": "session_start", "ts": time.time(),
            "mode": mode, **(extra or {})})
