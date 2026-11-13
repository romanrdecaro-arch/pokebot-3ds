"""
Gen 7 fishing, as the loop runs live (Ultra Sun, 2026-11-12):

    A to cast; nothing until the bite counter moves; A once (the hook)
    nothing, or a late A -> B, B, then A to cast again
    judge it: shiny -> catch (or stop, on_target: stop); else run

The fake pond follows the game: the cast adds 0x52 to the counter, the
"!" adds 0x13, an A before the "!" reels the line in, and the wild
record is written only once the hook lands and the battle starts.
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
#: The waits are wall-clock; the fake pond needs a fraction of them.
FAST = {"gen7_bite_wait": 0.2}
#: Counter reads after the cast before the "!".
BITE_AFTER = 5


class Pond:
    """A casts (+0x52); the next queued wild (None: nothing bites) bites
    BITE_AFTER counter reads later (+0x13), and an A then starts the
    battle and writes its record -- unless that cast is in ``late``, when
    the A comes too late and only reels in. An A before the bite reels
    in too. B clears. In battle Down, A runs; Y, A, A and 15 B catch."""

    def __init__(self, ctx, wilds, late=(), swallow=0):
        self.ctx, self.queue, self.late = ctx, list(wilds), set(late)
        self.swallow = swallow               # cast presses the game ignores
        self.records = [Pk(1, ot="Roman")]
        self.state, self.line, self.fish = "field", None, None
        self.counter, self.reads, self.bitten = 0xFFFFFFF0, 0, False  # wraps
        self.read = self.bs = self.casts = 0
        self.early = 0                       # A presses before the bite
        #: How many inputs had been sent when each battle started.
        self.battle_at: list[int] = []

    def process(self):
        ev = self.ctx.input.events
        for i in range(self.read, len(ev)):
            b = ev[i]
            if self.state == "field":
                if b == "A" and self.line is None and self.swallow:
                    self.swallow -= 1            # still fading in from a battle
                elif b == "A" and self.line is None:
                    if not self.queue:
                        self.ctx.request_stop("out of wilds")
                        continue
                    self.line, self.reads, self.bitten = "out", 0, False
                    self.counter = (self.counter + gf.CAST_RISE) & 0xFFFFFFFF
                    self.fish, self.casts = self.queue.pop(0), self.casts + 1
                elif b == "A" and self.line == "out":
                    if not self.bitten:
                        self.early += 1
                    elif self.casts not in self.late:
                        self.records.append(self.fish)   # written now
                        self.state = "menu"
                        self.battle_at.append(i + 1)
                    self.line = "text"           # "too slow" / reeled in
                elif b == "B":
                    self.line = None
            elif self.state == "menu" and ev[i - 1:i + 1] == ["DpadDown", "A"]:
                self.state, self.line = "field", None
            elif self.state == "menu" and ev[i - 2:i + 1] == ["Y", "A", "A"]:
                self.state, self.bs = "throw", 0
            elif self.state == "throw" and b == "B":
                self.bs += 1
                if self.bs == 15:
                    self.state, self.line = "field", None
        self.read = len(ev)

    def read_u32(self, addr):
        assert addr == gf.BITE_COUNTER["USUM-USA-1.2"]
        self.process()
        if self.line == "out" and self.fish is not None and not self.bitten:
            self.reads += 1
            if self.reads >= BITE_AFTER:
                self.counter = (self.counter + 0x13) & 0xFFFFFFFF
                self.bitten = True
        return self.counter

    def names(self):
        self.process()
        return {"field": {"FieldRo"}, "menu": {"Battle", "Bag"},
                "throw": {"Battle"}}[self.state]

    def new(self, seen):
        return [(BASE + i, p) for i, p in enumerate(self.records)
                if not p.ot_name and p.encryption_key not in seen]


def run_pond(monkeypatch, wilds, config=None, generation=7, late=(),
             swallow=0):
    ctx = Ctx(generation)
    ctx.input = FakeInput()
    ctx.config = {"random_encounters": {**FAST, **(config or {})}}
    pond = Pond(ctx, wilds, late, swallow)
    ctx.rpc = pond
    monkeypatch.setattr(gf, "HOOK_WAIT", 0.05)
    monkeypatch.setattr(gf, "QUIET", 0.01)
    monkeypatch.setattr(gf, "RECAST", 0.05)
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


def test_waits_for_the_bite_hooks_once_judges_and_runs(monkeypatch):
    ctx, reported, pond = run_pond(monkeypatch, [None, Pk(10), Pk(11, shiny=True)],
                                   config={"on_target": "stop"})
    ev = ctx.input.events
    assert reported == [10, 11]
    assert ctx.stop_reason == "shiny"
    assert pond.early == 0                       # never reeled in early
    assert ev[:3] == ["A", "B", "B"]             # nothing bit: cast, clear
    assert ev[3:5] == ["A", "A"]                 # cast, one hook press
    assert ev[pond.battle_at[0]:pond.battle_at[0] + 3] == ["B", "DpadDown", "A"]
    # the shiny's battle: not one press after it started
    assert len(ev) == pond.battle_at[-1]


def test_a_hook_with_no_battle_clears_and_casts_again(monkeypatch):
    ctx, reported, pond = run_pond(monkeypatch, [Pk(10), Pk(12)], late={1})
    ev = ctx.input.events
    assert ev[:5] == ["A", "A", "B", "B", "A"]   # missed: clear, cast again
    assert reported == [12]


def test_a_swallowed_cast_is_pressed_again_not_waited_out(monkeypatch):
    ctx, reported, pond = run_pond(monkeypatch, [Pk(10)], swallow=1,
                                   config={"gen7_bite_wait": 1.0})
    ev = ctx.input.events
    assert ev[:3] == ["A", "A", "A"]             # lost cast, cast, hook
    assert reported == [10] and pond.early == 0


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


def test_sun_moon_stops_instead_of_reeling_in_blind():
    ctx = Ctx()
    ctx.game.key = "SM-USA-1.2"
    gf.run(ctx)
    assert ctx.stop_reason == "no bite signal for this game"
    assert ctx.input.events == []
