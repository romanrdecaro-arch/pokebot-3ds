"""
The test run must never touch the user's real files.

A full run used to append a fake "shiny Froakie caught" to the real
logs/events.jsonl -- the log a hunt is audited from -- while a live
hunt was writing to the same file. conftest.py now sandboxes it; this
pins that, so the sandbox cannot quietly stop applying.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot import event_log  # noqa: E402

REAL_LOGS = REPO / "logs"


def test_the_event_log_is_not_the_real_one():
    assert REAL_LOGS not in event_log.current_path().parents


def test_configuring_it_the_way_run_py_does_stays_in_the_sandbox():
    """run.py calls configure() with no path, which means the default."""
    event_log.configure()
    assert REAL_LOGS not in event_log.current_path().parents


def test_run_pys_explicit_path_into_logs_stays_in_the_sandbox():
    """run.py passes Path(__file__).parent / "logs" / "events.jsonl"
    rather than relying on the default."""
    event_log.configure(REAL_LOGS / "events.jsonl")
    assert REAL_LOGS not in event_log.current_path().parents
    event_log.configure(REAL_LOGS / "events-pid1234.jsonl")
    assert REAL_LOGS not in event_log.current_path().parents


def test_a_tests_own_tmp_path_is_left_alone(tmp_path):
    mine = tmp_path / "mine.jsonl"
    event_log.configure(mine)
    assert event_log.current_path() == mine


def test_a_real_dashboard_broadcast_lands_in_the_sandbox():
    from pokebot.dashboard_server import DashboardServer

    DashboardServer().broadcast(
        "target_caught", species=656, shiny=True, count=42, caught=1)
    path = event_log.current_path()
    assert REAL_LOGS not in path.parents
    assert path.exists() and "target_caught" in path.read_text(
        encoding="utf-8")
