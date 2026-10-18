"""
scripts/rng_preview.py: list upcoming shiny advances from the live MT.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import rng_preview  # noqa: E402
from pokebot import rng6  # noqa: E402
from pokebot.citra_rpc import RPCError  # noqa: E402

TID, SID = 31182, 25810
X_TITLE, SUN_TITLE = 0x0004000000055D00, 0x0004000000164800


class SteppingRPC:
    """An MT object at a fixed heap address that can be moved on."""

    def __init__(self, mt, step=0):
        self.mt = mt.copy()
        self.step = step
        self.ptr = 0x08C52848

    def read_u32(self, addr):
        assert addr == rng6.MAIN_MT_PTR_XY
        return self.ptr

    def read(self, addr, size):
        assert addr == self.ptr
        block = struct.pack(f"<{rng6.MT_N + 1}I", self.mt.index, *self.mt.words)
        return block[:size]

    def advance(self, n=None):
        for _ in range(self.step if n is None else n):
            self.mt.next()


class FakeCitra(SteppingRPC):
    """Stands in for CitraRPC in main()."""

    def __init__(self, title_id=X_TITLE, fail=None):
        super().__init__(rng6.GFMT.from_seed(5), step=150)
        self.title_id, self.fail, self.closed = title_id, fail, False

    def attach_to_pokemon_game(self):
        if self.fail:
            raise self.fail
        return 1, self.title_id, "Sun" if self.title_id == SUN_TITLE else "X"

    def close(self):
        self.closed = True


_REAL_MEASURE = rng_preview.measure


def no_wait_measure(rpc, interval=1.0, **_):
    """measure() with the sleep replaced by stepping the fake MT."""
    return _REAL_MEASURE(rpc, interval, sleep=lambda s: rpc.advance(),
                         clock=iter([0.0, interval]).__next__)


def test_measure_counts_advances_over_the_interval():
    rpc = SteppingRPC(rng6.GFMT.from_seed(5), step=250)
    mt, rate = no_wait_measure(rpc, interval=0.5)
    assert rate == pytest.approx(500.0)
    assert mt == rpc.mt


def test_measure_reports_zero_when_paused():
    rpc = SteppingRPC(rng6.GFMT.from_seed(5), step=0)
    _, rate = no_wait_measure(rpc)
    assert rate == 0


def test_measure_refuses_reads_no_stepping_links():
    rpc = SteppingRPC(rng6.GFMT.from_seed(5))

    def reseed(_):
        rpc.mt = rng6.GFMT.from_seed(6)
    with pytest.raises(rng6.RNGReadError, match="not within"):
        rng_preview.measure(rpc, sleep=reseed, clock=iter([0.0, 1.0]).__next__)


@pytest.mark.parametrize("rolls", [1, 3])
def test_windows_cover_exactly_the_shiny_offsets(rolls):
    mt = rng6.GFMT.from_seed(0xBEEF)
    spec = rng6.GenSpec(pid_rolls=rolls)
    windows = list(rng_preview.shiny_windows(mt, spec, TID, SID, 30000))
    shiny = {o: p for o, p in rng6.scan(mt, spec, TID, SID, 30000) if p.shiny}
    covered = {o for first, last, _ in windows for o in range(first, last + 1)}
    assert windows and covered == set(shiny)
    for first, last, p in windows:
        assert last - first < rolls
        assert all(shiny[o].pid == p.pid for o in range(first, last + 1))


def test_window_still_open_at_the_last_offset_is_reported():
    mt = rng6.GFMT.from_seed(0xBEEF)
    spec = rng6.GenSpec(pid_rolls=3)
    first, _ = rng6.find_shiny(mt, spec, TID, SID, 100000)
    windows = list(rng_preview.shiny_windows(mt, spec, TID, SID, first + 1))
    assert windows == [(first, first, windows[0][2])]


def test_report_lists_windows_and_eta():
    mt = rng6.GFMT.from_seed(0xBEEF)
    lines = rng_preview.report(mt, 100.0, rng6.GenSpec(pid_rolls=3),
                               TID, SID, 30000, show=2)
    assert lines[0] == f"MT index {mt.index}; advancing 100/s"
    assert len(lines) == 3
    assert all("PID" in ln and "s)" in ln for ln in lines[1:])


@pytest.mark.parametrize("rate,text", [(None, "advance rate unknown"),
                                       (0, "not advancing (paused?)")])
def test_report_without_a_usable_rate_has_no_eta(rate, text):
    mt = rng6.GFMT.from_seed(0xBEEF)
    lines = rng_preview.report(mt, rate, rng6.GenSpec(pid_rolls=3), TID, SID, 30000, 1)
    assert text in lines[0]
    assert "s)" not in lines[1]


def test_report_says_when_nothing_is_in_range():
    mt = rng6.GFMT.from_seed(0xBEEF)
    first, _ = rng6.find_shiny(mt, rng6.GenSpec(), TID, SID, 100000)
    lines = rng_preview.report(mt, None, rng6.GenSpec(), TID, SID, first, show=3)
    assert lines[1].strip() == f"no shiny in the next {first} advances"


@pytest.mark.parametrize("argv", [
    ["--tid", "70000", "--sid", "1"],
    ["--tid", "1", "--sid", "-1"],
    ["--tid", "1", "--sid", "1", "--advances", "0"],
    ["--tid", "1", "--sid", "1", "--show", "0"],
    ["--tid", "1", "--sid", "1", "--rolls", "0"],
    ["--tid", "1", "--sid", "1", "--nature", "25"],
])
def test_rejects_bad_arguments(argv):
    with pytest.raises(SystemExit):
        rng_preview.parse_args(argv)


@pytest.fixture
def fake(monkeypatch):
    def install(**kwargs):
        citra = FakeCitra(**kwargs)
        monkeypatch.setattr(rng_preview, "CitraRPC", lambda **_: citra)
        monkeypatch.setattr(rng_preview, "measure", no_wait_measure)
        return citra
    return install


def test_main_prints_the_preview(fake, capsys):
    citra = fake()
    assert rng_preview.main(["--tid", str(TID), "--sid", str(SID), "--rolls", "3"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"Pokemon X, TID {TID} SID {SID}, 3 PID roll(s)")
    assert "advancing 150/s" in out
    assert citra.closed


def test_main_refuses_a_game_that_is_not_xy(fake, capsys):
    citra = fake(title_id=SUN_TITLE)
    assert rng_preview.main(["--tid", "1", "--sid", "1"]) == 1
    assert "X/Y only" in capsys.readouterr().err
    assert citra.closed


@pytest.mark.parametrize("error", [RPCError("no reply"),
                                   rng6.RNGReadError("not the MT")])
def test_main_turns_read_failures_into_an_exit_code(fake, capsys, error):
    citra = fake(fail=error)
    assert rng_preview.main(["--tid", "1", "--sid", "1"]) == 1
    assert str(error) in capsys.readouterr().err
    assert citra.closed


def test_main_ctrl_c_exits_130(fake):
    citra = fake(fail=KeyboardInterrupt())
    assert rng_preview.main(["--tid", "1", "--sid", "1"]) == 130
    assert citra.closed
