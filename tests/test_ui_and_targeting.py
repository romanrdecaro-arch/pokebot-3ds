"""
Tests for the Treeview encounter table and multi-instance targeting.

The Tk parts skip automatically where no display is available (CI),
but the window-selection logic in ``platform_utils`` is pure and runs
everywhere its platform supports it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot import platform_utils as pu  # noqa: E402


@pytest.fixture(scope="module")
def _tk_session():
    """One hidden Tk root for the module, or skip when there is none.

    Module-scoped deliberately: creating and destroying a Tk root per
    test intermittently fails with `invalid command name
    "tcl_findLibrary"`, which is an artefact of repeated interpreter
    setup rather than anything about the widget under test.
    """
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception as exc:                       # no display / no Tk
        pytest.skip(f"Tk unavailable: {exc}")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except Exception:
        pass


@pytest.fixture
def tk_root(_tk_session):
    """A clean container inside the shared root, torn down per test."""
    import tkinter as tk
    frame = tk.Frame(_tk_session)
    frame.pack()
    yield frame
    try:
        frame.destroy()
    except Exception:
        pass


@pytest.fixture
def launcher_mod(tmp_path, monkeypatch):
    """The launcher module with its stats file redirected to tmp.

    ``add_pokemon`` persists counters on every call, so without this a
    test run silently inflates the user's real hunt statistics —
    lifetime totals and shiny counts they cannot get back.
    """
    pytest.importorskip("tkinter")
    import launcher
    # Patch the two inputs _stats_path() reads, not the function
    # itself, so the scoping logic under test still runs for real.
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.setattr(launcher, "_STATS_FILE", tmp_path / "stats.json")
    launcher.set_stats_scope("")
    yield launcher
    launcher.set_stats_scope("")


# --------------------------------------------------------------------
# Window targeting — the core of running several instances at once
# --------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_target():
    """Never leak a pinned window between tests."""
    yield
    pu.set_target_window(None, None)


def test_target_defaults_to_unpinned() -> None:
    assert pu.get_target_window() == (None, None)


def test_set_and_read_back_target() -> None:
    pu.set_target_window(pid=4321, title_match="Azahar - Pokemon Y")
    assert pu.get_target_window() == (4321, "Azahar - Pokemon Y")


def test_pinned_process_never_falls_back_to_another_window(monkeypatch) -> None:
    """The whole point of pinning.

    With two emulators open, a bot whose window is gone must report
    "not found" rather than grabbing the other instance — otherwise
    two bots drive one game and the other hunt runs unattended.
    """
    windows = [
        {"hwnd": 111, "pid": 10, "title": "Azahar - Pokemon X"},
        {"hwnd": 222, "pid": 20, "title": "Azahar - Pokemon Y"},
    ]
    monkeypatch.setattr(pu, "list_azahar_windows", lambda *a, **k: windows)

    pu.set_target_window(pid=10, title_match="Azahar - Pokemon X")
    assert pu.find_azahar_hwnd() == 111
    pu.set_target_window(pid=20, title_match="Azahar - Pokemon Y")
    assert pu.find_azahar_hwnd() == 222

    # Target process is gone and its title matches nothing -> refuse.
    pu.set_target_window(pid=999, title_match="Azahar - Pokemon Z")
    assert pu.find_azahar_hwnd() == 0


def test_dead_pid_reacquires_by_title(monkeypatch) -> None:
    """Azahar restarting mid-hunt changes its pid but not its title."""
    windows = [
        {"hwnd": 111, "pid": 10, "title": "Azahar - Pokemon X"},
        {"hwnd": 222, "pid": 77, "title": "Azahar - Pokemon Y"},
    ]
    monkeypatch.setattr(pu, "list_azahar_windows", lambda *a, **k: windows)
    pu.set_target_window(pid=20, title_match="Pokemon Y")   # old pid
    assert pu.find_azahar_hwnd() == 222


def test_unpinned_keeps_first_match_behaviour(monkeypatch) -> None:
    """Single-emulator users must see no behaviour change."""
    windows = [{"hwnd": 111, "pid": 10, "title": "Azahar - Pokemon X"}]
    monkeypatch.setattr(pu, "list_azahar_windows", lambda *a, **k: windows)
    pu.set_target_window(None, None)
    assert pu.find_azahar_hwnd() == 111


def test_no_windows_returns_zero(monkeypatch) -> None:
    monkeypatch.setattr(pu, "list_azahar_windows", lambda *a, **k: [])
    assert pu.find_azahar_hwnd() == 0


# --------------------------------------------------------------------
# Click delivery — the bot must not run off with the user's pointer
# --------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_mouse_mode():
    yield
    pu.set_mouse_mode("restore")


def test_default_mode_restores_the_pointer() -> None:
    assert pu.get_mouse_mode() == "restore"


def test_mode_round_trips() -> None:
    for mode in ("post", "cursor", "restore"):
        pu.set_mouse_mode(mode)
        assert pu.get_mouse_mode() == mode


def test_bad_mode_is_rejected() -> None:
    with pytest.raises(ValueError):
        pu.set_mouse_mode("clicky")


def test_click_is_a_noop_without_a_window() -> None:
    assert pu.click_window_at(0, 0.5, 0.5) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows click paths")
def test_restore_mode_puts_the_pointer_back(tk_root) -> None:
    """The hunt clicks RUN on every flee, for hours.

    Leaving the pointer parked on Azahar's RUN button makes the machine
    unusable alongside the bot, and makes two instances fight over it.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    def cursor():
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        return (pt.x, pt.y)

    top = tk_root.winfo_toplevel()
    top.geometry("420x320+280+280")
    top.update()
    hwnd = user32.GetParent(top.winfo_id()) or top.winfo_id()

    pu.set_mouse_mode("restore")
    user32.SetCursorPos(200, 200)
    before = cursor()
    pu.click_window_at(hwnd, 0.5, 0.5, hold_s=0.02)
    assert cursor() == before, "restore mode moved the user's pointer"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows click paths")
