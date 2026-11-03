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


def test_a_stop_line_on_stdin_stops_the_hunt_cleanly(monkeypatch):
    """The launcher's Stop used to kill the bot: no session_end, and a
    held key left down in Azahar. It now writes "stop" to stdin."""
    import io
    monkeypatch.setenv("POKEBOT_STDIN_CONTROL", "1")
    monkeypatch.setattr(bot_mod.sys, "stdin", io.StringIO("noise\nstop\n"))

    def mode(ctx):
        ctx._stop_evt.wait(5)               # the hunt, until told to stop
    ends = run(monkeypatch, mode)
    assert ends == [{"mode": "fake", "reason": "dashboard"}]


def test_the_launcher_asks_before_it_kills(monkeypatch):
    import io
    import subprocess
    import launcher

    class Proc:
        def __init__(self, exits):
            self.exits, self.killed = exits, False
            self.stdin = io.StringIO()

        def poll(self):
            return None

        def wait(self, timeout=None):
            if not self.exits:
                raise subprocess.TimeoutExpired("bot", timeout)

        def terminate(self):
            self.killed = True

    class NowThread:                      # the grace runs inline
        def __init__(self, target, daemon=None):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(launcher.threading, "Thread", NowThread)
    for exits, killed in ((True, False), (False, True)):
        bp = launcher._BotProcess(lambda line: None, lambda code: None)
        bp._proc = proc = Proc(exits)
        bp.stop()
        assert proc.stdin.getvalue() == "stop\n"
        assert proc.killed is killed
