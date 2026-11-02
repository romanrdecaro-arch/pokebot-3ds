"""The watchdog's two judgments: never restart over a shiny, and read why
a bot it started stopped."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import watchdog  # noqa: E402


def enc(shiny=False):
    return {"type": "encounter", "shiny": shiny}


def test_hunting_normally_is_not_a_pending_shiny():
    assert not watchdog.shiny_pending([enc(), enc(), {"type": "status"}])


def test_a_hit_with_no_catch_after_it_is_pending():
    assert watchdog.shiny_pending([enc(), enc(True), {"type": "target_hit"},
                                   {"type": "session_start"}])


def test_a_caught_shiny_is_not_pending():
    assert not watchdog.shiny_pending([enc(True), {"type": "target_hit"},
                                       {"type": "target_caught"}])


def test_a_bot_that_died_on_the_shiny_encounter_itself_is_pending():
    assert watchdog.shiny_pending([enc(), enc(True)])


def test_an_old_uncaught_shiny_is_not_pending_once_the_hunt_went_on():
    assert not watchdog.shiny_pending([enc(True), {"type": "target_hit"}, enc()])


def test_a_live_bot_just_after_a_shiny_is_catching_it():
    hit = [enc(True), {"type": "target_hit", "ts": 1000.0}]
    assert watchdog.catching(hit, alive=True, now=1030.0)     # 30 s in
    assert not watchdog.catching(hit, alive=False, now=1030.0)  # bot gone
    assert not watchdog.catching(hit, alive=True,
                                 now=1000.0 + watchdog.CATCH_S + 1)


def test_the_mode_is_read_from_a_command_line():
    cmd = r"C:\py\python.exe -u D:\bot\run.py --mode observe --game USUM-USA-1.2"
    assert watchdog.mode_of(cmd) == "observe"
    assert watchdog.mode_of("--mode gen7_encounter --target shiny") == "gen7_encounter"
    assert watchdog.mode_of("run.py") is None


def test_the_stop_reason_is_read_from_the_last_session_end():
    end = {"type": "session_end", "ts": 200.0, "reason": "started in a battle"}
    assert watchdog.end_reason([enc(), end], since=100.0) == "started in a battle"
    assert watchdog.end_reason([end, enc()], since=100.0) is None  # hunt went on
    assert watchdog.end_reason([end], since=300.0) is None  # before the watch


def test_someone_stopping_the_bot_is_not_undone():
    assert "dashboard".startswith(watchdog.LEAVE)
    assert "interrupted".startswith(watchdog.LEAVE)
    assert not "crashed: RPCError: timed out".startswith(watchdog.LEAVE)


def test_breeding_stops_a_restart_would_make_worse_are_left():
    for reason in ("decline failed", "nursery lady not answering",
                   "egg not shiny"):
        assert reason.startswith(watchdog.LEAVE)
    assert not "catch failed".startswith(watchdog.LEAVE)
