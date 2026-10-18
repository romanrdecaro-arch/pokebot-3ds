"""
Reading the keybinds out of Azahar's ACTIVE input profile.

Azahar keeps the current profile as a 0-based index (``profile=0``), but
QSettings writes the profile array 1-based (``profiles\\1\\...``), so the
profile in use lives under ``profiles\\<index + 1>``. Reading
``profiles\\<index>`` instead meant a fresh install (``profile=0``) found
nothing and fell back to the defaults, and anyone with a second profile
selected got the FIRST profile's keys -- a keyboard map the emulator was
not listening to.

The other half: a profile mapped to a controller. Azahar ignores the
keyboard for any button bound to a gamepad, so every key the bot posts is
dropped -- and the bot said only "qt-config.ini not found", which sent
people looking in the wrong place (issue #3).
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot import azahar_config  # noqa: E402

SDL = '"button:0,engine:sdl,guid:0300ce536f0e00008001000072056800,port:0"'
KEYBOARD_CIRCLE = (
    '"down:code$016777237$1engine$0keyboard,engine:analog_from_button,'
    'left:code$016777234$1engine$0keyboard,modifier:code$068$1engine$0'
    'keyboard,modifier_scale:0.500000,right:code$016777236$1engine$0'
    'keyboard,up:code$016777235$1engine$0keyboard"')
STICK_CIRCLE = ('"axis_x:0,axis_y:1,deadzone:0.100000,engine:sdl,'
                'guid:0300ce536f0e00008001000072056800,port:0"')


def key(code: int) -> str:
    return f'"code:{code},engine:keyboard"'


def profile(number: int, name: str, **binds: str) -> list[str]:
    lines = [f"profiles\\{number}\\name={name}"]
    lines += [f"profiles\\{number}\\{k}={v}" for k, v in binds.items()]
    return lines


@pytest.fixture
def write_config(tmp_path, monkeypatch):
    """Write a qt-config.ini where the bot will look for it."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.delenv("AZAHAR_USER_DIR", raising=False)
    monkeypatch.delenv("CITRA_USER_DIR", raising=False)
    # Never the real emulator's folder, if one happens to be running.
    monkeypatch.setattr(azahar_config, "_running_azahar_user_dir",
                        lambda: None)
    path = tmp_path / "Azahar" / "config" / "qt-config.ini"
    path.parent.mkdir(parents=True)

    def write(*controls: str) -> Path:
        path.write_text("[Controls]\n" + "\n".join(controls) + "\n[UI]\n",
                        encoding="utf-8")
        return path
    return write


def two_profiles(index_line: str | None):
    """Profile 1 maps A to J, profile 2 maps A to K."""
    lines = [index_line] if index_line else []
    return (lines + profile(1, "Default", button_a=key(0x4A))
            + profile(2, "Second", button_a=key(0x4B))
            + ["profiles\\size=2"])


# ----------------------------------------------------------------------
# Which profile is active
# ----------------------------------------------------------------------
def test_profile_zero_is_the_first_profile(write_config):
    write_config(*two_profiles("profile=0"))
    binds = azahar_config.load_active_profile_binds()
    assert binds["A"] == "j"
    assert binds["_profile"] == "Default"


def test_profile_one_is_the_second_profile(write_config):
    write_config(*two_profiles("profile=1"))
    binds = azahar_config.load_active_profile_binds()
    assert binds["A"] == "k"
    assert binds["_profile"] == "Second"


def test_no_profile_line_means_the_first_profile(write_config):
    """Azahar's own default for the index is 0."""
    write_config(*two_profiles(None))
    assert azahar_config.load_active_profile_binds()["A"] == "j"


def test_an_out_of_range_index_is_clamped_like_azahar_does(write_config):
    write_config(*two_profiles("profile=7"))
    assert azahar_config.load_active_profile_binds()["A"] == "k"


def test_no_profiles_at_all_falls_back_to_the_defaults(write_config):
    write_config("profile=0")
    assert azahar_config.load_active_profile_binds() is None


def test_no_profiles_size_falls_back_to_the_defaults(write_config):
    """Azahar reads that as zero profiles and uses its legacy keys."""
    write_config("profile=0", *profile(1, "Default", button_a=key(0x4A)))
    assert azahar_config.load_active_profile_binds() is None


@pytest.mark.parametrize("index", ["profile=-1", "profile=abc",
                                   "profile="])
def test_a_junk_index_means_the_first_profile(write_config, index):
    write_config(*two_profiles(index))
    assert azahar_config.load_active_profile_binds()["A"] == "j"


