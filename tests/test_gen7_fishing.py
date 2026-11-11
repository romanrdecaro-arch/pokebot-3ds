"""
Gen 7 fishing, as the loop was asked for:

    A to cast; A until the battle starts (that is the hook)
    nothing -> B, B, then A to cast again
    judge it: shiny -> catch (or stop, on_target: stop); else run

The fake pond follows the live Ultra Moon run (2026-11-11): the bite
leaves nothing in memory, and the wild record is written only once the
hook lands and the battle starts.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pokebot.modes import gen7_encounter as g7  # noqa: E402
from pokebot.modes import gen7_fishing as gf  # noqa: E402
from test_gen7_encounter import Ctx, FakeInput, Pk  # noqa: E402

BASE = 0x3002F9A0
FAST = {"fish_cast_settle": 0.05}
#: A presses after the cast before the "!" -- earlier ones do nothing.
BITE_AFTER = 5


class Pond:
    """A casts; the next queued wild (None: nothing bites) bites
    BITE_AFTER presses later, and an A then starts the battle and writes
    its record. B reels in. In battle Down, A runs; Y, A, A and 15 B
    catch."""

    def __init__(self, ctx, wilds):
        self.ctx, self.queue = ctx, list(wilds)
        self.records = [Pk(1, ot="Roman")]
        self.state, self.line, self.fish, self.since = "field", False, None, 0
        self.read = self.bs = self.casts = 0
        #: How many inputs had been sent when each battle started.
        self.battle_at: list[int] = []

    def process(self):
        ev = self.ctx.input.events
        for i in range(self.read, len(ev)):
            b = ev[i]
            if self.state == "field":
                if b == "A" and not self.line:
                    if not self.queue:
                        self.ctx.request_stop("out of wilds")
                        continue
                    self.line, self.since = True, 0
                    self.fish, self.casts = self.queue.pop(0), self.casts + 1
                elif b == "A":
                    self.since += 1
                    if self.fish is not None and self.since >= BITE_AFTER:
                        self.records.append(self.fish)   # written now
                        self.state, self.line = "menu", False
                        self.battle_at.append(i + 1)
                elif b == "B":
                    self.line = False
            elif self.state == "menu" and ev[i - 1:i + 1] == ["DpadDown", "A"]:
                self.state = "field"
            elif self.state == "menu" and ev[i - 2:i + 1] == ["Y", "A", "A"]:
                self.state, self.bs = "throw", 0
            elif self.state == "throw" and b == "B":
                self.bs += 1
                if self.bs == 15:
                    self.state = "field"
        self.read = len(ev)

    def names(self):
        self.process()
        return {"field": {"FieldRo"}, "menu": {"Battle", "Bag"},
                "throw": {"Battle"}}[self.state]

    def new(self, seen):
        return [(BASE + i, p) for i, p in enumerate(self.records)
                if not p.ot_name and p.encryption_key not in seen]


def run_pond(monkeypatch, wilds, config=None, generation=7):
    ctx = Ctx(generation)
    ctx.input = FakeInput()
    ctx.config = {"random_encounters": {**FAST, **(config or {})}}
    pond = Pond(ctx, wilds)
    monkeypatch.setattr(g7.Modules, "names", lambda self: pond.names())
    monkeypatch.setattr(gf, "scan_windows",
                        lambda ctx, w, k: list(enumerate(pond.records)))
    monkeypatch.setattr(gf, "find_wild",
                        lambda ctx, w, seen, t: pond.new(seen))
    monkeypatch.setattr(gf, "ensure_targets_dir", lambda: None)
    monkeypatch.setattr(gf, "_focus_if_needed", lambda ctx: None)
    monkeypatch.setattr(g7, "_balls_left", lambda ctx: None)
    reported = []
    monkeypatch.setattr(g7, "_report_encounter",
                        lambda c, p, a, n, via: reported.append(p.encryption_key))
    gf.run(ctx)
    return ctx, reported, pond


def test_a_bite_seen_only_as_the_battle_is_hooked_judged_and_run_from(monkeypatch):
    ctx, reported, pond = run_pond(monkeypatch, [None, Pk(10), Pk(11, shiny=True)],
                                   config={"on_target": "stop"})
    ev = ctx.input.events
    assert reported == [10, 11]
    assert ctx.stop_reason == "shiny"
    first_b = ev.index("B")
    assert ev[first_b:first_b + 2] == ["B", "B"]     # nothing: two B
    assert set(ev[:first_b]) == {"A"}                # cast + blind hook presses
    assert ev[pond.battle_at[0]:pond.battle_at[0] + 3] == ["B", "DpadDown", "A"]
    # the shiny's battle: not one press after it started
    assert len(ev) == pond.battle_at[-1]


def test_no_press_ever_lands_in_a_battle_before_it_is_judged(monkeypatch):
    ctx, _, pond = run_pond(monkeypatch, [Pk(10), Pk(12)])
    ev = ctx.input.events
    for at in pond.battle_at:
        assert ev[at] == "B", f"{ev[at]} pressed straight into the battle"


def test_a_shiny_is_caught_then_fishing_goes_on(monkeypatch):
    ctx, reported, pond = run_pond(monkeypatch, [Pk(11, shiny=True), Pk(12)])
    ev = ctx.input.events
    assert reported == [11, 12]
    i = ev.index("Y")
    assert ev[i:i + 18] == ["Y", "A", "A"] + ["B"] * 15
    assert ev[i + 18] == "A"           # straight back to casting
    assert pond.casts == 2
    assert [e for e, _ in ctx.dashboard.sent] == ["target_caught"]


def test_the_gen6_rod_button_is_never_used(monkeypatch):
    ctx, _, _ = run_pond(monkeypatch, [None, Pk(10)],
                         config={"fish_cast_button": "Y", "fish_clear_taps": 4})
    ev = ctx.input.events
    assert "Y" not in ev
    first_b = ev.index("B")
    assert ev[first_b:first_b + 3] == ["B", "B", "A"]


def test_not_a_gen7_game_stops_at_once(monkeypatch):
    ctx, _, _ = run_pond(monkeypatch, [Pk(10)], generation=6)
    assert ctx.stop_reason == "not a Gen 7 game"
    assert ctx.input.events == []
