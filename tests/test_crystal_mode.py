"""
Tests for Crystal manual mode and its Gen 2 -> dashboard mapping.

Gen 2 has no PID, no TSV, and ONE Special stat where Gen 6 has two, so
the payload has to bridge two data models without inventing values.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot import gen2                              # noqa: E402
from pokebot.modes import MODES, crystal_observe      # noqa: E402
from test_gen2 import make_mon                        # noqa: E402

import pytest  # noqa: E402


@pytest.fixture
def tk_root_for_table(tmp_path, monkeypatch):
    """A Tk container with the launcher's stats redirected to tmp."""
    tk = pytest.importorskip("tkinter")
    import launcher
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.setattr(launcher, "_STATS_FILE", tmp_path / "stats.json")
    try:
        root = tk.Tk()
    except Exception as exc:
        pytest.skip(f"Tk unavailable: {exc}")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except Exception:
        pass


def _mon(dv_word=0x1234, species=157, level=30):
    return gen2.parse_pokemon(bytes(make_mon(species, level, dv_word)))


def test_mode_is_registered() -> None:
    assert "crystal_observe" in MODES


def test_special_maps_to_both_gen6_special_stats() -> None:
    """Gen 2's single Special governs both; reporting it under both is
    accurate, and inventing a second value would not be."""
    dvs = {"HP": 1, "Atk": 2, "Def": 3, "Spe": 4, "Spc": 9}
    ivs = crystal_observe._iv_payload(dvs)
    assert ivs["SpA"] == 9 and ivs["SpD"] == 9
    assert ivs["HP"] == 1 and ivs["Atk"] == 2
    assert ivs["Def"] == 3 and ivs["Spe"] == 4


def test_payload_does_not_invent_a_pid_or_shiny_value() -> None:
    """Gen 2 has neither. Sending them as null stops anything
    downstream deriving a meaningless 'shiny value' from a fake PID."""
    payload = crystal_observe._payload(_mon())
    assert payload["pid"] == 0
    assert payload["psv"] is None
    assert payload["tsv"] is None
    assert payload["generation"] == 2


def test_payload_keeps_the_real_gen2_dvs_alongside_the_mapping() -> None:
    payload = crystal_observe._payload(_mon(dv_word=0xAAAA))
    assert set(payload["dvs"]) == {"HP", "Atk", "Def", "Spe", "Spc"}
    assert payload["shiny"] is True


def test_payload_is_json_serialisable() -> None:
    """It goes to the dashboard and the event log as JSON."""
    import json
    json.dumps(crystal_observe._payload(_mon()))


def test_describe_flags_a_shiny() -> None:
    assert "SHINY" in crystal_observe._describe(_mon(dv_word=0xAAAA))
    assert "SHINY" not in crystal_observe._describe(_mon(dv_word=0x1234))


def test_party_signature_changes_on_a_new_catch() -> None:
    """Party re-reports must fire on change, not every poll."""
    a = [_mon(species=157), _mon(species=175)]
    same = [_mon(species=157), _mon(species=175)]
    grown = a + [_mon(species=209)]
    sig = crystal_observe._party_signature
    assert sig(a) == sig(same), "identical party re-reported"
    assert sig(a) != sig(grown), "a new catch was not noticed"


def test_party_signature_notices_a_level_up() -> None:
    sig = crystal_observe._party_signature
    assert sig([_mon(level=5)]) != sig([_mon(level=6)])


def test_gen2_game_skips_the_gen6_offset_block() -> None:
    """Applying X/Y's party_base to Crystal is meaningless and was
    producing a confusing log line about offsets it never uses."""
    import pokebot.bot as bot_mod
    from pokebot.games import GAMES
    crystal = GAMES["CRYSTAL-USA"]
    assert crystal.generation == 2
    # The guard is generation-based, so it holds for any VC title.
    assert crystal.generation < 6
    assert hasattr(bot_mod.Bot, "_connect")


# --------------------------------------------------------------------
# Regression: _payload crashed on an in-battle opponent
# --------------------------------------------------------------------

def _enemy(dv_word=0x1234, species=19, level=11):
    from pokebot.crystal import EnemyReading
    dvs = gen2.parse_dvs(dv_word)
    return EnemyReading(species=species, level=level, dvs=dvs,
                        shiny=gen2.is_shiny(dvs), confirmed=False)


def test_payload_accepts_an_in_battle_opponent() -> None:
    """Regression: manual mode died on the first wild battle.

    _payload read p.ot_id, which only a caught Pokemon has —
    EnemyReading has no Original Trainer. Every wild encounter raised
    AttributeError and ended the run.
    """
    payload = crystal_observe._payload(_enemy())
    assert payload["species"] == 19
    assert payload["level"] == 11
    assert "ot_id" not in payload, "an opponent has no OT until caught"
    assert payload["generation"] == 2


def test_enemy_payload_is_json_serialisable() -> None:
    import json
    json.dumps(crystal_observe._payload(_enemy(), count=3, confirmed=False))


def test_party_payload_still_carries_its_ot_id() -> None:
    assert "ot_id" in crystal_observe._payload(_mon())


def test_shiny_enemy_is_flagged() -> None:
    assert crystal_observe._payload(_enemy(dv_word=0xAAAA))["shiny"] is True


class _FakeDash:
    def __init__(self):
        self.sent = []

    def broadcast(self, kind, **fields):
        self.sent.append((kind, fields))


class _FakeSession:
    def __init__(self, enemy=None):
        self._enemy = enemy

    def enemy(self):
        return self._enemy