def test_post_mode_emits_no_pointer_movement(tk_root) -> None:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    top = tk_root.winfo_toplevel()
    top.update()
    hwnd = user32.GetParent(top.winfo_id()) or top.winfo_id()

    pu.set_mouse_mode("post")
    user32.SetCursorPos(200, 200)
    pu.click_window_at(hwnd, 0.5, 0.5, hold_s=0.02)
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    assert (pt.x, pt.y) == (200, 200)


# --------------------------------------------------------------------
# Per-instance stats scoping
# --------------------------------------------------------------------

def test_stats_scope_separates_instances(launcher_mod) -> None:
    L = launcher_mod
    try:
        L.set_stats_scope("")
        shared = L._stats_path()
        L.set_stats_scope("Azahar - Pokemon X")
        first = L._stats_path()
        L.set_stats_scope("Azahar - Pokemon Y")
        second = L._stats_path()
    finally:
        L.set_stats_scope("")

    assert first != second, "two instances must not share a stats file"
    assert first != shared and second != shared
    # The slug must be filesystem-safe: titles contain spaces and dashes.
    for p in (first, second):
        assert not set(p.name) & set('<>:"/\\|?*')


def test_closest_metric_is_the_xor_not_the_bare_psv(launcher_mod) -> None:
    """"Best SV" used to track the lowest PSV, which says nothing.

    Shininess is psv ^ tsv < 16, so closeness is that xor. A tiny PSV
    against a large TSV is nowhere near shiny, and used to be reported
    as the phase's best result.
    """
    RS = launcher_mod._RecentlySeen
    far = {"psv": 3, "tsv": 60000}        # tiny PSV, nowhere near shiny
    near = {"psv": 3657, "tsv": 3648}     # xor 9 — one roll off shiny
    assert RS._evt_shiny_distance(far) == 3 ^ 60000
    assert RS._evt_shiny_distance(near) == 9
    assert RS._evt_shiny_distance(near) < RS._evt_shiny_distance(far)


def test_closest_metric_needs_a_tsv(launcher_mod) -> None:
    assert launcher_mod._RecentlySeen._evt_shiny_distance(
        {"psv": 100}) is None


def test_v1_stats_discard_the_incomparable_best_sv(
        launcher_mod, tmp_path) -> None:
    """The stored number changed meaning, so it must not carry over.

    A small v1 PSV would otherwise sit there as an unbeatable "best"
    forever, since the v2 metric measures something else entirely.
    """
    import json
    (tmp_path / "stats.json").write_text(json.dumps({
        "total": 7388, "phase": 5868, "shinies": 13,
        "phase_best_sv": 36, "phase_best_iv": 163}))     # no version = v1
    s = launcher_mod._load_stats()
    assert s["total"] == 7388, "unrelated counters must survive"
    assert s["phase"] == 5868
    assert s["phase_best_iv"] == 163
    assert s["phase_best_sv"] is None, "stale v1 metric carried over"


def test_v2_stats_are_kept(launcher_mod, tmp_path) -> None:
    import json
    (tmp_path / "stats.json").write_text(json.dumps({
        "version": 2, "total": 10, "phase": 5, "shinies": 1,
        "phase_best_sv": 42, "phase_best_iv": 100}))
    assert launcher_mod._load_stats()["phase_best_sv"] == 42


def test_slug_is_filesystem_safe(launcher_mod) -> None:
    slug = launcher_mod._slug('Azahar | "X" <v2>/beta')
    assert not set(slug) & set('<>:"/\\|?* ')


# --------------------------------------------------------------------
# Treeview encounter table
# --------------------------------------------------------------------

ENCOUNTER = {
    "type": "encounter", "species": 25, "shiny": False, "gender": "M",
    "level": 5, "pid": 0x12345678, "nature": "Adamant",
    "ivs": {"HP": 31, "Atk": 20, "Def": 15, "Spe": 31, "SpA": 5, "SpD": 9},
    "ability_id": 9, "ability_num": 1,
}


def test_row_values_match_the_column_count(launcher_mod) -> None:
    values = launcher_mod._RecentlySeen._row_values(ENCOUNTER)
    assert len(values) == len(launcher_mod._RecentlySeen._COLUMNS)


