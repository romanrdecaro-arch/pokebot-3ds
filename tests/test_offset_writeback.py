"""
Tests for the config write-back that ``find_offsets --save-config`` uses.

This function edits the user's real ``config.yaml`` in place, so the
things worth pinning down are: it must not lose anything it did not
mean to change, and it must never leave a half-written file behind.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pokebot.find_offsets import write_offsets_to_config  # noqa: E402

SAMPLE = """\
# pokebot-3ds configuration
mode: observe

offsets:
  # PartyOffset (X/Y): 6 live party slots.
  party_base: 0x00000000
  party_stride: 484
  in_battle_flag: 0x0        # TODO: not yet located (battle-state u8)

random_encounters:
  movement: horizontal
"""


def _write(tmp_path: Path, text: str = SAMPLE) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_updates_requested_key(tmp_path: Path) -> None:
    p = _write(tmp_path)
    written = write_offsets_to_config(p, {"party_base": 0x08CE1CF8})
    assert written == ["party_base"]
    assert "party_base: 0x08ce1cf8" in p.read_text(encoding="utf-8").lower()


def test_leaves_other_keys_and_comments_intact(tmp_path: Path) -> None:
    p = _write(tmp_path)
    write_offsets_to_config(p, {"party_base": 0x08CE1CF8})
    text = p.read_text(encoding="utf-8")
    assert "# pokebot-3ds configuration" in text
    assert "# PartyOffset (X/Y): 6 live party slots." in text
    assert "party_stride: 484" in text
    assert "movement: horizontal" in text
    assert "mode: observe" in text


def test_preserves_trailing_comment_on_a_replaced_line(tmp_path: Path) -> None:
    """Regression: the TODO note used to be silently deleted."""
    p = _write(tmp_path)
    write_offsets_to_config(p, {"in_battle_flag": 0x1234})
    line = next(ln for ln in p.read_text(encoding="utf-8").splitlines()
                if "in_battle_flag" in ln)
    assert "0x00001234" in line
    assert "# TODO: not yet located (battle-state u8)" in line


def test_appends_unknown_key_inside_the_block(tmp_path: Path) -> None:
    p = _write(tmp_path)
    written = write_offsets_to_config(p, {"foe_base": 0x08800000})
    assert "foe_base" in written
    text = p.read_text(encoding="utf-8")
    offsets_idx = text.index("offsets:")
    foe_idx = text.index("foe_base")
    rand_idx = text.index("random_encounters:")
    assert offsets_idx < foe_idx < rand_idx, "new key escaped the block"


def test_result_still_parses(tmp_path: Path) -> None:
    yaml = pytest.importorskip("yaml")
    p = _write(tmp_path)
    write_offsets_to_config(p, {"party_base": 0x08CE1CF8,
                                "foe_base": 0x08800000})
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert cfg["offsets"]["party_base"] == 0x08CE1CF8
    assert cfg["offsets"]["foe_base"] == 0x08800000
    assert cfg["mode"] == "observe"


def test_missing_file_is_a_noop(tmp_path: Path) -> None:
    assert write_offsets_to_config(tmp_path / "nope.yaml", {"a": 1}) == []


def test_no_temp_files_left_behind(tmp_path: Path) -> None:
    p = _write(tmp_path)
    write_offsets_to_config(p, {"party_base": 0x08CE1CF8})
    leftovers = [f.name for f in tmp_path.iterdir() if f.name != "config.yaml"]
    assert leftovers == []


def test_failed_write_leaves_original_intact(tmp_path: Path,
                                             monkeypatch) -> None:
    """If the rename dies, the old config must still be readable."""
    p = _write(tmp_path)
    original = p.read_text(encoding="utf-8")

    import pokebot.find_offsets as fo

    def boom(src, dst):
        raise OSError("simulated failure mid-replace")

    monkeypatch.setattr(fo.os, "replace", boom)
    with pytest.raises(OSError):
        write_offsets_to_config(p, {"party_base": 0x08CE1CF8})

    assert p.read_text(encoding="utf-8") == original
    leftovers = [f.name for f in tmp_path.iterdir() if f.name != "config.yaml"]
    assert leftovers == [], "temp file survived a failed write"
