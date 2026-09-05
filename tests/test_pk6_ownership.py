"""
Tests for exporting a CAUGHT target rather than a wild one.

A wild Pokémon in the foe slot is complete in every stat the hunt cares
about, but it has no owner: the OT name, ball, game version, met
location and trainer memories are written when it is CAUGHT. Exporting
before that produced files PKHeX rejected with "OT Name too short" and
"unable to match an encounter from origin game" -- correct data, no
ownership.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot import pk6_export  # noqa: E402


OT_NAME = slice(0xB0, 0xC8)
BALL = 0xDC
VERSION = 0xDF


def make_record(*, species=659, pid=0xB199AC8E, ot="Roman",
                ball=4, version=25) -> bytes:
    """A 232-byte record with the fields legality actually looks at."""
    d = bytearray(232)
    struct.pack_into("<H", d, 0x08, species)
    struct.pack_into("<I", d, 0x18, pid)
    name = ot.encode("utf-16-le")[:22]
    d[0xB0:0xB0 + len(name)] = name
    d[BALL] = ball
    d[VERSION] = version
    return bytes(d)


class FakeMon:
    def __init__(self, pid, species, addr=0, shiny=True, nickname="Bunnelby"):
        self.pid = pid
        self.species = species
        self.source_address = addr
        self.shiny = shiny
        self.nickname = nickname


# ----------------------------------------------------------------------
# Telling an owned record from a wild one
# ----------------------------------------------------------------------
def test_a_caught_record_is_recognised_as_owned():
    assert pk6_export.is_owned_record(make_record())


def test_a_wild_record_has_no_owner():
    """Exactly the shape the bot was writing: right stats, no owner."""
    wild = make_record(ot="", ball=0, version=0)
    assert not pk6_export.is_owned_record(wild)


@pytest.mark.parametrize("missing", ["ot", "ball", "version"])
def test_any_missing_ownership_field_means_unowned(missing):
    kw = {"ot": "Roman", "ball": 4, "version": 25}
    kw[missing] = "" if missing == "ot" else 0
    assert not pk6_export.is_owned_record(make_record(**kw))


def test_a_truncated_record_is_not_owned():
    assert not pk6_export.is_owned_record(b"\x00" * 10)


# ----------------------------------------------------------------------
# The real files on disk
# ----------------------------------------------------------------------
def _targets(pattern):
    return sorted((REPO / "targets").glob(pattern))


@pytest.mark.skipif(not _targets("*Fennekin*.pk6"),
                    reason="no starter export on disk")
def test_the_starter_exports_are_owned():
    """These are the ones PKHeX accepts — read from the party."""
    for f in _targets("*Fennekin*.pk6"):
        assert pk6_export.is_owned_record(f.read_bytes()), f.name


def _wild_exports():
    return [f for f in _targets("*.pk6")
            if "Fennekin" not in f.name and "Froakie" not in f.name]


@pytest.mark.skipif(not _wild_exports(), reason="no wild export on disk")
def test_the_post_capture_export_produces_legal_wild_files():
    """End-to-end proof on real data.

    Before the fix every wild export was unowned. This asserts at least
    one on disk now carries an owner, which can only happen through the
    post-capture re-export. It deliberately does NOT require all of
    them: a catch that goes to a PC box (full party) cannot be read
    back from the party, and keeps its pre-capture record by design.
    """
    owned = [f for f in _wild_exports()
             if pk6_export.is_owned_record(f.read_bytes())]
    assert owned, ("no wild export on disk has an owner — the "
                   "post-capture re-export is not working")


# ----------------------------------------------------------------------
# Re-exporting after the catch
# ----------------------------------------------------------------------
class FakeCtx:
    def __init__(self, memory: dict):
        self.rpc = self          # read() lives here
        self.memory = memory

    def read(self, addr, size):
        return self.memory.get(addr, b"\x00" * size)[:size]


def _valid(record: bytes) -> bytes:
    """Fix the checksum so save_target_pk6 accepts the record."""
    from pokebot.parser import calc_checksum
    d = bytearray(record)
    struct.pack_into("<H", d, 0x06, calc_checksum(bytes(d)))
    return bytes(d)


def test_the_caught_copy_is_read_from_the_party(tmp_path, monkeypatch):
    monkeypatch.setattr(pk6_export, "TARGETS_DIR", tmp_path)
    owned = _valid(make_record())
    ctx = FakeCtx({0x08CE1CF8: owned})
    mon = FakeMon(0xB199AC8E, 659, addr=0x08CE1CF8)

    path = pk6_export.save_caught_pk6(ctx, mon, [mon])

    assert path is not None
    assert pk6_export.is_owned_record(path.read_bytes())


def test_the_precapture_file_is_removed_once_a_legal_one_exists(
        tmp_path, monkeypatch):
    monkeypatch.setattr(pk6_export, "TARGETS_DIR", tmp_path)
    stale = tmp_path / "shiny_659_Bunnelby_PIDB199AC8E_1.pk6"
    stale.write_bytes(make_record(ot="", ball=0, version=0))

    ctx = FakeCtx({0x08CE1CF8: _valid(make_record())})
    mon = FakeMon(0xB199AC8E, 659, addr=0x08CE1CF8)

    path = pk6_export.save_caught_pk6(ctx, mon, [mon], supersedes=stale)

    assert path is not None and path.exists()
    assert not stale.exists(), "the illegal copy was left behind"


def test_a_catch_that_is_not_in_the_party_keeps_the_precapture_file(
        tmp_path, monkeypatch):
    """A full party sends it to a PC box, which we cannot read."""
    monkeypatch.setattr(pk6_export, "TARGETS_DIR", tmp_path)
    stale = tmp_path / "shiny_659_Bunnelby_PIDB199AC8E_1.pk6"
    stale.write_bytes(make_record(ot="", ball=0, version=0))

    ctx = FakeCtx({})
    mon = FakeMon(0xB199AC8E, 659, addr=0x08CE1CF8)

    path = pk6_export.save_caught_pk6(ctx, mon, [], supersedes=stale)

    assert path is None
    assert stale.exists(), "the only copy was deleted"


def test_a_different_mon_in_the_party_is_not_mistaken_for_the_catch():
    ctx = FakeCtx({})
    wanted = FakeMon(0xB199AC8E, 659)
    someone_else = FakeMon(0x12345678, 25, addr=0x08CE1CF8)

    assert pk6_export.save_caught_pk6(ctx, wanted, [someone_else]) is None


def test_a_party_member_without_an_address_is_skipped():
    ctx = FakeCtx({})
    mon = FakeMon(0xB199AC8E, 659, addr=0)
    assert pk6_export.save_caught_pk6(ctx, mon, [mon]) is None


def test_an_empty_party_does_not_crash():
    ctx = FakeCtx({})
    mon = FakeMon(0xB199AC8E, 659)
    assert pk6_export.save_caught_pk6(ctx, mon, None) is None
