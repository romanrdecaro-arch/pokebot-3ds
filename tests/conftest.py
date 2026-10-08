"""
Shared test fixtures.

The important one is ``tk_session``: exactly ONE Tk root for the whole
test run. Tk does not reliably tolerate a second root being created
after a first is destroyed in the same process — it fails with
`invalid command name "tcl_findLibrary"` — and because the Tk fixtures
treat that as "no display available", the affected tests SKIPPED
instead of running. Silently skipped tests are worse than failing ones,
so the root is created once at session scope and shared.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for path in (str(REPO), str(REPO / "scripts"), str(REPO / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)


@pytest.fixture(scope="session")
def tk_session():
    """One hidden Tk root for the entire run, or skip if there is none."""
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception as exc:                 # genuinely no display
        pytest.skip(f"Tk unavailable: {exc}")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _protect_real_stats(monkeypatch):
    """No test may write the user's real hunt counters.

    ``add_pokemon`` persists on every call, so a test that builds the
    launcher's table would otherwise edit lifetime totals — and a
    synthetic shiny row resets the phase counter, which cannot be
    recovered. Belt and braces alongside the per-test tmp redirects.
    """
    monkeypatch.setenv("POKEBOT_STATS_RO", "1")


@pytest.fixture(autouse=True)
def _protect_real_event_log(monkeypatch, tmp_path_factory):
    """No test may append to the real logs/events.jsonl either.

    It is the record a hunt is audited from afterwards -- "did the bot
    miss a shiny 4,000 encounters ago?" -- and a test broadcasting
    through a real DashboardServer was writing into it: every full run
    left a fake shiny Froakie "caught" in the user's log, interleaved
    with a live hunt's records. Both the path and the default directory
    are redirected, and so is any EXPLICIT path into the real logs/
    folder: run.py builds one itself rather than using the default, and
    test_regressions drives run.main() -- which is how a bare
    "session_start" kept turning up in the user's log after every run.
    A test that configures its own tmp path is left alone.
    """
    from pokebot import event_log

    sandbox = tmp_path_factory.getbasetemp() / "event-log"
    real_logs = Path(event_log.__file__).resolve().parent.parent / "logs"
    real_configure = event_log.configure

    def configure(path=None, enabled=True):
        if path is not None and real_logs in Path(path).resolve().parents:
            path = sandbox / Path(path).name
        return real_configure(path, enabled)

    monkeypatch.setattr(event_log, "_DEFAULT_DIR", sandbox)
    monkeypatch.setattr(event_log, "_path", sandbox / "events.jsonl")
    monkeypatch.setattr(event_log, "configure", configure)