# ----------------------------------------------------------------------
# Which file
# ----------------------------------------------------------------------
def write_ini(path: Path, number: int, code: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[Controls]\nprofile=0\n"
                    + "\n".join(profile(1, f"p{number}",
                                        button_a=key(code)))
                    + "\nprofiles\\size=1\n", encoding="utf-8")


def test_the_running_portable_azahar_beats_a_leftover_appdata_file(
        write_config, tmp_path, monkeypatch):
    write_ini(tmp_path / "Azahar" / "config" / "qt-config.ini", 1, 0x4A)
    portable = tmp_path / "portable" / "user"
    write_ini(portable / "config" / "qt-config.ini", 2, 0x4B)
    monkeypatch.setattr(azahar_config, "_running_azahar_user_dir",
                        lambda: portable)
    assert azahar_config.load_active_profile_binds()["A"] == "k"


def test_an_explicit_user_dir_beats_appdata(write_config, tmp_path,
                                            monkeypatch):
    write_ini(tmp_path / "Azahar" / "config" / "qt-config.ini", 1, 0x4A)
    user_dir = tmp_path / "elsewhere"
    write_ini(user_dir / "config" / "qt-config.ini", 2, 0x4B)
    monkeypatch.setenv("AZAHAR_USER_DIR", str(user_dir))
    assert azahar_config.load_active_profile_binds()["A"] == "k"


# ----------------------------------------------------------------------
# Buttons the bot cannot press
# ----------------------------------------------------------------------
def test_a_controller_profile_is_reported_not_mistaken_for_no_config(
        write_config):
    write_config("profile=1",
                 *profile(1, "Default", button_a=key(0x41)),
                 *profile(2, "cont", button_a=SDL, button_b=SDL,
                          button_l=SDL, button_r=SDL, button_start=SDL),
                 "profiles\\size=2")
    binds = azahar_config.load_active_profile_binds()
    assert binds is not None, "a controller profile is not a missing config"
    assert "A" not in binds
    assert set(binds["_unusable"]) >= {"A", "B", "L", "R", "Start"}
    assert "controller" in binds["_unusable"]["A"]


def test_a_keyboard_key_the_bot_cannot_send_is_reported(write_config):
    write_config("profile=0",
                 *profile(1, "Default", button_a=key(0x3B)),  # ';'
                 "profiles\\size=1")
    binds = azahar_config.load_active_profile_binds()
    assert "A" not in binds
    assert "A" in binds["_unusable"]


def test_an_empty_bind_is_reported_as_not_bound(write_config):
    write_config("profile=0",
                 *profile(1, "Default", button_a=key(0x41),
                          button_select='""'),
                 "profiles\\size=1")
    binds = azahar_config.load_active_profile_binds()
    assert binds["_unusable"] == {"Select": "not bound"}


def test_numpad_enter_is_reported_rather_than_sent_as_return(
        write_config):
    """The bot can only post VK_RETURN, which Qt reports as Key_Return."""
    write_config("profile=0",
                 *profile(1, "Default", button_start=key(0x01000005)),
                 "profiles\\size=1")
    binds = azahar_config.load_active_profile_binds()
    assert "Start" not in binds
    assert "Start" in binds["_unusable"]


def test_a_keyboard_profile_has_nothing_unusable(write_config):
    write_config("profile=0",
                 *profile(1, "Default", button_a=key(0x41),
                          circle_pad=KEYBOARD_CIRCLE),
                 "profiles\\size=1")
    assert azahar_config.load_active_profile_binds()["_unusable"] == {}


# ----------------------------------------------------------------------
# The Circle Pad (USUM walks with it)
# ----------------------------------------------------------------------
def test_circle_pad_keys_come_from_the_profile(write_config):
    remapped = KEYBOARD_CIRCLE.replace("left:code$016777234",
                                       "left:code$074")       # J
    write_config("profile=0",
                 *profile(1, "Default", circle_pad=remapped),
                 "profiles\\size=1")
    binds = azahar_config.load_active_profile_binds()
    assert binds["CircleLeft"] == "j"
    assert binds["CircleUp"] == "up"
    assert binds["CircleDown"] == "down"
    assert binds["CircleRight"] == "right"


def test_a_circle_pad_on_controller_axes_is_reported(write_config):
    """The shape Azahar actually writes for a gamepad profile:
    analog_from_button, with each direction an SDL axis."""
    axes = ('"down:axis$01$1direction$0+$1engine$0sdl$1port$00,'
            'engine:analog_from_button,'
            'left:axis$00$1direction$0-$1engine$0sdl$1port$00,'
            'modifier_scale:0.500000,'
            'right:axis$00$1direction$0+$1engine$0sdl$1port$00,'
            'up:axis$01$1direction$0-$1engine$0sdl$1port$00"')
    write_config("profile=0",
                 *profile(1, "Default", circle_pad=axes),
                 "profiles\\size=1")
    binds = azahar_config.load_active_profile_binds()
    for d in ("CircleUp", "CircleDown", "CircleLeft", "CircleRight"):
        assert d not in binds
        assert binds["_unusable"][d] == "controller (sdl)"


