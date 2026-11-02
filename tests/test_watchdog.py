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


def test_the_last_stop_reason_is_read():
    text = ("10:00 INFO stop requested: flee failed\n"
            "10:05 INFO stop requested: started in a battle\n")
    assert watchdog.last_stop_reason(text) == "started in a battle"
    assert watchdog.last_stop_reason("nothing here") is None


def test_breeding_stops_a_restart_would_make_worse_are_left():
    for reason in ("decline failed", "nursery lady not answering",
                   "egg not shiny"):
        assert reason.startswith(watchdog.LEAVE)
    assert not "catch failed".startswith(watchdog.LEAVE)
