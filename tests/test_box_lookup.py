"""
Tests for finding a caught Pokémon in the PC boxes.

A catch made with a full party goes straight to a box, where the party
scan cannot see it — 45 of 71 wild exports on disk were pre-capture
records for exactly this reason. The search has to stay BOUNDED: a
sweep wide enough to find it anywhere is what crashed Azahar early in
this project's life.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot.modes import box_lookup  # noqa: E402


PARTY_ADDR = 0x08C79DA8          # the LiveHeX reference for X/Y
BOX1_SLOT1 = 0x08C861C8


class FakeMon:
    def __init__(self, pid, species=659):
        self.pid = pid
        self.species = species
        self.source_address = 0


class FakeCtx:
    def __init__(self):
        self.calls: list[tuple[int, int]] = []

    def should_stop(self):
        return False


def scan_returning(*records, ctx=None):
    """A _scan_owned stand-in that records the window it was asked for."""
    def fake(c, lo, hi, ot):
        c.calls.append((lo, hi))
        return list(records)
    return fake


# ----------------------------------------------------------------------
# The search window
# ----------------------------------------------------------------------
def test_the_window_covers_box_one():
    lo, hi = box_lookup.box_window(PARTY_ADDR)
    assert lo < BOX1_SLOT1 < hi


def test_the_window_covers_the_last_box():
    """31 boxes x 30 slots x 232 bytes past box 1 slot 1."""
    last = BOX1_SLOT1 + 31 * 30 * 232
    lo, hi = box_lookup.box_window(PARTY_ADDR)
    assert last < hi


def test_the_window_starts_after_the_party():
    """Scanning back over the party wastes reads and re-finds it."""
    lo, _ = box_lookup.box_window(PARTY_ADDR)
    assert lo > PARTY_ADDR


def test_the_window_stays_bounded():
    """A sweep this wide is what crashed Azahar before."""
    lo, hi = box_lookup.box_window(PARTY_ADDR)
    assert hi - lo <= 0x50000


def test_the_window_is_relative_so_relocation_is_survivable():
    """Azahar moves the save block; the party-to-box delta does not."""
    a = box_lookup.box_window(PARTY_ADDR)
    b = box_lookup.box_window(PARTY_ADDR + 0x100000)
    assert b[0] - a[0] == 0x100000


def test_a_low_party_address_cannot_produce_a_negative_window():
    lo, hi = box_lookup.box_window(0)
    assert lo >= 0 and hi > lo


# ----------------------------------------------------------------------
# Finding the catch
# ----------------------------------------------------------------------
def test_a_boxed_catch_is_found(monkeypatch):
    ctx = FakeCtx()
    mon = FakeMon(0xB199AC8E)
    monkeypatch.setattr(box_lookup, "_scan_owned",
                        scan_returning((BOX1_SLOT1, mon)))

    found = box_lookup.find_in_boxes(ctx, 0xB199AC8E, 659, "Roman",
                                     PARTY_ADDR)

    assert found is mon
    assert found.source_address == BOX1_SLOT1


def test_the_address_is_attached_so_the_exporter_can_re_read_it(monkeypatch):
    ctx = FakeCtx()
    mon = FakeMon(0xB199AC8E)
    monkeypatch.setattr(box_lookup, "_scan_owned",
                        scan_returning((0x08C90000, mon)))

    found = box_lookup.find_in_boxes(ctx, 0xB199AC8E, 659, "Roman",
                                     PARTY_ADDR)

    assert found.source_address == 0x08C90000


def test_a_different_pid_is_not_returned(monkeypatch):
    ctx = FakeCtx()
    monkeypatch.setattr(box_lookup, "_scan_owned",
                        scan_returning((BOX1_SLOT1, FakeMon(0x11111111))))

    assert box_lookup.find_in_boxes(ctx, 0xB199AC8E, 659, "Roman",
                                    PARTY_ADDR) is None


def test_a_matching_pid_of_the_wrong_species_is_rejected(monkeypatch):
    ctx = FakeCtx()
    monkeypatch.setattr(
        box_lookup, "_scan_owned",
        scan_returning((BOX1_SLOT1, FakeMon(0xB199AC8E, species=25))))

    assert box_lookup.find_in_boxes(ctx, 0xB199AC8E, 659, "Roman",
                                    PARTY_ADDR) is None


def test_empty_boxes_return_nothing(monkeypatch):
    ctx = FakeCtx()
    monkeypatch.setattr(box_lookup, "_scan_owned", scan_returning())

    assert box_lookup.find_in_boxes(ctx, 0xB199AC8E, 659, "Roman",
                                    PARTY_ADDR) is None


def test_the_scan_is_asked_for_the_box_window(monkeypatch):
    ctx = FakeCtx()
    monkeypatch.setattr(box_lookup, "_scan_owned", scan_returning())

    box_lookup.find_in_boxes(ctx, 1, 2, "Roman", PARTY_ADDR)

    assert ctx.calls == [box_lookup.box_window(PARTY_ADDR)]


# ----------------------------------------------------------------------
# Failure must never end a hunt
# ----------------------------------------------------------------------
def test_no_party_anchor_means_no_scan(monkeypatch):
    """Without the party located there is nothing to measure from."""
    ctx = FakeCtx()
    monkeypatch.setattr(box_lookup, "_scan_owned", scan_returning())

    assert box_lookup.find_in_boxes(ctx, 1, 2, "Roman", 0) is None
    assert ctx.calls == []


def test_a_failing_scan_is_swallowed(monkeypatch):
    ctx = FakeCtx()

    def boom(c, lo, hi, ot):
        raise RuntimeError("RPC timeout")

    monkeypatch.setattr(box_lookup, "_scan_owned", boom)

    assert box_lookup.find_in_boxes(ctx, 1, 2, "Roman", PARTY_ADDR) is None
