"""
Regression tests for bugs found in the audit.

Each test names the failure it prevents. These are the pure-logic
halves of the fixes — the parts reachable without a running emulator.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pokebot.dashboard_server import _strip_controls          # noqa: E402
from pokebot.input_driver import BUTTON_NAMES, _check_button  # noqa: E402
from pokebot.targets import Rule, Target, target_from_dict    # noqa: E402


# --------------------------------------------------------------------
# encounter.py rebound its walk-direction variable to a PK6 address
# --------------------------------------------------------------------

def test_button_check_rejects_an_address() -> None:
    """The crash was TypeError deep inside getattr; make it legible.

    encounter.run held the walk directions in `a, b`, then a `for a, p
    in ordered:` loop rebound `a` to an int address. The next walk step
    passed that int as a button name and killed the run.
    """
    with pytest.raises(ValueError) as exc:
        _check_button(0x8800000)
    assert "unknown button" in str(exc.value)
    assert "int" in str(exc.value)


def test_button_check_accepts_every_real_bind() -> None:
    for name in BUTTON_NAMES:
        _check_button(name)


def test_walk_directions_are_not_single_letter_names() -> None:
    """Guard the root cause: `a, b` is what made the collision possible."""
    source = (Path(__file__).resolve().parents[1]
              / "pokebot" / "modes" / "encounter.py").read_text(encoding="utf-8")
    assert "    a, b = _BTN[movement]" not in source
    assert "walk_a, walk_b = _BTN[movement]" in source


# --------------------------------------------------------------------
# targets.py: config mistakes used to fail silently at hunt time
# --------------------------------------------------------------------

def test_typo_in_rule_key_is_rejected() -> None:
    """`shinny: true` used to build a rule matching EVERY Pokemon,
    halting the hunt on the first encounter."""
    with pytest.raises(ValueError) as exc:
        target_from_dict({"rules": [{"shinny": True}]})
    assert "shinny" in str(exc.value)


def test_scalar_species_is_wrapped_not_iterated() -> None:
    """`species: 25` used to raise TypeError inside Rule.matches,
    which aborted _report_encounter and skipped the shiny save."""
    t = target_from_dict({"rules": [{"species": 25}]})
    assert t.rules[0].species == [25]


def test_scalar_nature_is_not_split_into_characters() -> None:
    """set("Adamant") is a set of 7 letters, so it matched nothing."""
    t = target_from_dict({"rules": [{"nature": "Adamant"}]})
    assert t.rules[0].nature == ["Adamant"]


def test_unknown_stat_name_is_rejected() -> None:
    """`iv_min: {Speed: 31}` silently never matched — the key is Spe."""
    with pytest.raises(ValueError) as exc:
        target_from_dict({"rules": [{"iv_min": {"Speed": 31}}]})
    assert "Speed" in str(exc.value)


def test_scalar_iv_min_is_rejected() -> None:
    with pytest.raises(ValueError):
        target_from_dict({"rules": [{"iv_min": 31}]})


def test_bad_mode_is_rejected() -> None:
    with pytest.raises(ValueError):
        target_from_dict({"mode": "both", "rules": []})


@pytest.mark.parametrize("preset", [
    {"mode": "all", "rules": [{}]},                       # "any"
    {"mode": "all", "rules": [{"shiny": True}]},          # "shiny"
    {"mode": "all", "rules": [{"perfect_iv_count_min": 6}]},
    {"mode": "all", "rules": [{"shiny": True, "perfect_iv_count_min": 4}]},
])
def test_run_py_presets_still_build(preset: dict) -> None:
    """The CLI presets must survive the new validation."""
    assert isinstance(target_from_dict(preset), Target)


def test_empty_rule_still_means_match_anything() -> None:
    """`--target any` relies on a deliberately empty rule; only a
    *typo* should raise, not an intentionally unconstrained rule."""
    t = target_from_dict({"mode": "all", "rules": [{}]})
    assert t.rules == [Rule()]


# --------------------------------------------------------------------
# dashboard_server: nicknames come from RAM and reach a terminal raw
# --------------------------------------------------------------------

def test_ansi_escapes_are_stripped_from_terminal_output() -> None:
    assert "\x1b" not in _strip_controls("Pika\x1b[2J\x1b[Hchu")


def test_nickname_cannot_forge_a_second_line() -> None:
    """A newline in a nickname could fake an EVENT: line to the GUI."""
    assert "\n" not in _strip_controls('a\nEVENT: {"type":"fake"}')


def test_ordinary_text_survives_stripping() -> None:
    assert _strip_controls("Pokémon ♦ ひらがな") == "Pokémon ♦ ひらがな"
    assert _strip_controls("col\tsep") == "col\tsep"


# --------------------------------------------------------------------
# horde/fishing wrote different defaults into one shared config dict
# --------------------------------------------------------------------

def test_horde_and_fishing_do_not_mutate_the_shared_config() -> None:
    from dataclasses import dataclass, field

    import pokebot.modes.fishing as fishing
    import pokebot.modes.horde as horde

    @dataclass
    class FakeCtx:
        config: dict = field(default_factory=dict)

    captured = {}
    for mod, name in ((horde, "horde"), (fishing, "fishing")):
        original = {"random_encounters": {"movement": "horizontal"}}
        ctx = FakeCtx(config=original)
        snapshot = {"random_encounters": dict(original["random_encounters"])}
        mod._encounter_run = lambda c, _n=name: captured.__setitem__(_n, c.config)
        mod.run(ctx)
        assert original == snapshot, f"{name} mutated the caller's config"

    # Each mode still got ITS own defaults, not the other's.
    assert captured["horde"]["random_encounters"]["idle_action"] == "sweet_scent"
    assert captured["fishing"]["random_encounters"]["idle_action"] == "fish"
    # ...and a user-set value still wins over the mode default.
    assert captured["horde"]["random_encounters"]["movement"] == "horizontal"


# --------------------------------------------------------------------
# run.py crashed on a present-but-empty config section
# --------------------------------------------------------------------

def test_cli_override_survives_an_empty_config_section(tmp_path) -> None:
    """A section written as bare `input:` parses to None, not {}.

    `config.setdefault("input", {})` then handed back that None and the
    subscript raised TypeError, so any CLI override crashed against a
    perfectly legal config file. The section must be present-but-empty
    for this to reproduce — a missing section never hit the bug.
    """
    cfg = tmp_path / "config.yaml"
    cfg.write_text("mode: observe\ninput:\nrandom_encounters:\n",
                   encoding="utf-8")

    # Confirm the precondition: the sections really do parse to None.
    from pokebot.config_io import load_config
    loaded = load_config(cfg)
    assert loaded["input"] is None
    assert loaded["random_encounters"] is None

    import run as run_mod

    class _Stop(Exception):
        pass

    parsed: dict = {}

    def fake_bot(config, **kwargs):
        parsed.update(config)
        raise _Stop

    import pokebot.bot as bot_mod
    original = bot_mod.Bot
    bot_mod.Bot = fake_bot
    try:
        with pytest.raises(_Stop):
            run_mod.main(["--dry-run", "--movement", "vertical",
                          "--config", str(cfg)])
    finally:
        bot_mod.Bot = original

    assert parsed["input"]["dry_run"] is True
    assert parsed["random_encounters"]["movement"] == "vertical"
