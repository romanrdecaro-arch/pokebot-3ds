"""
Gen 7 fishing, as the loop was asked for:

    A to cast; a wild detected -> A to hook it
    nothing -> B, B, then A to cast again
    judge it: shiny -> catch (or stop, on_target: stop); else run
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
FAST = {"fish_cast_settle": 0.02}


class Input(FakeInput):
    def target_alive(self):
        return True


class Pond:
    """A fishing spot. A casts; the next queued wild (None: nothing bites)
    lands in the foe window at once, and A hooks it into a battle. B
    reels the line in. In battle Down, A runs; Y, A, A and 15 B catch."""

    def __init__(self, ctx, wilds):
        self.ctx, self.queue = ctx, list(wilds)
        self.records = [Pk(1, ot="Roman")]
        self.state, self.line, self.bite, self.read, self.bs = "field", False, False, 0, 0
        self.seen = set()
        self.casts = 0

    def process(self):
        ev = self.ctx.input.events
        for i in range(self.read, len(ev)):
            b = ev[i]
            if self.state == "field":
                if b == "A" and self.line and self.bite:
                    self.state, self.line, self.bite = "menu", False, False
                elif b == "A" and not self.line:
                    if not self.queue:
                        self.ctx.request_stop("out of wilds")
                        continue
                    self.line, self.casts = True, self.casts + 1
                    wild = self.queue.pop(0)
                    if wild is not None:
                        self.records.append(wild)
                        self.bite = True
                elif b == "B":
                    self.line = self.bite = False
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
    ctx.input = Input()
    ctx.config = {"random_encounters": {**FAST, **(config or {})}}
    pond = Pond(ctx, wilds)

    class Watch:
        def __init__(self, ctx, base, length, party, seen, windows=None):
            self.seen, self.hot, self.slots = seen, 0, []

        def check(self, full=False):
            pond.process()
            new = pond.new(self.seen)
            if new:
                self.hot = new[0][0]
            return bool(new)

    monkeypatch.setattr(g7.Modules, "names", lambda self: pond.names())
    monkeypatch.setattr(gf, "FoeWatch", Watch)
    monkeypatch.setattr(gf, "scan_windows",
                        lambda ctx, w, k: list(enumerate(pond.records)))
    monkeypatch.setattr(gf, "read_pk6_at",
                        lambda ctx, a: pond.records[a - BASE])
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


def test_nothing_is_b_b_then_a_bite_is_hooked_judged_and_run_from(monkeypatch):
    ctx, reported, _ = run_pond(monkeypatch, [None, Pk(10), Pk(11, shiny=True)],
                                config={"on_target": "stop"})
    assert ctx.input.events == [
        "A", "B", "B",                 # cast, nothing: two B
        "A", "A",                      # cast, bite: hook
        "B", "DpadDown", "A",          # not shiny: run
        "A", "A"]                      # cast, bite: hook -- shiny, then nothing
    assert reported == [10, 11]
    assert ctx.stop_reason == "shiny"


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
    assert "Y" not in ctx.input.events
    assert ctx.input.events[:3] == ["A", "B", "B"]


def test_not_a_gen7_game_stops_at_once(monkeypatch):
    ctx, _, _ = run_pond(monkeypatch, [Pk(10)], generation=6)
    assert ctx.stop_reason == "not a Gen 7 game"
    assert ctx.input.events == []
