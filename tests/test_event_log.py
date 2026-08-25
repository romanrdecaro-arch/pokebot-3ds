"""
Tests for the durable event log and the missed-shiny audit.

The log exists to answer one question after the fact: "did a shiny go
past undetected?" These tests pin the parts that question depends on —
that events reach disk, that the Pokemon's PID survives, that rotation
does not lose history, and that the audit actually catches a miss.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from pokebot import event_log  # noqa: E402

import audit_log  # noqa: E402


@pytest.fixture
def log_path(tmp_path):
    p = tmp_path / "events.jsonl"
    event_log.configure(p)
    yield p
    event_log.configure(enabled=False)


TSV = 3648


def _encounter(pid: int, shiny: bool) -> dict:
    psv = (pid >> 16) ^ (pid & 0xFFFF)
    return {"type": "encounter", "species": 656, "pid": pid,
            "psv": psv, "tsv": TSV, "shiny": shiny}


def _shiny_pid(seed: int = 0) -> int:
    """A PID that really is shiny against TSV."""
    for pid in range(seed, seed + 1_000_000):
        if (((pid >> 16) ^ (pid & 0xFFFF)) ^ TSV) < 16:
            return pid
    raise AssertionError("no shiny PID found")


# --------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------

def test_events_reach_disk(log_path) -> None:
    event_log.append({"type": "encounter", "species": 25})
    assert log_path.exists()
    assert len(event_log.read_events(log_path)) == 1


def test_process_id_does_not_clobber_the_pokemon_pid(log_path) -> None:
    """Regression: the writer's field was called "pid", same as the
    Personality Value, so the record overwrote it and attribution was
    silently lost on exactly the events worth attributing."""
    event_log.append(_encounter(0xD476C969, False))
    rec = event_log.read_events(log_path)[0]
    assert rec["pid"] == 0xD476C969        # the Pokemon
    assert isinstance(rec["proc"], int)    # the writing process
    assert rec["proc"] != rec["pid"]


def test_append_never_raises_on_a_bad_path(tmp_path) -> None:
    """Logging must not be able to kill a hunt."""
    event_log.configure(tmp_path / "nope" / "\0bad" / "events.jsonl")
    event_log.append({"type": "encounter"})     # must not raise
    event_log.configure(enabled=False)


def test_disabled_log_writes_nothing(tmp_path) -> None:
    p = tmp_path / "events.jsonl"
    event_log.configure(p, enabled=False)
    event_log.append({"type": "encounter"})
    assert not p.exists()


def test_truncated_line_does_not_break_reading(log_path) -> None:
    event_log.append(_encounter(0x11111111, False))
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write('{"type": "encounter", "pid": 12')   # killed mid-write
    events = event_log.read_events(log_path)
    assert len(events) == 1, "a torn final line must not hide the rest"


def test_rotation_bounds_history_and_keeps_it_ordered(
        tmp_path, monkeypatch) -> None:
    """Rotation caps disk use, so the OLDEST events are dropped.

    What must hold: the file set stays bounded, the most recent events
    survive, and reading back returns them oldest-first across the
    rotated files rather than jumbled.
    """
    p = tmp_path / "events.jsonl"
    event_log.configure(p)
    monkeypatch.setattr(event_log, "MAX_BYTES", 400)
    for i in range(60):
        event_log.append(_encounter(0x1000 + i, False))

    rotated = list(tmp_path.glob("events.jsonl.*"))
    assert rotated, "nothing rotated despite exceeding MAX_BYTES"
    assert len(rotated) <= event_log.KEEP, "more files kept than KEEP"

    events = event_log.read_events(p)
    pids = [e["pid"] for e in events]
    assert pids == sorted(pids), "rotated history came back out of order"
    assert pids[-1] == 0x1000 + 59, "the newest event was rotated away"
    assert len(events) < 60, "nothing was discarded; cap not enforced"
    event_log.configure(enabled=False)


def test_default_retention_covers_a_long_hunt() -> None:
    """The defaults must hold far more than a realistic phase.

    An encounter line is ~250-400 bytes; at 5 MB x (1 active + 3
    rotated) that is well over 50,000 encounters, so a 6,000-encounter
    phase is never at risk of being rotated away mid-hunt.
    """
    budget = event_log.MAX_BYTES * (event_log.KEEP + 1)
    assert budget // 400 > 40_000


# --------------------------------------------------------------------
# The audit
# --------------------------------------------------------------------

def test_audit_flags_a_missed_shiny() -> None:
    bad = _shiny_pid()
    events = [_encounter(0x12345678, False), _encounter(bad, False)]
    r = audit_log.audit(events)
    assert len(r["missed"]) == 1
    assert r["missed"][0][1]["pid"] == bad


def test_audit_is_quiet_when_detection_was_correct() -> None:
    good = _shiny_pid()
    events = [_encounter(0x12345678, False), _encounter(good, True),
              {"type": "target_hit", "count": 1}]
    r = audit_log.audit(events)
    assert r["missed"] == []
    assert len(r["flagged"]) == 1


def test_audit_recomputes_without_a_stored_psv() -> None:
    """Older records may carry only the PID."""
    bad = _shiny_pid()
    r = audit_log.audit([{"type": "encounter", "pid": bad, "tsv": TSV,
                          "shiny": False}])
    assert len(r["missed"]) == 1


def test_audit_reports_unverifiable_records() -> None:
    """No tsv recorded means shininess cannot be recomputed at all —
    that must be reported, not silently counted as clean."""
    r = audit_log.audit([{"type": "encounter", "pid": 1, "shiny": False}])
    assert r["undecidable"] == 1
    assert r["missed"] == []


def test_audit_surfaces_read_failures_as_gaps() -> None:
    r = audit_log.audit([{"type": "read_failure", "reason": "timeout"}])
    assert len(r["failures"]) == 1


def test_audit_ranks_closest_approaches() -> None:
    events = [_encounter(0x12345678, False), _encounter(0xAABBCCDD, False)]
    r = audit_log.audit(events)
    dists = [d for d, _ in r["closest"]]
    assert dists == sorted(dists)


def test_end_to_end_broadcast_is_auditable(log_path) -> None:
    """A real broadcast must land in the log in an auditable shape."""
    from pokebot.dashboard_server import DashboardServer
    bad = _shiny_pid()
    d = DashboardServer()
    d.broadcast("encounter", species=656, pid=bad,
                psv=(bad >> 16) ^ (bad & 0xFFFF), tsv=TSV, shiny=False,
                ivs={}, nature="Timid", gender="M", level=5)
    r = audit_log.audit(event_log.read_events(log_path))
    assert len(r["missed"]) == 1, "audit could not see a real broadcast"
