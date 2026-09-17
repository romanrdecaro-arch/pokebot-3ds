"""
Tests for the fast foe check.

The full window scan is 128 RPC round trips and ~32,000 parse
attempts. X/Y's bite window is about 170 ms of wall time at the speed
this hunt runs Azahar, so a check that expensive cannot land inside
it -- the fishing loop was not hooking late, it was often looking
after the window had already shut.

So the behaviour that matters here is the cost: the common case must
be ONE read, and it must still notice when the wild slot moves.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot.modes import foe_watch  # noqa: E402


FOE_BASE = 0x08800000
FOE_LEN = 0x20000
SLOT = FOE_BASE + 0x3ECC


class FakeMon:
    def __init__(self, key):
        self.encryption_key = key


class Recorder:
    """Counts what each check actually costs."""

    def __init__(self):
        self.reads = 0
        self.scans = 0
        self.at: dict[int, FakeMon] = {}

    def read_pk6_at(self, ctx, addr):
        self.reads += 1
        return self.at.get(addr)

    def scan_nonparty(self, ctx, base, length, party_keys):
        self.scans += 1
        return sorted(self.at.items())


def make(monkeypatch, seen=None, party=None, **kw):
    rec = Recorder()
    monkeypatch.setattr(foe_watch, "read_pk6_at", rec.read_pk6_at)
    monkeypatch.setattr(foe_watch, "scan_nonparty", rec.scan_nonparty)
    w = foe_watch.FoeWatch(object(), FOE_BASE, FOE_LEN,
                           party or set(), seen if seen is not None else set(),
                           **kw)
    return w, rec


# ----------------------------------------------------------------------
# Finding a new wild
# ----------------------------------------------------------------------
def test_a_new_wild_is_found_by_the_full_scan_first_time(monkeypatch):
    w, rec = make(monkeypatch)
    rec.at[SLOT] = FakeMon(0xAAAA)

    assert w.check() is True
    assert rec.scans == 1


def test_the_address_is_remembered_after_the_first_find(monkeypatch):
    w, rec = make(monkeypatch)
    rec.at[SLOT] = FakeMon(0xAAAA)
    w.check()
    assert w.hot == SLOT


def test_the_second_check_costs_one_read_not_a_scan(monkeypatch):
    """The whole point: a poll must be cheap enough to repeat."""
    w, rec = make(monkeypatch)
    rec.at[SLOT] = FakeMon(0xAAAA)
    w.check()
    before_scans, before_reads = rec.scans, rec.reads

    assert w.check() is True

    assert rec.scans == before_scans, "paid for a full scan again"
    assert rec.reads == before_reads + 1


def test_nothing_new_reports_false(monkeypatch):
    w, rec = make(monkeypatch)
    assert w.check() is False


def test_an_already_seen_wild_is_not_new(monkeypatch):
    seen = {0xAAAA}
    w, rec = make(monkeypatch, seen=seen)
    rec.at[SLOT] = FakeMon(0xAAAA)

    assert w.check() is False


def test_a_party_member_is_never_a_wild(monkeypatch):
    w, rec = make(monkeypatch, party={0xBEEF})
    rec.at[SLOT] = FakeMon(0xBEEF)

    assert w.check() is False


def test_the_hot_slot_turning_over_is_caught_by_one_read(monkeypatch):
    """A fresh wild in the same buffer is the common case."""
    seen = set()
    w, rec = make(monkeypatch, seen=seen)
    rec.at[SLOT] = FakeMon(0xAAAA)
    w.check()
    seen.add(0xAAAA)
    assert w.check() is False

    rec.at[SLOT] = FakeMon(0xBBBB)          # next bite, same slot
    before = rec.scans

    assert w.check() is True
    assert rec.scans == before, "should not have needed a full scan"


# ----------------------------------------------------------------------
# Re-acquiring when the slot moves
# ----------------------------------------------------------------------
def test_a_moved_slot_is_re_found_by_the_periodic_full_scan(monkeypatch):
    seen = set()
    w, rec = make(monkeypatch, seen=seen, full_every=3)
    rec.at[SLOT] = FakeMon(0xAAAA)
    w.check()
    seen.add(0xAAAA)

    rec.at.clear()
    moved = FOE_BASE + 0x9000
    rec.at[moved] = FakeMon(0xCCCC)

    results = [w.check() for _ in range(4)]

    assert True in results, "never re-acquired the moved slot"
    assert w.hot == moved


def test_the_full_scan_is_not_run_on_every_poll(monkeypatch):
    w, rec = make(monkeypatch, full_every=10)
    rec.at[SLOT] = FakeMon(0xAAAA)
    w.check()                                # the acquiring scan
    w.seen.add(0xAAAA)
    before = rec.scans

    for _ in range(5):
        w.check()

    assert rec.scans == before, "polling fell back to full scans"


def test_full_every_cannot_be_zero(monkeypatch):
    """Zero would mean a full scan every single poll."""
    w, _ = make(monkeypatch, full_every=0)
    assert w.full_every >= 1


# ----------------------------------------------------------------------
# Robustness
# ----------------------------------------------------------------------
def test_an_unreadable_slot_does_not_crash_the_poll(monkeypatch):
    w, rec = make(monkeypatch)
    rec.at[SLOT] = FakeMon(0xAAAA)
    w.check()

    rec.at.clear()                           # read now returns None
    assert w.check() is False


def test_stats_are_reportable(monkeypatch):
    w, rec = make(monkeypatch)
    w.check()
    assert "full scan" in w.stats()