def test_a_half_keyboard_circle_pad_keeps_the_keyboard_half(write_config):
    mixed = KEYBOARD_CIRCLE.replace(
        "up:code$016777235$1engine$0keyboard",
        "up:axis$01$1direction$0-$1engine$0sdl$1port$00")
    write_config("profile=0",
                 *profile(1, "Default", circle_pad=mixed),
                 "profiles\\size=1")
    binds = azahar_config.load_active_profile_binds()
    assert binds["CircleLeft"] == "left"
    assert set(binds["_unusable"]) == {"CircleUp"}


@pytest.mark.parametrize("escaped,plain", [
    ("code$065$1engine$0keyboard", "code:65,engine:keyboard"),
    ("$20", "$0"),          # an escaped '$' followed by a literal 0
    ("$2$0", "$:"),
])
def test_unescape_matches_citras_param_package(escaped, plain):
    assert azahar_config._unescape(escaped) == plain


def test_a_circle_pad_on_a_controller_stick_is_reported(write_config):
    write_config("profile=0",
                 *profile(1, "Default", button_a=key(0x41),
                          circle_pad=STICK_CIRCLE),
                 "profiles\\size=1")
    binds = azahar_config.load_active_profile_binds()
    assert "CircleLeft" not in binds
    assert "controller" in binds["_unusable"]["CircleLeft"]


# ----------------------------------------------------------------------
# What the user is told
# ----------------------------------------------------------------------
def test_the_message_names_the_profile_the_buttons_and_the_fix():
    msg = azahar_config.unusable_binds_message(
        "cont", {"A": "controller (sdl)", "Start": "controller (sdl)"})
    assert "'cont'" in msg
    assert "A" in msg and "Start" in msg
    assert "keyboard" in msg.lower()
    assert "Controls" in msg


def test_no_message_when_every_button_is_on_the_keyboard():
    assert azahar_config.unusable_binds_message("Default", {}) is None


def test_the_bot_logs_a_controller_profile_as_an_error(write_config,
                                                        caplog):
    from pokebot.bot import Bot

    write_config("profile=0",
                 *profile(1, "cont", button_a=SDL, button_start=SDL),
                 "profiles\\size=1")
    bot = Bot.__new__(Bot)
    bot.config = {"input": {"dry_run": True}}
    with caplog.at_level(logging.INFO, logger="pokebot.bot"):
        bot._start_input()
    errors = [r.getMessage() for r in caplog.records
              if r.levelno >= logging.ERROR]
    assert any("'cont'" in m and "controller" in m for m in errors), errors
    assert not any("not found" in r.getMessage() for r in caplog.records)
    assert bot.input.binds.A == "a"    # untouched default, not "_unusable"


def started_bot(write_config, a_bind: str):
    from pokebot.bot import Bot

    write_config("profile=0", *profile(1, "p", button_a=a_bind),
                 "profiles\\size=1")
    bot = Bot.__new__(Bot)
    bot.config = {"input": {"dry_run": True}}
    bot._start_input()
    return bot


@pytest.mark.parametrize("mode", ["usum_starters", "soft_reset",
                                  "encounter", "static_encounter"])
def test_a_pressing_mode_does_not_start_without_an_a_key(write_config,
                                                         mode):
    assert started_bot(write_config, SDL)._refuse_to_press(mode)


@pytest.mark.parametrize("mode", ["observe", "debug", "livehex",
                                  "crystal_observe"])
def test_a_read_only_mode_starts_whatever_the_profile(write_config, mode):
    assert not started_bot(write_config, SDL)._refuse_to_press(mode)


def test_a_keyboard_a_lets_every_mode_start(write_config):
    assert not started_bot(write_config,
                           key(0x41))._refuse_to_press("usum_starters")


def test_read_only_modes_really_press_nothing():
    """The list is a promise; hold the modes to it."""
    import re
    from pokebot.modes import READ_ONLY_MODES

    presses = re.compile(r"\.input\.(tap|hold|soft_reset|combo|"
                         r"move_running|tap_touch|gb_soft_reset)\(")
    for mode in READ_ONLY_MODES:
        src = (REPO / "pokebot" / "modes" / f"{mode}.py").read_text(
            encoding="utf-8")
        assert not presses.search(src), mode