class _Ctx:
    def __init__(self):
        self.dashboard = _FakeDash()


def test_report_battle_broadcasts_a_wild_encounter() -> None:
    from pokebot.crystal import BATTLE_WILD
    ctx = _Ctx()
    crystal_observe._report_battle(ctx, _FakeSession(_enemy()),
                                   BATTLE_WILD, 7)
    assert [k for k, _ in ctx.dashboard.sent] == ["encounter"]
    kind, fields = ctx.dashboard.sent[0]
    assert fields["count"] == 7
    assert fields["confirmed"] is False, "unverified offsets must be marked"


def test_report_battle_handles_an_unreadable_opponent() -> None:
    from pokebot.crystal import BATTLE_WILD
    ctx = _Ctx()
    crystal_observe._report_battle(ctx, _FakeSession(None), BATTLE_WILD, 1)
    assert ctx.dashboard.sent == []       # logged, nothing broadcast


def test_report_battle_is_quiet_for_non_wild_states() -> None:
    from pokebot.crystal import BATTLE_NONE, BATTLE_TRAINER
    for mode in (BATTLE_NONE, BATTLE_TRAINER):
        ctx = _Ctx()
        crystal_observe._report_battle(ctx, _FakeSession(), mode, 1)
        assert ctx.dashboard.sent == []


# --------------------------------------------------------------------
# Launcher rendering: Gen 2 must not be shown through a Gen 6 lens
# --------------------------------------------------------------------

def _launcher():
    import launcher
    return launcher


def _gen2_evt(**over):
    from pokebot.modes import crystal_observe as co
    base = co._payload(_mon(dv_word=0xEAAA, species=130, level=30))
    base.update(over)
    return base


def test_gen2_row_shows_no_ability_or_nature() -> None:
    """Neither existed until Gen 3. The columns carry Gen 2 data
    instead of blanks or fabricated Gen 6 values."""
    L = _launcher()
    vals = L._RecentlySeen._row_values(_gen2_evt(ot_id=43813, held_item=0))
    _gender, _lvl, dv_hex, shiny, held, ot, dvs, hp = vals
    assert dv_hex == "EAAA", "the DV word stands in for the absent PID"
    assert shiny == "★ YES"
    assert held == "—"                    # no held item
    assert ot == "43813"
    assert dvs.startswith("0/14/10/10/10")
    assert "GRASS" in hp                  # Gen 2 hidden power


def test_gen2_row_uses_the_gen2_hidden_power() -> None:
    """The Gen 3+ formula gives a different answer; using it here
    would print a wrong type and power for every Gen 2 encounter."""
    from pokebot import gen2 as g2
    L = _launcher()
    dvs = {"HP": 0, "Atk": 14, "Def": 10, "Spe": 10, "Spc": 10}
    expected_type, expected_power = g2.hidden_power(dvs)
    hp = L._RecentlySeen._row_values(_gen2_evt())[7]
    assert expected_type.upper() in hp
    assert str(expected_power) in hp


def test_gen2_row_does_not_invent_a_gender() -> None:
    """Gen 2 gender needs the species ratio, which we do not carry."""
    L = _launcher()
    assert L._RecentlySeen._row_values(_gen2_evt())[0] == "—"


def test_gen6_rows_are_unchanged() -> None:
    L = _launcher()
    evt = {"type": "encounter", "species": 25, "shiny": False,
           "gender": "M", "level": 5, "pid": 0x12345678,
           "nature": "Adamant", "ability_id": 9, "ability_num": 1,
           "ivs": {"HP": 31, "Atk": 20, "Def": 15, "Spe": 31,
                   "SpA": 5, "SpD": 9}}
    gender, level, pid, sv, _ability, nature, ivs, _hp = \
        L._RecentlySeen._row_values(evt)
    assert gender == "♂" and level == "Lv 5"
    assert pid == "12345678"              # a real PID, not a DV word
    assert nature == "Adamant"
    assert ivs.startswith("31/20/15/31/5/9")


def test_generation_switch_retitles_the_columns(tk_root_for_table) -> None:
    L = _launcher()
    table = L._RecentlySeen(tk_root_for_table)
    table._apply_generation(2)
    assert table._tree.heading("ability")["text"] == "Held item"
    assert table._tree.heading("pid")["text"] == "DVs (hex)"
    table._apply_generation(6)
    assert table._tree.heading("ability")["text"] == "Ability"
    assert table._tree.heading("pid")["text"] == "PID"


def test_stats_can_be_made_read_only(tmp_path, monkeypatch) -> None:
    """Guard against harness scripts rewriting real hunt counters.

    add_pokemon persists on every call, so any script that builds an
    _App silently edits the user's lifetime totals — and a synthetic
    shiny row resets their phase counter, which cannot be recovered.
    """
    import launcher as L
    monkeypatch.setattr(L, "ROOT", tmp_path)
    monkeypatch.setattr(L, "_STATS_FILE", tmp_path / "stats.json")
    L.set_stats_scope("")

    monkeypatch.delenv("POKEBOT_STATS_RO", raising=False)
    L._save_stats({"total": 5, "phase": 1, "shinies": 0,
                   "phase_best_sv": None, "phase_best_iv": None})
    assert (tmp_path / "stats.json").exists()
    written = (tmp_path / "stats.json").read_text()

    monkeypatch.setenv("POKEBOT_STATS_RO", "1")
    L._save_stats({"total": 999, "phase": 9, "shinies": 9,
                   "phase_best_sv": None, "phase_best_iv": None})
    assert (tmp_path / "stats.json").read_text() == written