def test_row_values_render_the_expected_text(launcher_mod) -> None:
    gender, level, pid, sv, _ability, nature, ivs, hp = \
        launcher_mod._RecentlySeen._row_values(ENCOUNTER)
    assert gender == "♂"
    assert level == "Lv 5"
    assert pid == "12345678"
    assert sv.startswith("—")                 # not shiny
    assert nature == "Adamant"
    assert ivs.startswith("31/20/15/31/5/9")
    assert "(111)" in ivs                     # IV sum still visible
    assert hp                                 # hidden power present


def test_shiny_row_is_marked(launcher_mod) -> None:
    sv = launcher_mod._RecentlySeen._row_values(
        dict(ENCOUNTER, shiny=True))[3]
    assert sv.startswith("★")


def test_iv_order_is_the_pk6_canonical_order(launcher_mod) -> None:
    """Guards the ordering the whole codebase shares."""
    assert launcher_mod._RecentlySeen._IV_ORDER == (
        "HP", "Atk", "Def", "Spe", "SpA", "SpD")


def test_table_caps_rows_and_widget_count_stays_flat(
        tk_root, launcher_mod) -> None:
    """The reason for the Treeview: widgets must not grow with rows."""
    table = launcher_mod._RecentlySeen(tk_root)
    tk_root.update()

    def widget_count(w):
        return 1 + sum(widget_count(c) for c in w.winfo_children())

    before = widget_count(tk_root)
    for i in range(table.MAX_ROWS * 2):
        table.add_pokemon(dict(ENCOUNTER, pid=0x1000 + i,
                               shiny=(i == 2)))
    tk_root.update()
    after = widget_count(tk_root)

    rows = table._tree.get_children("")
    assert len(rows) == table.MAX_ROWS, "row cap not enforced"
    assert after == before, (
        f"widget count grew {before} -> {after}; the table is supposed "
        f"to render rows inside one native widget")


def test_newest_encounter_is_on_top(tk_root, launcher_mod) -> None:
    table = launcher_mod._RecentlySeen(tk_root)
    for i in range(3):
        table.add_pokemon(dict(ENCOUNTER, pid=0xAA00 + i))
    tk_root.update()
    top = table._tree.item(table._tree.get_children("")[0])
    assert top["values"][2] == f"{0xAA02:08X}"


def test_shiny_row_gets_the_shiny_tag(tk_root, launcher_mod) -> None:
    table = launcher_mod._RecentlySeen(tk_root)
    table.add_pokemon(dict(ENCOUNTER, shiny=True))
    tk_root.update()
    tags = table._tree.item(table._tree.get_children("")[0])["tags"]
    assert "shiny" in tags


def test_same_species_is_fetched_once_and_applied_to_every_row(
        tk_root, launcher_mod, monkeypatch) -> None:
    """Regression: duplicate species used to blank each other out.

    Each row built its own PhotoImage, but the cache holds one image
    per species — so every duplicate but the last lost its only
    reference, was garbage-collected, and rendered as an empty cell.
    One fetch per species, applied to every waiting row, fixes both
    the wasted work and the blanking.
    """
    submitted = []

    def fake_submit(widget, species_id, shiny, w, h, on_done,
                    generation=6):
        submitted.append((species_id, shiny, on_done))

    monkeypatch.setattr(launcher_mod, "_submit_sprite_job", fake_submit)
    table = launcher_mod._RecentlySeen(tk_root)
    for i in range(4):
        table.add_pokemon(dict(ENCOUNTER, pid=0xBB00 + i, species=25))
    tk_root.update()

    assert len(submitted) == 1, (
        f"one fetch expected for 4 rows of one species, got "
        f"{len(submitted)}")

    # Completing that single fetch must fill in all four rows.
    sentinel = object()
    submitted[0][2](sentinel)
    assert table._sprites.get((6, 25, False)) is sentinel
    assert table._pending == {}, "pending map leaked after completion"


def test_failed_sprite_fetch_clears_pending(
        tk_root, launcher_mod, monkeypatch) -> None:
    """A failed fetch must not wedge that species forever."""
    captured = []
    monkeypatch.setattr(
        launcher_mod, "_submit_sprite_job",
        lambda w, s, sh, mw, mh, cb, gen=6: captured.append(cb))
    table = launcher_mod._RecentlySeen(tk_root)
    table.add_pokemon(dict(ENCOUNTER, species=133))
    tk_root.update()
    captured[0](None)                       # fetch failed
    assert table._pending == {}
    # A later row for the same species can try again.
    table.add_pokemon(dict(ENCOUNTER, species=133, pid=0xCC01))
    tk_root.update()
    assert len(captured) == 2


def test_placeholder_clears_on_first_encounter(tk_root, launcher_mod) -> None:
    table = launcher_mod._RecentlySeen(tk_root)
    tk_root.update()
    assert len(table._tree.get_children("")) == 1     # the placeholder
    table.add_pokemon(dict(ENCOUNTER))
    tk_root.update()
    assert len(table._tree.get_children("")) == 1     # replaced, not added
    assert table._placeholder is None
