"""
Relaunching Azahar after it crashes mid-hunt.

Azahar 2125.1.1 dies inside its own soft-reset relaunch about once every
650-900 resets (the OpenGL paint follows a freed GPU pointer). A reset
hunt's save is its starting point, so relaunching Azahar on the same ROM
is a slow soft reset -- this module does that, and nothing here may
start a real process or touch the real emulator.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot import azahar_recover as ar  # noqa: E402
from pokebot import platform_utils  # noqa: E402

#: The shape Qt writes it in: entries with commas are quoted.
INI = ('[UI]\nPaths\\recentFiles="D:/3ds/Pokemon Ultra Moon/Pokemon Ultra '
       'Moon (USA) (En,Ja,Fr,De,Es,It,Zh,Ko)-decrypted.3ds", "D:/3ds/Pokemon '
       'X/Pokemon X (USA) (En,Ja,Fr,De,Es,It,Ko)-decrypted.3ds", D:/3ds/'
       'crystal/Pokemon Crystal (0004000000172800)-decrypted.cci\n')


def test_recent_files_parses_qts_quoting():
    files = ar.recent_files(INI)
    assert files == [
        "D:/3ds/Pokemon Ultra Moon/Pokemon Ultra Moon (USA) "
        "(En,Ja,Fr,De,Es,It,Zh,Ko)-decrypted.3ds",
        "D:/3ds/Pokemon X/Pokemon X (USA) (En,Ja,Fr,De,Es,It,Ko)"
        "-decrypted.3ds",
        "D:/3ds/crystal/Pokemon Crystal (0004000000172800)-decrypted.cci",
    ]


def test_game_names_become_file_words():
    assert ar.game_words("UltraMoon") == "Ultra Moon"
    assert ar.game_words("AlphaSapphire") == "Alpha Sapphire"
    assert ar.game_words("X") == "X"


def test_the_rom_is_the_recent_file_naming_the_game(tmp_path):
    um = tmp_path / "Pokemon Ultra Moon (USA) (En,Ja)-decrypted.3ds"
    x = tmp_path / "Pokemon X (USA)-decrypted.3ds"
    xy = tmp_path / "Pokemon XY Guide.3ds"          # must not match "X"
    for f in (um, x, xy):
        f.write_bytes(b"")
    ini = f'Paths\\recentFiles="{xy}", "{um}", "{x}"\n'
    assert ar.rom_for("UltraMoon", ini) == str(um)
    assert ar.rom_for("X", ini) == str(x)
    assert ar.rom_for("UltraSun", ini) is None


def test_a_recent_file_that_no_longer_exists_is_skipped(tmp_path):
    ini = 'Paths\\recentFiles="D:/gone/Pokemon Ultra Moon (USA).3ds"\n'
    assert ar.rom_for("UltraMoon", ini) is None


def test_restarts_are_bounded_per_window():
    r = ar.Recovery("azahar.exe", "game.3ds", max_restarts=2, window_s=60)
    r.restarts.extend([100.0, 110.0])
    assert not r.allowed(120.0), "a crash loop must not be survived forever"
    assert r.allowed(175.0), "old restarts age out of the window"


class Ctx:
    def __init__(self, attach):
        self.rpc = types.SimpleNamespace(attach_to_pokemon_game=attach)
        self.input = types.SimpleNamespace(_azahar_hwnd=999)
        self.config = {}
        self.stopped = False

    def should_stop(self):
        return self.stopped


@pytest.fixture
def no_real_processes(monkeypatch):
    launched = []

    class Proc:
        pid = 5150

    def popen(args, **kw):
        launched.append(args)
        return Proc()

    monkeypatch.setattr(ar.subprocess, "Popen", popen)
    monkeypatch.setattr(ar, "process_alive", lambda pid: False)
    monkeypatch.setattr(ar.time, "sleep", lambda s: None)
    monkeypatch.setattr(ar, "dismiss_dialogs", lambda *a, **k: [])
    monkeypatch.setattr(platform_utils, "ensure_dpi_aware", lambda: "test")
    return launched


def test_revive_relaunches_on_the_rom_and_reattaches(no_real_processes,
                                                    monkeypatch):
    monkeypatch.setattr(platform_utils, "find_azahar_hwnd", lambda *a: 77)
    ctx = Ctx(lambda: (11, 0x00040000001B5100, "UltraMoon"))
    r = ar.Recovery(r"C:\Azahar\azahar.exe", r"D:\um.3ds", pid=9016,
                    dialog_watch=0)

    assert r.revive(ctx) is True
    assert no_real_processes == [[r"C:\Azahar\azahar.exe", r"D:\um.3ds"]]
    assert ctx.input._azahar_hwnd == 0, "the dead window's handle was kept"
    assert r.pid == 5150


def test_revive_waits_for_the_game_not_just_the_window(no_real_processes,
                                                      monkeypatch):
    """The window comes up before the game is running: keep trying."""
    monkeypatch.setattr(platform_utils, "find_azahar_hwnd", lambda *a: 77)
    tries = {"n": 0}

    def attach():
        tries["n"] += 1
        if tries["n"] < 3:
            raise RuntimeError("no Pokémon process yet")
        return 11, 0x00040000001B5100, "UltraMoon"

    r = ar.Recovery("azahar.exe", "um.3ds", dialog_watch=0)
    assert r.revive(Ctx(attach)) is True
    assert tries["n"] == 3


def test_revive_gives_up_when_the_game_never_comes_back(no_real_processes,
                                                       monkeypatch):
    monkeypatch.setattr(platform_utils, "find_azahar_hwnd", lambda *a: 0)
    clock = {"t": 0.0}

    def monotonic():
        clock["t"] += 5.0
        return clock["t"]

    monkeypatch.setattr(ar.time, "monotonic", monotonic)
    r = ar.Recovery("azahar.exe", "um.3ds", boot_timeout=30)
    assert r.revive(Ctx(lambda: None)) is False


def test_a_crashed_process_that_lingers_is_ended_first(no_real_processes,
                                                       monkeypatch):
    """Two Azahars would both answer RPC on the same port."""
    ended = []
    monkeypatch.setattr(ar, "process_alive", lambda pid: pid == 9016
                        and not ended)
    monkeypatch.setattr(ar, "terminate", lambda pid: ended.append(pid))
    clock = {"t": 0.0}

    def monotonic():
        clock["t"] += 1.0
        return clock["t"]

    monkeypatch.setattr(ar.time, "monotonic", monotonic)
    monkeypatch.setattr(platform_utils, "find_azahar_hwnd", lambda *a: 77)
    r = ar.Recovery("azahar.exe", "um.3ds", pid=9016, dialog_watch=0)
    assert r.revive(Ctx(lambda: (11, 1, "UltraMoon"))) is True
    assert ended == [9016]


# ----------------------------------------------------------------------
# The update dialog: found live, blocks every posted key until answered
# ----------------------------------------------------------------------
def test_the_update_dialog_is_answered_with_ignore(monkeypatch):
    clicks = []
    monkeypatch.setattr(ar, "windows_of", lambda pid: [
        (100, "Azahar 2125.1.1 | Pokémon Ultra Moon"),
        (200, "Update Available")])
    monkeypatch.setattr(ar, "post_click",
                        lambda hwnd, x, y: clicks.append((hwnd, x, y)))
    assert ar.dismiss_dialogs(4242, main_hwnd=100) == ["Update Available"]
    assert clicks == [(200, *ar.STARTUP_DIALOGS["Update Available"])]
    x, y = ar.STARTUP_DIALOGS["Update Available"]
    assert x > 0.5 and y > 0.5, "Ignore is the right-hand button"


def test_an_unknown_dialog_is_reported_once_and_never_clicked(monkeypatch,
                                                              caplog):
    clicks = []
    monkeypatch.setattr(ar, "windows_of", lambda pid: [
        (100, "Azahar"), (300, "Delete all save data?")])
    monkeypatch.setattr(ar, "post_click",
                        lambda *a: clicks.append(a))
    seen = set()
    with caplog.at_level("WARNING"):
        assert ar.dismiss_dialogs(4242, 100, seen) == []
        assert ar.dismiss_dialogs(4242, 100, seen) == []
    assert clicks == []
    assert caplog.text.count("Delete all save data?") == 1


def test_a_dialog_arriving_after_the_attach_is_still_answered(
        no_real_processes, monkeypatch):
    """The update check is a network round trip: the game can be up and
    attached before the dialog shows."""
    monkeypatch.setattr(platform_utils, "find_azahar_hwnd", lambda *a: 77)
    clock = {"t": 0.0}

    def monotonic():
        clock["t"] += 1.0
        return clock["t"]

    monkeypatch.setattr(ar.time, "monotonic", monotonic)
    answered = []

    def dismiss(pid, main, seen=None):
        if clock["t"] > 8 and not answered:
            answered.append(clock["t"])
            return ["Update Available"]
        return []

    monkeypatch.setattr(ar, "dismiss_dialogs", dismiss)
    r = ar.Recovery("azahar.exe", "um.3ds", dialog_watch=15)
    assert r.revive(Ctx(lambda: (11, 1, "UltraMoon"))) is True
    assert answered, "stopped watching before the dialog arrived"


def test_recovery_can_be_switched_off():
    ctx = types.SimpleNamespace(config={"azahar": {"restart_on_crash":
                                                   False}})
    assert ar.Recovery.for_hunt(ctx) is None


def test_no_rom_found_means_no_recovery_and_says_so(monkeypatch, caplog,
                                                    tmp_path):
    monkeypatch.setattr(platform_utils, "find_azahar_hwnd", lambda *a: 77)
    monkeypatch.setattr(ar, "window_pid", lambda hwnd: 9016)
    monkeypatch.setattr(ar, "exe_of", lambda pid: r"C:\Azahar\azahar.exe")
    ini = tmp_path / "qt-config.ini"
    ini.write_text("Paths\\recentFiles=\n", encoding="utf-8")
    monkeypatch.setattr(ar, "qt_config_path", lambda: ini)
    ctx = types.SimpleNamespace(
        config={}, rpc=types.SimpleNamespace(
            attached_title_id=0x00040000001B5100))
    with caplog.at_level("WARNING"):
        assert ar.Recovery.for_hunt(ctx) is None
    assert "crash recovery OFF" in caplog.text
