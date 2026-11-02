"""Every hunt leaves a session_end event saying why it stopped: the
watchdog reads it, and so does anyone asking why a hunt went quiet."""
from __future__ import annotations

import queue
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pokebot.bot as bot_mod  # noqa: E402


class Dash:
    def __init__(self):
        self.inbox, self.sent = queue.Queue(), []

    def broadcast(self, kind, **fields):
        self.sent.append((kind, fields))

    def stop(self):
        pass


def run(monkeypatch, mode):
    bot = bot_mod.Bot({"mode": "fake"})
    dash = Dash()

    def setup():
        bot.dashboard = dash
        bot.game = SimpleNamespace(display="game", verified=True,
                                   offsets=SimpleNamespace(party_base=0,
                                                           foe_base=0))
    monkeypatch.setattr(bot, "_connect", setup)
    for name in ("_start_dashboard", "_start_input", "_load_target",
                 "_install_signal_handlers"):
        monkeypatch.setattr(bot, name, lambda: None)
    monkeypatch.setitem(bot_mod.MODES, "fake", mode)
    try:
        bot.run()
    finally:
        bot._stop_evt.set()             # ends the dashboard thread
    return [f for k, f in dash.sent if k == "session_end"]


def test_the_first_stop_reason_is_the_one_kept(monkeypatch):
    def mode(ctx):
        ctx.request_stop("catch failed")
        ctx.request_stop("dashboard")
    assert run(monkeypatch, mode) == [{"mode": "fake",
                                       "reason": "catch failed"}]


def test_a_crash_says_what_crashed(monkeypatch):
    dash_ends = []

    def mode(ctx):
        dash_ends.append(ctx.dashboard)
        raise RuntimeError("boom")
    with pytest.raises(RuntimeError):
        run(monkeypatch, mode)
    ends = [f for k, f in dash_ends[0].sent if k == "session_end"]
    assert ends == [{"mode": "fake", "reason": "crashed: RuntimeError: boom"}]
