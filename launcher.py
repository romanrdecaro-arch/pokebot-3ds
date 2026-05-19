"""
pokebot-3ds launcher.

Run this single file to get started:
    python launcher.py

It will:
  1. Check Python version (3.8+ required).
  2. Auto-install any missing packages from requirements.txt.
  3. Open a GUI to configure and start the bot.
  4. Stream bot output into the log panel.
  5. Let you open the dashboard in your browser.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).parent

# Encounter stats persisted between sessions:
#   total = every wild ever seen by this install
#   phase = encounters since the last shiny/target (a "phase" in
#           shiny-hunting parlance); resets to 0 on a target hit.
_STATS_FILE = ROOT / ".pokebot_stats.json"


_STATS_DEFAULT = {
    "total": 0,
    "phase": 0,
    "phase_best_sv": None,   # lowest PSV seen this phase (rarer = lower)
    "phase_best_iv": None,   # highest IV-sum seen this phase
}


def _load_stats() -> dict:
    s = dict(_STATS_DEFAULT)
    try:
        import json as _j
        d = _j.loads(_STATS_FILE.read_text())
        s["total"] = int(d.get("total", 0))
        s["phase"] = int(d.get("phase", 0))
        bsv = d.get("phase_best_sv")
        biv = d.get("phase_best_iv")
        s["phase_best_sv"] = int(bsv) if bsv is not None else None
        s["phase_best_iv"] = int(biv) if biv is not None else None
    except Exception:
        return dict(_STATS_DEFAULT)
    return s


def _save_stats(stats: dict) -> None:
    try:
        import json as _j
        _STATS_FILE.write_text(_j.dumps(stats))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Step 1 — Python version gate (before anything else)
# ---------------------------------------------------------------------------

if sys.version_info < (3, 8):
    print(f"Python 3.8+ is required. You have {sys.version}. "
          "Download the latest Python from https://python.org")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Step 2 — Auto-install missing packages
# ---------------------------------------------------------------------------

def _pip_install(*pkgs: str) -> None:
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--quiet", *pkgs],
    )


def _ensure_deps() -> list[str]:
    """Read requirements.txt and pip-install anything not importable."""
    req = ROOT / "requirements.txt"
    if not req.exists():
        return []
    installed: list[str] = []
    import_map = {"PyYAML": "yaml", "pynput": "pynput"}
    for line in req.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        pkg = line.split(">=")[0].split("==")[0].split("[")[0].strip()
        imp = import_map.get(pkg, pkg.lower().replace("-", "_"))
        try:
            __import__(imp)
        except ImportError:
            print(f"[setup] Installing {pkg} ...")
            try:
                _pip_install(line)
                installed.append(pkg)
                print(f"[setup] {pkg} installed.")
            except Exception as exc:
                print(f"[setup] WARNING: could not install {pkg}: {exc}")
    return installed


_AUTO_INSTALLED = _ensure_deps()


# ---------------------------------------------------------------------------
# Step 3 — Load YAML config helper (safe after deps are in place)
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    cfg_path = ROOT / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        import yaml  # type: ignore
        return yaml.safe_load(cfg_path.read_text()) or {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Bot subprocess manager
# ---------------------------------------------------------------------------

class _BotProcess:
    def __init__(self, on_line, on_exit):
        self._proc: subprocess.Popen | None = None
        self.on_line = on_line
        self.on_exit = on_exit

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, extra_args: list[str]) -> None:
        if self.running:
            return
        # -u forces unbuffered stdout/stderr in the bot subprocess.
        # Without it, Python block-buffers when stdout is a pipe, and
        # the EVENT lines we use for the Recently Seen tab get stuck
        # in the buffer instead of flowing line-by-line. PYTHONUNBUFFERED
        # is set on the env too as belt-and-suspenders.
        cmd = [sys.executable, "-u", str(ROOT / "run.py")] + extra_args
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            bufsize=1,
            cwd=str(ROOT),
            env=env,
        )
        threading.Thread(target=self._drain, daemon=True).start()

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()

    def _drain(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            self.on_line(line.rstrip())
        code = self._proc.wait()
        self.on_exit(code)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

import tkinter as tk
from tkinter import scrolledtext, messagebox, ttk

# iOS-leaning dark palette: pure-black background with slightly raised
# 'cards' in iOS Dark's typical surface gray, plus the brand red as the
# primary action. Borders are barely-there so cards float instead of
# looking boxed-in.
_BG     = "#000000"   # iOS dark background
_PANEL  = "#0a0a0a"   # 'group' container
_PANEL2 = "#1c1c1e"   # iOS Dark elevated surface
_PANEL3 = "#2c2c2e"   # iOS Dark grouped table row
_BORDER = "#262626"
_TEXT   = "#ffffff"
_MUTED  = "#8e8e93"   # iOS secondary label
_ACCENT = "#ee1515"   # Pokéball red — primary CTA
_ACCENT_DEEP = "#b80f0f"  # darker red for hairline accents on dark bg
_ACCENT2 = "#0a84ff"  # iOS systemBlue
_GOOD   = "#30d158"   # iOS systemGreen
_WARN   = "#ffd60a"   # iOS systemYellow
_DANGER = "#ff453a"   # iOS systemRed


def _draw_pokeball(canvas: tk.Canvas, size: int = 32) -> None:
    """Clean monoline Pokéball mark — flat, two-tone, no skeuomorphism.

    A single accent-coloured ring, an equator line, and a hollow
    centre dot. Reads instantly at any size and matches the dark UI.
    """
    bg = canvas.cget("bg")
    lw = max(2, size // 16)
    pad = lw
    a, b = pad, size - pad
    cx = cy = size / 2
    r_in = size * 0.17
    # Outer ring.
    canvas.create_oval(a, a, b, b, outline=_ACCENT, width=lw, fill=bg)
    # Equator (stop short of the centre dot on each side).
    canvas.create_line(a, cy, cx - r_in, cy, fill=_ACCENT, width=lw)
    canvas.create_line(cx + r_in, cy, b, cy, fill=_ACCENT, width=lw)
    # Hollow centre button.
    canvas.create_oval(cx - r_in, cy - r_in, cx + r_in, cy + r_in,
                       outline=_ACCENT, width=lw, fill=bg)


_TYPE_COLORS = {
    "Normal":   "#a8a878", "Fire":     "#f08030", "Water":    "#6890f0",
    "Electric": "#f8d030", "Grass":    "#78c850", "Ice":      "#98d8d8",
    "Fighting": "#c03028", "Poison":   "#a040a0", "Ground":   "#e0c068",
    "Flying":   "#a890f0", "Psychic":  "#f85888", "Bug":      "#a8b820",
    "Rock":     "#b8a038", "Ghost":    "#705898", "Dragon":   "#7038f8",
    "Dark":     "#705848", "Steel":    "#b8b8d0", "Fairy":    "#ee99ac",
}

_NATURE_NAMES = (
    "Hardy",  "Lonely", "Brave",   "Adamant", "Naughty",
    "Bold",   "Docile", "Relaxed", "Impish",  "Lax",
    "Timid",  "Hasty",  "Serious", "Jolly",   "Naive",
    "Modest", "Mild",   "Quiet",   "Bashful", "Rash",
    "Calm",   "Gentle", "Sassy",   "Careful", "Quirky",
)


def _hidden_power(ivs: dict):
    try:
        from pokebot.sprites import hidden_power
        return hidden_power(ivs or {})
    except Exception:
        return ("Normal", 30)


# ---------------------------------------------------------------------------
# Shared sprite loader — prefers Pokémon Showdown animated GIFs, falls
# back to the static Gen 6 PNG. Used by both _PartyStrip and
# _RecentlySeen so animation logic lives in exactly one place.
# ---------------------------------------------------------------------------

def _prep_icon(path, max_w: int, max_h: int):
    """Menu icons are 68x56 RGBA with the creature sitting at a
    DIFFERENT spot in the mostly-transparent canvas per species — so
    centring the raw canvas looks random and they're tiny. With
    Pillow: crop to the actual art (alpha bbox), then scale UP crisply
    (nearest-neighbour) to fill the cell. Result: every sprite is the
    same visual size and properly centred. Returns an ImageTk image
    or None (caller falls back to a raw tk.PhotoImage).
    """
    try:
        from PIL import Image, ImageTk
    except Exception:
        return None
    try:
        im = Image.open(path).convert("RGBA")
        box = im.getbbox()
        if box:
            im = im.crop(box)
        w, h = im.size
        if not w or not h:
            return None
        scale = min(max_w / w, max_h / h)
        im = im.resize((max(1, round(w * scale)),
                        max(1, round(h * scale))), Image.NEAREST)
        return ImageTk.PhotoImage(im)
    except Exception:
        return None


def _hex_to_rgb(c: str):
    c = (c or "#1c1c1e").lstrip("#")
    if len(c) != 6:
        return (28, 28, 30)
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _prep_gif(path, max_w: int, max_h: int, bg: str = "#1c1c1e"):
    """Animated GIF → list of ImageTk frames, scaled to fit and fully
    OPAQUE on ``bg``.

    Two causes of flicker, both fixed here:
      1. Transparency — Tk clears the label between frames and the
         cell background flashes. We flatten every frame onto a solid
         ``bg`` so each is opaque.
      2. Ghosting/trails — Showdown 'ani' GIFs are INDEPENDENT full
         frames; compositing them cumulatively (the previous attempt)
         smeared every frame on top of the last. We now read each
         frame standalone via `seek`, letting PIL apply that frame's
         own transparency/disposal.
    A single union bbox keeps the sprite from bobbing. None on failure.
    """
    try:
        from PIL import Image, ImageTk
    except Exception:
        return None
    try:
        im = Image.open(path)
        n = getattr(im, "n_frames", 1)
        frames = []
        for i in range(n):
            im.seek(i)
            frames.append(im.convert("RGBA"))
        if not frames:
            return None
        union = None
        for f in frames:
            bb = f.getbbox()
            if bb is None:
                continue
            union = bb if union is None else (
                min(union[0], bb[0]), min(union[1], bb[1]),
                max(union[2], bb[2]), max(union[3], bb[3]))
        if union:
            frames = [f.crop(union) for f in frames]
        w, h = frames[0].size
        if not w or not h:
            return None
        scale = min(max_w / w, max_h / h)
        size = (max(1, round(w * scale)), max(1, round(h * scale)))
        rgb = _hex_to_rgb(bg)
        out = []
        for f in frames:
            f = f.resize(size, Image.NEAREST)
            flat = Image.new("RGB", size, rgb)
            flat.paste(f, (0, 0), f)          # opaque, on cell bg
            out.append(ImageTk.PhotoImage(flat))
        return out
    except Exception:
        return None


def _apply_sprite(widget: tk.Label, sprite) -> None:
    """Show a sprite on ``widget``. ``sprite`` is either a single
    PhotoImage (static PNG) or a list of PhotoImages (GIF frames). Any
    animation already running on the widget is cancelled first.
    """
    prev = getattr(widget, "_anim_after", None)
    if prev:
        try:
            widget.after_cancel(prev)
        except Exception:
            pass
        widget._anim_after = None
    if isinstance(sprite, list):
        _animate(widget, sprite, 0)
    else:
        widget.config(image=sprite, text="")


def _animate(widget: tk.Label, frames: list, idx: int) -> None:
    try:
        if not widget.winfo_exists():
            return
    except Exception:
        return
    widget.config(image=frames[idx], text="")
    widget._anim_after = widget.after(
        90, _animate, widget, frames, (idx + 1) % len(frames))


def _load_sprite_into(widget: tk.Label, species_id: int, shiny: bool,
                      cache: dict, empty_text: str = "—") -> None:
    """Fetch + display a species sprite on ``widget`` (worker thread for
    the network/IO, Tk calls marshalled back via .after). Animated GIF
    preferred; static PNG fallback; ``#id`` text if neither resolves.
    ``cache`` maps a per-(species,shiny) key to a PhotoImage or a
    frame-list so we never re-decode.
    """
    if not species_id:
        widget.config(text=empty_text, fg=_MUTED, width=4, height=2,
                       font=("Segoe UI", 12))
        return
    key = species_id * 2 + (1 if shiny else 0)
    cached = cache.get(key)
    if cached is False:                       # negative cache: gave up
        widget.config(text=f"#{species_id}", fg=_MUTED,
                      font=("Consolas", 8))
        return
    if cached is not None:
        _apply_sprite(widget, cached)
        return
    widget.config(text="…", fg=_MUTED, width=4, height=2,
                  font=("Segoe UI", 11))

    def _fail():
        cache[key] = False                    # don't re-hit the network
        try:
            widget.config(text=f"#{species_id}", fg=_MUTED,
                           font=("Consolas", 8))
        except Exception:
            pass

    def _worker():
        # Resolve EVERY source we can, in priority order, so one
        # failing (e.g. Showdown name lookup) just falls through:
        #   animated GIF → static front PNG → menu icon.
        sources = []
        try:
            from pokebot.sprites import (get_animated_sprite_path,
                                         get_sprite_path,
                                         get_menu_sprite_path)
            for fn, kind in ((get_animated_sprite_path, "gif"),
                             (get_sprite_path, "static"),
                             (get_menu_sprite_path, "static")):
                try:
                    p = fn(species_id, shiny=shiny)
                except Exception:
                    p = None
                if p:
                    sources.append((kind, str(p)))
        except Exception:
            sources = []
        if not sources:
            widget.after(0, _fail)
            return

        def _install():
            try:
                _bg = widget.cget("bg")
            except Exception:
                _bg = "#1c1c1e"
            for kind, p in sources:
                try:
                    if kind == "gif":
                        frames = _prep_gif(p, 68, 54, _bg)
                        if not frames:        # raw Tk frames fallback
                            frames, i = [], 0
                            while True:
                                try:
                                    frames.append(tk.PhotoImage(
                                        file=p,
                                        format=f"gif -index {i}"))
                                except tk.TclError:
                                    break
                                i += 1
                        if frames:
                            cache[key] = frames
                            _apply_sprite(widget, frames)
                            return
                    else:
                        img = _prep_icon(p, 68, 54)
                        if img is None:
                            img = tk.PhotoImage(file=p)
                        cache[key] = img
                        _apply_sprite(widget, img)
                        return
                except Exception:
                    continue
            _fail()

        widget.after(0, _install)

    threading.Thread(target=_worker, daemon=True).start()


class _PartyStrip(tk.Frame):
    """A horizontal row of six slot tiles showing the player's party.

    Updates when the bot broadcasts a 'party' event (full slot list) or
    a 'candidate' event (single Pokémon — treated as slot 0).
    """

    SLOTS = 6

    def __init__(self, parent):
        super().__init__(parent, bg=_PANEL)
        self.pack(side="top", fill="x", padx=8, pady=(8, 0))

        title_bar = tk.Frame(self, bg=_PANEL)
        title_bar.pack(fill="x", padx=6, pady=(0, 6))
        tk.Frame(title_bar, bg=_ACCENT, width=3, height=14).pack(
            side="left", padx=(0, 8))
        tk.Label(title_bar, text="Party", bg=_PANEL, fg=_TEXT,
                 font=("Segoe UI", 11, "bold")).pack(side="left")

        row = tk.Frame(self, bg=_PANEL2,
                       highlightthickness=1, highlightbackground=_BORDER)
        row.pack(fill="x", padx=4, pady=(0, 6), ipady=4)
        # Six equal-width slot cells.
        for i in range(self.SLOTS):
            row.grid_columnconfigure(i, weight=1, uniform="party")
        self._slot_frames: list[tk.Frame] = []
        self._slot_sprites: dict[int, tk.PhotoImage] = {}
        for i in range(self.SLOTS):
            cell = tk.Frame(row, bg=_PANEL2, padx=6, pady=6)
            cell.grid(row=0, column=i, sticky="nsew")
            self._slot_frames.append(cell)
            self._render_empty(cell, i)

    # ---- public API --------------------------------------------------------

    def update_from_party(self, slots_data: list):
        """Update from a 'party' broadcast — list of slot dicts."""
        # Sort by slot index (party broadcasts are usually sorted, but
        # observe.py only includes non-empty entries, so a slot may be
        # missing from the list).
        by_index = {int(s.get("slot", -1)): s for s in slots_data
                    if isinstance(s, dict)}
        for i in range(self.SLOTS):
            cell = self._slot_frames[i]
            for child in cell.winfo_children():
                child.destroy()
            slot = by_index.get(i)
            if slot is None:
                self._render_empty(cell, i)
            else:
                self._render_slot(cell, i, slot)

    def update_from_candidate(self, evt: dict):
        """Update slot 0 from a 'candidate' broadcast."""
        cell = self._slot_frames[0]
        for child in cell.winfo_children():
            child.destroy()
        self._render_slot(cell, 0, evt)

    # ---- rendering ---------------------------------------------------------

    def _render_empty(self, cell: tk.Frame, idx: int):
        tk.Label(cell, text=f"Slot {idx + 1}", bg=_PANEL2, fg=_MUTED,
                 font=("Segoe UI", 8, "italic")).pack()
        tk.Label(cell, text="—", bg=_PANEL2, fg=_MUTED,
                 font=("Segoe UI", 14)).pack(pady=4)

    def _render_slot(self, cell: tk.Frame, idx: int, evt: dict):
        species_id = int(evt.get("species") or 0)
        shiny = bool(evt.get("shiny"))
        level = evt.get("level")
        nick = evt.get("nickname") or ""
        # Border colour for shiny / non-shiny.
        if shiny:
            cell.configure(bg="#2a230a", highlightthickness=1,
                           highlightbackground="#ffd86b")
        else:
            cell.configure(bg=_PANEL2, highlightthickness=0)
        bg = cell.cget("bg")
        # Sprite.
        sprite_lbl = tk.Label(cell, bg=bg, bd=0, highlightthickness=0)
        sprite_lbl.pack()
        self._load_sprite_async(species_id, shiny, sprite_lbl)
        # Level
        lvl_text = f"Lv {level}" if level is not None else "Lv —"
        if shiny:
            lvl_text += "  ★"
        tk.Label(cell, text=lvl_text, bg=bg,
                 fg="#ffd86b" if shiny else _TEXT,
                 font=("Segoe UI", 9, "bold")).pack()
        # Nickname (or species number fallback).
        sub = nick if nick else f"#{species_id}"
        tk.Label(cell, text=sub, bg=bg, fg=_MUTED,
                 font=("Segoe UI", 8)).pack()

    def _load_sprite_async(self, species_id: int, shiny: bool,
                           target: tk.Label):
        _load_sprite_into(target, species_id, shiny,
                          self._slot_sprites, empty_text="—")


class _RecentlySeen(tk.Frame):
    """Sprite-rich encounter table that mirrors the pokebot-gen3 dashboard.

    Each row is a Frame containing: sprite, gender icon, level, PID,
    Shiny Value (PSV), ability id, nature, IV breakdown with per-stat
    colour, and Hidden Power type/power.
    """

    MAX_ROWS = 100  # how many recent encounters to keep on screen

    # Column layout: (label, fixed_width_px, anchor). Fixed pixel
    # widths, NO uniform group (a shared uniform forces every column
    # to the widest one's size — that overflowed the panel). minsize
    # + weight 0 keeps each column its own width and identical
    # between the header and every row, so they line up.
    _HEADERS = (
        ("Species",      76, "center"),
        ("Gender",       52, "center"),
        ("Level",        56, "center"),
        ("PID",          82, "center"),
        ("Shiny Value",  86, "center"),
        ("Ability",     122, "center"),
        ("Nature",       76, "center"),
        ("IVs",         178, "center"),
        ("Hidden Power", 96, "center"),
    )

    # Data columns live at grid index 1..N. Columns 0 and N+1 are
    # flexible spacers (weight 1) so the fixed block is CENTRED in
    # the panel instead of jammed left with dead space on the right.
    @classmethod
    def _config_cols(cls, frame) -> None:
        n = len(cls._HEADERS)
        frame.grid_columnconfigure(0, weight=1, minsize=0)
        for i, (_, w, _) in enumerate(cls._HEADERS):
            frame.grid_columnconfigure(i + 1, minsize=w, weight=0)
        frame.grid_columnconfigure(n + 1, weight=1, minsize=0)

    @staticmethod
    def _col(i: int) -> int:
        """Grid column for data column ``i`` (0-based) — offset by the
        left spacer."""
        return i + 1

    def __init__(self, parent):
        super().__init__(parent, bg=_PANEL)
        self.pack(fill="both", expand=True)

        # Title bar
        title_bar = tk.Frame(self, bg=_PANEL)
        title_bar.pack(fill="x", padx=14, pady=(10, 4))
        tk.Frame(title_bar, bg=_ACCENT, width=4, height=18).pack(
            side="left", padx=(0, 10))
        tk.Label(title_bar, text="Recently Seen",
                 bg=_PANEL, fg=_TEXT,
                 font=("Segoe UI", 13, "bold")).pack(side="left")
        self._stats = _load_stats()
        self._counter_lbl = tk.Label(
            title_bar, text=self._counter_text(),
            bg=_PANEL, fg=_MUTED, font=("Segoe UI", 9))
        self._counter_lbl.pack(side="right")

        # Header row
        header = tk.Frame(self, bg=_PANEL)
        header.pack(fill="x", padx=8)
        self._config_cols(header)
        for i, (text, _, anchor) in enumerate(self._HEADERS):
            tk.Label(header, text=text, bg=_PANEL, fg=_MUTED,
                     font=("Segoe UI", 9, "bold"),
                     anchor=anchor).grid(row=0, column=self._col(i),
                                         pady=(0, 6), sticky="nsew")
        tk.Frame(self, bg=_BORDER, height=1).pack(fill="x", padx=8)

        # Scrollable rows area
        scroll_frame = tk.Frame(self, bg=_PANEL)
        scroll_frame.pack(fill="both", expand=True, padx=8, pady=(2, 8))
        self._canvas = tk.Canvas(scroll_frame, bg=_PANEL,
                                 highlightthickness=0)
        self._canvas.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(scroll_frame, orient="vertical",
                           command=self._canvas.yview)
        sb.pack(side="right", fill="y")
        self._canvas.configure(yscrollcommand=sb.set)
        self._rows_frame = tk.Frame(self._canvas, bg=_PANEL)
        self._rows_frame_id = self._canvas.create_window(
            (0, 0), window=self._rows_frame, anchor="nw")
        self._rows_frame.bind(
            "<Configure>",
            lambda e: self._canvas.configure(
                scrollregion=self._canvas.bbox("all")))
        self._canvas.bind(
            "<Configure>",
            lambda e: self._canvas.itemconfigure(
                self._rows_frame_id, width=e.width))
        # Mouse wheel scroll
        self._canvas.bind_all(
            "<MouseWheel>",
            lambda e: self._canvas.yview_scroll(int(-1 * (e.delta / 120)),
                                                "units"))

        self._rows: list[tk.Frame] = []
        self._sprites: dict[int, tk.PhotoImage] = {}  # keep refs alive

        # Empty placeholder
        self._empty = tk.Label(self._rows_frame,
                               text="No encounters yet — start a hunt.",
                               bg=_PANEL, fg=_MUTED,
                               font=("Segoe UI", 10, "italic"))
        self._empty.pack(pady=24)

    # ---- public API --------------------------------------------------------

    def _counter_text(self) -> str:
        s = self._stats
        bsv = s["phase_best_sv"]
        biv = s["phase_best_iv"]
        return (f"Phase {s['phase']}  ·  Total {s['total']}  ·  "
                f"Best SV {bsv if bsv is not None else '—'}  ·  "
                f"Best IVs {biv if biv is not None else '—'}")

    @staticmethod
    def _evt_psv(evt: dict):
        psv = evt.get("psv")
        if psv is None:
            pid = int(evt.get("pid") or 0)
            psv = ((pid >> 16) ^ (pid & 0xFFFF)) if pid else None
        return int(psv) if psv is not None else None

    @staticmethod
    def _evt_ivsum(evt: dict):
        ivs = evt.get("ivs") or {}
        if not ivs:
            return None
        return sum(int(ivs.get(s, 0)) for s in
                   ("HP", "Atk", "Def", "Spe", "SpA", "SpD"))

    def add_pokemon(self, evt: dict):
        if self._empty:
            self._empty.destroy()
            self._empty = None
        # A shiny/target emits a separate 'target_hit' after its
        # 'encounter' — that encounter already bumped the counters, so
        # target_hit only CLOSES the phase (reset phase + per-phase
        # bests), no double count.
        if evt.get("type") == "target_hit":
            self._stats["phase"] = 0
            self._stats["phase_best_sv"] = None
            self._stats["phase_best_iv"] = None
        else:
            self._stats["total"] += 1
            self._stats["phase"] += 1
            psv = self._evt_psv(evt)
            if psv is not None:
                cur = self._stats["phase_best_sv"]
                self._stats["phase_best_sv"] = (
                    psv if cur is None else min(cur, psv))
            ivs = self._evt_ivsum(evt)
            if ivs is not None:
                cur = self._stats["phase_best_iv"]
                self._stats["phase_best_iv"] = (
                    ivs if cur is None else max(cur, ivs))
        _save_stats(self._stats)
        self._counter_lbl.config(
            text=self._counter_text(),
            fg=_ACCENT, font=("Segoe UI", 9, "bold"))

        row = self._build_row(evt)
        row.pack(fill="x", padx=0, pady=2)
        self._rows.insert(0, row)
        # Move newest row to top
        for r in self._rows:
            r.pack_forget()
        for r in self._rows[: self.MAX_ROWS]:
            r.pack(fill="x", padx=0, pady=2)
        # Trim
        for r in self._rows[self.MAX_ROWS :]:
            r.destroy()
        self._rows = self._rows[: self.MAX_ROWS]
        self._canvas.yview_moveto(0.0)

    # ---- row construction --------------------------------------------------

    def _build_row(self, evt: dict) -> tk.Frame:
        species_id = int(evt.get("species") or 0)
        shiny = bool(evt.get("shiny"))
        gender = evt.get("gender") or "G"
        level = evt.get("level")
        pid = int(evt.get("pid") or 0)
        psv = evt.get("psv")
        if psv is None and pid:
            psv = (pid >> 16) ^ (pid & 0xFFFF)
        ability_id = evt.get("ability_id")
        ability_num = evt.get("ability_num")
        nature = evt.get("nature") or ""
        ivs = evt.get("ivs") or {}
        hp_type, hp_power = _hidden_power(ivs)

        bg = _PANEL2 if not shiny else "#2a230a"  # warm tint for shiny rows
        row = tk.Frame(self._rows_frame, bg=bg, padx=0, pady=4,
                       highlightthickness=1,
                       highlightbackground="#ffd86b" if shiny else _BORDER)
        self._config_cols(row)

        # Sprite (Species) — locked in a fixed box so an odd-sized
        # menu icon can't widen column 0 and shove the row out of
        # alignment; the icon is centred inside the box.
        sp_w = self._HEADERS[0][1]
        sp_box = tk.Frame(row, bg=bg, width=sp_w, height=58)
        sp_box.grid(row=0, column=self._col(0), sticky="nsew")
        sp_box.grid_propagate(False)
        sp_box.pack_propagate(False)
        sprite_lbl = tk.Label(sp_box, bg=bg, bd=0,
                               highlightthickness=0)
        sprite_lbl.place(relx=0.5, rely=0.5, anchor="center")
        self._load_sprite_async(species_id, shiny, sprite_lbl)

        # Gender
        sex_color = {"M": "#5fa9ff", "F": "#ff7eb6", "G": _MUTED}.get(gender, _MUTED)
        sex_glyph = {"M": "♂", "F": "♀", "G": "—"}.get(gender, "—")
        tk.Label(row, text=sex_glyph, bg=bg, fg=sex_color,
                 font=("Segoe UI", 12, "bold")).grid(
                     row=0, column=self._col(1), sticky="nsew")

        # Level
        lvl = f"Lv {level}" if level is not None else "Lv ?"
        tk.Label(row, text=lvl, bg=bg, fg=_TEXT,
                 font=("Segoe UI", 10, "bold")).grid(
                     row=0, column=self._col(2), sticky="nsew")

        # PID
        tk.Label(row, text=f"{pid:08X}", bg=bg, fg=_TEXT,
                 font=("Consolas", 10)).grid(
                     row=0, column=self._col(3), sticky="nsew")

        # Shiny Value (PSV) — gold if shiny, muted otherwise
        psv_text = f"{int(psv):05d}" if psv is not None else "—"
        if shiny:
            sv_lbl = tk.Label(row, text=f"★ {psv_text}", bg=bg,
                              fg="#ffd86b", font=("Consolas", 10, "bold"))
        else:
            sv_lbl = tk.Label(row, text=f"— {psv_text}", bg=bg,
                              fg=_MUTED, font=("Consolas", 10))
        sv_lbl.grid(row=0, column=self._col(4), sticky="nsew")

        # Ability
        ab_text = self._ability_text(ability_id, ability_num)
        tk.Label(row, text=ab_text, bg=bg, fg=_TEXT,
                 font=("Segoe UI", 10)).grid(
                     row=0, column=self._col(5), sticky="nsew")

        # Nature
        nat_text = nature if isinstance(nature, str) and nature else \
            (_NATURE_NAMES[int(nature)] if isinstance(nature, int)
             and 0 <= int(nature) < 25 else "?")
        tk.Label(row, text=nat_text, bg=bg, fg=_TEXT,
                 font=("Segoe UI", 10)).grid(
                     row=0, column=self._col(6), sticky="nsew")

        # IVs — per-stat coloured (red=31, blue=0, white otherwise),
        # centred in the column.
        iv_outer = tk.Frame(row, bg=bg)
        iv_outer.grid(row=0, column=self._col(7), sticky="nsew")
        iv_frame = tk.Frame(iv_outer, bg=bg)
        iv_frame.place(relx=0.5, rely=0.5, anchor="center")
        order = ("HP", "Atk", "Def", "Spe", "SpA", "SpD")
        for i, stat in enumerate(order):
            v = int(ivs.get(stat, 0))
            color = _DANGER if v == 31 else (_ACCENT2 if v == 0 else _TEXT)
            weight = "bold" if v in (0, 31) else "normal"
            tk.Label(iv_frame, text=str(v), bg=bg, fg=color,
                     font=("Consolas", 10, weight),
                     width=2, anchor="center").pack(side="left", padx=1)
        iv_sum = sum(int(ivs.get(s, 0)) for s in order)
        tk.Label(iv_frame, text=f"({iv_sum})", bg=bg, fg=_MUTED,
                 font=("Consolas", 10)).pack(side="left", padx=(6, 0))

        # Hidden Power (type pill + power), centred in the column
        hp_outer = tk.Frame(row, bg=bg)
        hp_outer.grid(row=0, column=self._col(8), sticky="nsew")
        hp_frame = tk.Frame(hp_outer, bg=bg)
        hp_frame.place(relx=0.5, rely=0.5, anchor="center")
        hp_color = _TYPE_COLORS.get(hp_type, "#888")
        pill = tk.Label(hp_frame, text=hp_type.upper(),
                        bg=hp_color, fg="#000",
                        font=("Segoe UI", 8, "bold"),
                        padx=6, pady=1)
        pill.pack(side="top", anchor="w")
        tk.Label(hp_frame, text=f"{hp_power} Power", bg=bg, fg=_MUTED,
                 font=("Segoe UI", 9)).pack(side="top", anchor="w")

        return row

    # ---- async sprite loading ----------------------------------------------

    def _load_sprite_async(self, species_id: int, shiny: bool,
                           target: tk.Label):
        _load_sprite_into(target, species_id, shiny,
                          self._sprites, empty_text="?")

    # ---- small helpers -----------------------------------------------------

    @staticmethod
    def _ability_text(ability_id, ability_num) -> str:
        if ability_id is None:
            return "?"
        try:
            from pokebot.abilities import ability_name
        except Exception:
            ability_name = lambda i: f"#{i}"   # noqa: E731
        suffix = ""
        if ability_num == 4:
            suffix = " (HA)"
        elif ability_num == 1:
            suffix = " (1)"
        elif ability_num == 2:
            suffix = " (2)"
        return f"{ability_name(ability_id)}{suffix}"


class _App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("pokebot-3ds")
        self.geometry("1180x720")
        self.minsize(900, 520)
        self.configure(bg=_BG)
        try:
            ttk.Style().theme_use("clam")
        except Exception:
            pass
        self._tweak_ttk_theme()

        self._cfg = _load_config()
        self._bot = _BotProcess(self._on_bot_line, self._on_bot_exit)
        self._offset_proc: subprocess.Popen | None = None
        self._poll_alive = True
        self._last_detected_game: str | None = None

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if _AUTO_INSTALLED:
            self._log(f"Auto-installed: {', '.join(_AUTO_INSTALLED)}", "good")
        self._log("Ready. Configure offsets in config.yaml, then press Start.")

        threading.Thread(target=self._status_poll_loop,
                         daemon=True, name="StatusPoll").start()

    # ---- Layout -----------------------------------------------------------

    def _tweak_ttk_theme(self):
        """Restyle ttk Combobox to fit the dark palette."""
        s = ttk.Style()
        s.configure("Dark.TCombobox",
                    fieldbackground=_PANEL2, background=_PANEL2,
                    foreground=_TEXT, arrowcolor=_TEXT,
                    bordercolor=_BORDER, lightcolor=_BORDER,
                    darkcolor=_BORDER, selectbackground=_PANEL2,
                    selectforeground=_TEXT, padding=4)
        s.map("Dark.TCombobox",
              fieldbackground=[("readonly", _PANEL2)],
              foreground=[("readonly", _TEXT)],
              selectbackground=[("readonly", _PANEL2)],
              selectforeground=[("readonly", _TEXT)])
        # Combobox dropdown listbox uses option-db, not Style.
        self.option_add("*TCombobox*Listbox.background", _PANEL2)
        self.option_add("*TCombobox*Listbox.foreground", _TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", _ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", "white")
        self.option_add("*TCombobox*Listbox.borderWidth", 0)

    def _build(self):
        # ── Header ──────────────────────────────────────────────────────────
        hdr = tk.Frame(self, bg=_PANEL, padx=18, pady=12)
        hdr.pack(fill="x")
        # Clean monoline mark drawn straight onto a Tk canvas — no
        # bitmap asset (the old pre-rendered PNG looked crunchy).
        logo = tk.Canvas(hdr, width=40, height=40,
                         bg=_PANEL, highlightthickness=0)
        logo.pack(side="left", padx=(0, 14))
        _draw_pokeball(logo, 40)
        # Wordmark
        tk.Label(hdr, text="pokebot-3ds", bg=_PANEL, fg=_TEXT,
                 font=("Segoe UI", 16, "bold")).pack(side="left")
        tk.Label(hdr, text="3DS Gen 6/7 automation",
                 bg=_PANEL, fg=_MUTED,
                 font=("Segoe UI", 9)).pack(side="left", padx=(10, 0), pady=(6, 0))
        # Right-aligned: bot run status pill
        tk.Frame(hdr, bg=_PANEL).pack(side="left", fill="x", expand=True)
        pill = tk.Frame(hdr, bg=_PANEL2, padx=10, pady=4)
        pill.pack(side="right", padx=(8, 0))
        self._dot = tk.Label(pill, text="●", bg=_PANEL2, fg=_MUTED,
                             font=("Segoe UI", 11))
        self._dot.pack(side="left", padx=(0, 4))
        self._status_lbl = tk.Label(pill, text="stopped",
                                    bg=_PANEL2, fg=_MUTED,
                                    font=("Segoe UI", 10, "bold"))
        self._status_lbl.pack(side="left")
        tk.Label(hdr,
                 text=f"Python {sys.version.split()[0]}",
                 bg=_PANEL, fg=_MUTED, font=("Segoe UI", 9)).pack(side="right",
                                                                  padx=(0, 12))

        # Thin red accent stripe under the header — ties the brand colour
        # into the chrome instead of leaving it only on the Start button.
        tk.Frame(self, bg=_ACCENT, height=2).pack(fill="x")

        # ── Body: sidebar + log ─────────────────────────────────────────────
        body = tk.Frame(self, bg=_BG)
        body.pack(fill="both", expand=True, padx=10, pady=10)

        # Scrollable sidebar with a STICKY action bar at the top.
        # Start/Stop live above the scroll so they're always visible
        # regardless of how far down the user has scrolled.
        side_outer = tk.Frame(body, bg=_PANEL, width=280)
        side_outer.pack(side="left", fill="y", padx=(0, 10))
        side_outer.pack_propagate(False)
        self._build_sticky_actions(side_outer)
        side_canvas = tk.Canvas(side_outer, bg=_PANEL,
                                highlightthickness=0, bd=0)
        side_canvas.pack(side="top", fill="both", expand=True)
        side_inner = tk.Frame(side_canvas, bg=_PANEL)
        side_window = side_canvas.create_window(
            (0, 0), window=side_inner, anchor="nw")
        def _on_inner_configure(_):
            side_canvas.configure(scrollregion=side_canvas.bbox("all"))
        def _on_canvas_configure(e):
            side_canvas.itemconfigure(side_window, width=e.width)
        side_inner.bind("<Configure>", _on_inner_configure)
        side_canvas.bind("<Configure>", _on_canvas_configure)
        # Mousewheel scrolling when the cursor is anywhere over the
        # sidebar. We use bind (not bind_all) so the encounter table's
        # own mousewheel binding still works on the right pane.
        def _wheel(e):
            side_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        for w in (side_canvas, side_inner):
            w.bind("<Enter>", lambda e: side_canvas.bind_all("<MouseWheel>", _wheel))
            w.bind("<Leave>", lambda e: side_canvas.unbind_all("<MouseWheel>"))
        self._build_sidebar(side_inner)

        right = tk.Frame(body, bg=_PANEL)
        right.pack(side="left", fill="both", expand=True)
        # Party strip on top, then the Recently Seen / Log notebook.
        self._party = _PartyStrip(right)
        self._build_log(right)

    def _lbl(self, parent, text):
        tk.Label(parent, text=text.upper(), bg=parent.cget("bg"), fg=_MUTED,
                 font=("Segoe UI", 9, "bold"),
                 anchor="w").pack(fill="x", padx=4, pady=(0, 6))

    def _sep(self, parent):
        tk.Frame(parent, bg=_BORDER, height=1).pack(fill="x", padx=8, pady=4)

    def _card(self, parent, title: str | None = None) -> tk.Frame:
        """An iOS-style grouped card. Returns the inner content frame."""
        wrap = tk.Frame(parent, bg=_PANEL)
        wrap.pack(fill="x", padx=12, pady=(0, 14))
        if title:
            title_row = tk.Frame(wrap, bg=_PANEL)
            title_row.pack(fill="x", padx=4, pady=(0, 6))
            # 8px red square, then the title in red — gives every section
            # a brand-coloured anchor on the left edge.
            tk.Frame(title_row, bg=_ACCENT, width=3, height=12).pack(
                side="left", padx=(0, 8))
            tk.Label(title_row, text=title.upper(), bg=_PANEL, fg=_ACCENT,
                     font=("Segoe UI", 9, "bold"),
                     anchor="w").pack(side="left")
        card = tk.Frame(wrap, bg=_PANEL2, padx=14, pady=12,
                        highlightthickness=1,
                        highlightbackground=_BORDER)
        card.pack(fill="x")
        return card

    def _build_sidebar(self, p):
        # Wrap sidebar contents in a top-padded frame so cards float.
        tk.Frame(p, bg=_PANEL, height=14).pack(fill="x")

        # ── Card: Azahar / detected game ────────────────────────────────────
        status_card = self._card(p, "Status")
        self._azahar_lbl = tk.Label(status_card, text="● checking…",
                                    bg=_PANEL2, fg=_MUTED,
                                    font=("Segoe UI", 11), anchor="w")
        self._azahar_lbl.pack(fill="x", pady=(0, 4))
        self._game_var = tk.StringVar(value="")
        self._game_display_lbl = tk.Label(
            status_card, text="Waiting for Azahar…",
            bg=_PANEL2, fg=_MUTED,
            font=("Segoe UI", 13, "bold"), anchor="w",
            wraplength=235, justify="left")
        self._game_display_lbl.pack(fill="x", pady=(0, 2))
        self._game_detect_lbl = tk.Label(
            status_card, text="",
            bg=_PANEL2, fg=_MUTED,
            font=("Segoe UI", 9), anchor="w",
            wraplength=235, justify="left")
        self._game_detect_lbl.pack(fill="x")
        self._game_var.trace_add("write", self._on_game_change)

        # ── Card: Hunt setup (Method / Starter / Target) ────────────────────
        hunt_card = self._card(p, "Hunt")

        # METHOD
        tk.Label(hunt_card, text="Method", bg=_PANEL2, fg=_MUTED,
                 font=("Segoe UI", 9, "bold"),
                 anchor="w").pack(fill="x", pady=(0, 4))
        self._method_var = tk.StringVar(value="")
        self._method_cb = ttk.Combobox(hunt_card, textvariable=self._method_var,
                                       values=[],
                                       state="readonly",
                                       style="Dark.TCombobox")
        self._method_cb.pack(fill="x")
        self._method_warn = tk.Label(hunt_card, text="",
                                     bg=_PANEL2, fg=_WARN,
                                     font=("Segoe UI", 9), anchor="w",
                                     wraplength=235, justify="left")
        self._method_warn.pack(fill="x", pady=(2, 0))
        self._method_var.trace_add("write", self._on_method_change)

        # STARTER (sub-dropdown, only visible when method=Starters)
        self._starter_frame = tk.Frame(hunt_card, bg=_PANEL2)
        tk.Frame(self._starter_frame, bg=_BORDER, height=1).pack(
            fill="x", pady=(10, 8))
        tk.Label(self._starter_frame, text="Starter",
                 bg=_PANEL2, fg=_MUTED,
                 font=("Segoe UI", 9, "bold"),
                 anchor="w").pack(fill="x", pady=(0, 4))
        self._starter_var = tk.StringVar(value="")
        self._starter_cb = ttk.Combobox(self._starter_frame,
                                        textvariable=self._starter_var,
                                        values=[], state="readonly",
                                        style="Dark.TCombobox")
        self._starter_cb.pack(fill="x")
        self._starter_hint = tk.Label(self._starter_frame, text="",
                                      bg=_PANEL2, fg=_MUTED,
                                      font=("Segoe UI", 9, "italic"),
                                      anchor="w",
                                      wraplength=235, justify="left")
        self._starter_hint.pack(fill="x", pady=(2, 0))

        # MOVEMENT (sub-dropdown, only visible when method=Random encounters)
        self._movement_frame = tk.Frame(hunt_card, bg=_PANEL2)
        tk.Frame(self._movement_frame, bg=_BORDER, height=1).pack(
            fill="x", pady=(10, 8))
        tk.Label(self._movement_frame, text="Movement",
                 bg=_PANEL2, fg=_MUTED,
                 font=("Segoe UI", 9, "bold"),
                 anchor="w").pack(fill="x", pady=(0, 4))
        self._movement_choices = ["Horizontal (← →)", "Vertical (↑ ↓)"]
        self._movement_var = tk.StringVar(value=self._movement_choices[0])
        self._movement_cb = ttk.Combobox(self._movement_frame,
                                         textvariable=self._movement_var,
                                         values=self._movement_choices,
                                         state="readonly",
                                         style="Dark.TCombobox")
        self._movement_cb.pack(fill="x")
        tk.Label(self._movement_frame,
                 text="Stand on a tile where you can step both directions "
                      "in tall grass; the bot mashes the dpad pair.",
                 bg=_PANEL2, fg=_MUTED,
                 font=("Segoe UI", 9, "italic"),
                 anchor="w", wraplength=235, justify="left"
                 ).pack(fill="x", pady=(2, 0))

        # TARGET FILTER (always visible)
        # Keep a reference to the divider so _on_method_change can pack
        # the starter frame BEFORE it (between method and target rather
        # than below the target combo).
        self._target_divider = tk.Frame(hunt_card, bg=_BORDER, height=1)
        self._target_divider.pack(fill="x", pady=(10, 8))
        tk.Label(hunt_card, text="Target filter",
                 bg=_PANEL2, fg=_MUTED,
                 font=("Segoe UI", 9, "bold"),
                 anchor="w").pack(fill="x", pady=(0, 4))
        self._target_choices = [
            "Shiny only",
            "Any (first match)",
            "Perfect IVs (6×31)",
            "5+ perfect IVs",
            "Shiny + 4+ perfect IVs",
        ]
        self._target_var = tk.StringVar(value="Shiny only")
        self._target_cb = ttk.Combobox(hunt_card, textvariable=self._target_var,
                                       values=self._target_choices,
                                       state="readonly",
                                       style="Dark.TCombobox")
        self._target_cb.pack(fill="x")
        self._refresh_method_options()

        # Options card removed (was clutter in the common hunt flow).
        # Dry-run and verbose-logging are still wired for run.py CLI
        # users; the launcher just always sends both off. We keep the
        # BooleanVars so _start_bot doesn't need conditionals.
        self._dry_var = tk.BooleanVar(value=False)
        self._verb_var = tk.BooleanVar(value=False)

        # Hidden manual-override scan button (CLI fallback only — not
        # surfaced in the UI; kept so the existing _run_find_offsets
        # plumbing still has a target widget.)
        self._scan_btn = tk.Button(p, command=self._run_find_offsets)
        self._offset_lbl = tk.Label(p)

    def _build_sticky_actions(self, parent):
        """Always-visible Start/Stop bar at the top of the sidebar."""
        bar = tk.Frame(parent, bg=_PANEL, padx=12, pady=12)
        bar.pack(side="top", fill="x")
        self._start_btn = tk.Button(
            bar, text="▶  Start Bot", command=self._start_bot,
            bg=_ACCENT, fg="white", relief="flat", bd=0,
            font=("Segoe UI", 12, "bold"), cursor="hand2",
            activebackground=_ACCENT, activeforeground="white",
            padx=10, pady=12, highlightthickness=0)
        self._start_btn.pack(fill="x")
        self._stop_btn = tk.Button(
            bar, text="■  Stop Bot", command=self._stop_bot,
            bg=_PANEL3, fg=_MUTED, relief="flat", bd=0,
            font=("Segoe UI", 10), cursor="hand2", state="disabled",
            activebackground=_PANEL3, activeforeground=_MUTED,
            padx=10, pady=8, highlightthickness=0)
        self._stop_btn.pack(fill="x", pady=(8, 0))
        # Hairline divider between sticky bar and scrollable area.
        tk.Frame(parent, bg=_BORDER, height=1).pack(side="top", fill="x")

    def _btn(self, parent, text, cmd, bg=_ACCENT, fg="white",
             state="normal", subtle=False):
        b = tk.Button(parent, text=text, command=cmd,
                      bg=bg, fg=fg, relief="flat",
                      font=("Segoe UI", 10, "bold" if not subtle else "normal"),
                      cursor="hand2",
                      activebackground=bg, activeforeground=fg,
                      padx=10, pady=7, bd=0, state=state,
                      highlightthickness=0)
        b.pack(fill="x", padx=12, pady=3)
        return b

    def _build_log(self, parent):
        # Two-tab Notebook: "Recently Seen" (sprite encounter table) + "Log".
        nb = ttk.Notebook(parent)
        nb.pack(fill="both", expand=True, padx=6, pady=6)
        try:
            s = ttk.Style()
            s.configure("Dark.TNotebook", background=_PANEL, borderwidth=0)
            s.configure("Dark.TNotebook.Tab",
                        background=_PANEL, foreground=_MUTED,
                        padding=(14, 8), borderwidth=0,
                        font=("Segoe UI", 10, "bold"))
            s.map("Dark.TNotebook.Tab",
                  background=[("selected", _PANEL2)],
                  foreground=[("selected", _ACCENT)])
            nb.configure(style="Dark.TNotebook")
        except Exception:
            pass

        # ── Recently Seen tab ───────────────────────────────────────────────
        seen_tab = tk.Frame(nb, bg=_PANEL)
        nb.add(seen_tab, text="Recently Seen")
        self._seen = _RecentlySeen(seen_tab)

        # ── Log tab ─────────────────────────────────────────────────────────
        log_tab = tk.Frame(nb, bg=_PANEL)
        nb.add(log_tab, text="Log")
        log_hdr = tk.Frame(log_tab, bg=_PANEL)
        log_hdr.pack(fill="x", padx=10, pady=(8, 2))
        tk.Button(log_hdr, text="Clear", command=self._clear_log,
                  bg=_BORDER, fg=_TEXT, relief="flat",
                  font=("Segoe UI", 8), cursor="hand2",
                  padx=5, pady=2).pack(side="right")

        self._log_box = scrolledtext.ScrolledText(
            log_tab, bg=_PANEL2, fg=_TEXT,
            font=("Consolas", 10), relief="flat", bd=0,
            state="disabled", wrap="word",
        )
        self._log_box.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        for tag, col in (("good", _GOOD), ("warn", _WARN),
                         ("error", _DANGER), ("accent", _ACCENT),
                         ("muted", _MUTED)):
            self._log_box.tag_config(tag, foreground=col)

    # ---- Game / method helpers --------------------------------------------

    def _on_game_change(self, *_):
        self._refresh_offset_status()
        self._refresh_method_options()

    def _current_methods(self):
        try:
            from pokebot.games import methods_for
            return methods_for(self._game_var.get())
        except Exception:
            return []

    def _refresh_method_options(self):
        methods = self._current_methods()
        labels = [m.label for m in methods]
        self._method_cb.configure(values=labels)
        # Preserve user's choice if still valid; otherwise pick a sane default.
        if self._method_var.get() not in labels:
            self._method_var.set(labels[0] if labels else "")
        self._refresh_starter_options()
        self._on_method_change()

    def _selected_method(self):
        for m in self._current_methods():
            if m.label == self._method_var.get():
                return m
        return None

    def _has_offsets_configured(self) -> bool:
        """True if the bot has a usable party_base for the detected game."""
        # Always re-read config.yaml so offsets pasted after the launcher
        # opened are picked up without a relaunch.
        try:
            self._cfg = _load_config()
        except Exception:
            pass
        # 1. config.yaml override.
        cfg = (self._cfg or {}).get("offsets") or {}
        for v in cfg.values():
            if not v:
                continue
            try:
                if (int(v, 0) if isinstance(v, str) else int(v)):
                    return True
            except (TypeError, ValueError):
                continue
        # 2. Game registry pre-set offsets.
        try:
            from pokebot.games import GAMES
            g = GAMES.get(self._game_var.get())
            if g and (g.offsets.party_base or g.offsets.foe_base):
                return True
        except Exception:
            pass
        return False

    def _refresh_starter_options(self):
        """Repopulate the starter sub-dropdown for the current game.

        Display names are properly capitalized ('Chespin', 'Fennekin'),
        but the lowercase form is what flows through to the CLI flag.
        """
        try:
            from pokebot.games import starters_for
            names = list(starters_for(self._game_var.get()).keys())
        except Exception:
            names = []
        display = [n.capitalize() for n in names]
        self._starter_cb.configure(values=display)
        if self._starter_var.get() not in display:
            self._starter_var.set(display[0] if display else "")

    def _on_method_change(self, *_):
        m = self._selected_method()
        # Method descriptions removed by request — keep only the
        # critical shiny-locked safety warning (rarely shown).
        warn = ("⚠ Shiny-locked: this Pokémon cannot be shiny in this "
                "game (see README)." if (m and m.shiny_locked) else "")
        self._method_warn.config(text=warn)

        # Show / hide the method-specific sub-dropdowns. Pack them
        # BEFORE the target divider so the layout inside Hunt card is
        # Method → (Starter or Movement) → Target.
        self._starter_frame.pack_forget()
        self._movement_frame.pack_forget()
        if m and m.label == "Starters":
            self._starter_frame.pack(fill="x", pady=(4, 0),
                                     before=self._target_divider)
            self._starter_hint.config(
                text="Save in front of the starter table — see "
                     "TUTORIAL.md for the exact position per game.")
        elif m and m.mode == "encounter":
            self._movement_frame.pack(fill="x", pady=(4, 0),
                                      before=self._target_divider)

    # ---- Live Azahar status polling ----------------------------------------

    def _status_poll_loop(self):
        # Lazy-imported so the poller works even if a stale `pokebot`
        # build is on disk; we re-import each tick.
        while self._poll_alive:
            status = {"state": "no_rpc"}
            try:
                from pokebot.citra_rpc import quick_status
                status = quick_status(timeout=0.4)
            except Exception:
                pass
            try:
                self.after(0, self._apply_status, status)
            except Exception:
                return
            time.sleep(2.0)

    def _apply_status(self, status: dict):
        state = status.get("state", "no_rpc")
        if state == "no_rpc":
            self._azahar_lbl.config(text="● Azahar not detected", fg=_DANGER)
            self._game_detect_lbl.config(
                text="Open Azahar and load a Gen 6/7 game.")
            self._game_display_lbl.config(text="Waiting for Azahar…",
                                          fg=_MUTED)
            if self._game_var.get():
                self._game_var.set("")
            self._last_detected_game = None
            return
        if state == "running":
            self._azahar_lbl.config(text="● Azahar running", fg=_WARN)
            self._game_detect_lbl.config(
                text="No Pokémon Gen 6/7 process loaded yet.")
            self._game_display_lbl.config(text="No game loaded", fg=_WARN)
            if self._game_var.get():
                self._game_var.set("")
            self._last_detected_game = None
            return
        if state == "game":
            self._azahar_lbl.config(text="● Azahar + game ready", fg=_GOOD)
            self._game_detect_lbl.config(
                text=f"Detected: {status.get('game', '?')}")
            self._game_display_lbl.config(
                text=status.get("game", "?"), fg=_GOOD)
            tid = status.get("title_id")
            if tid:
                try:
                    from pokebot.games import find_game_by_title_id
                    g = find_game_by_title_id(tid)
                except Exception:
                    g = None
                if g and g.key != self._last_detected_game:
                    self._last_detected_game = g.key
                    if self._game_var.get() != g.key:
                        self._game_var.set(g.key)
                        self._log(f"Auto-selected detected game: {g.key}",
                                  "accent")

    # ---- Offset status helper (no-op stub; kept so callers don't break) ----

    def _refresh_offset_status(self, *_):
        # The visual offset-status label was removed when auto-discovery
        # was added — the bot now logs its own offset state. This stub
        # keeps existing callers (game-change trace, post-scan reload)
        # working without blowing up.
        return

    # ---- Actions -----------------------------------------------------------

    _TARGET_FILTER_FLAG = {
        "Shiny only":               "shiny",
        "Any (first match)":        "any",
        "Perfect IVs (6×31)":       "perfect6",
        "5+ perfect IVs":           "perfect5",
        "Shiny + 4+ perfect IVs":   "shiny+perfect4",
    }

    def _start_bot(self):
        if self._bot.running:
            return
        if not self._game_var.get():
            messagebox.showwarning(
                "No game detected",
                "Azahar isn't running a Gen 6/7 game yet. Open Azahar "
                "and load your ROM, then try again.")
            return
        method = self._selected_method()
        if not method:
            messagebox.showwarning(
                "No method selected",
                "Pick a method from the dropdown first.")
            return
        # No more preflight modal — soft_reset auto-discovers offsets
        # after the first starter is in the party. The bot logs its
        # progress, and we just note it here so the user knows the first
        # iteration takes a bit longer than subsequent ones.
        if not self._has_offsets_configured():
            self._log("First run — bot will auto-discover memory offsets "
                      "after the first starter is received (~1 min one-time "
                      "scan). Subsequent resets are full-speed.", "warn")
        # Validate starter sub-selection when method requires one.
        if method.label == "Starters":
            picked = self._starter_var.get().strip()
            if not picked:
                messagebox.showwarning(
                    "Pick a starter",
                    "Select which starter to hunt from the dropdown.")
                return
            # Dropdown shows 'Chespin' but the bot expects 'chespin'.
            chosen_starter = picked.lower()
        else:
            chosen_starter = method.starter
        # Resolve the movement axis for encounter mode.
        chosen_movement = None
        if method.mode == "encounter":
            chosen_movement = (
                "vertical"
                if "Vertical" in self._movement_var.get()
                else "horizontal"
            )
        args = ["--mode", method.mode]
        game = self._game_var.get()
        if game:
            args += ["--game", game]
        if chosen_starter:
            args += ["--starter", chosen_starter]
        if chosen_movement:
            args += ["--movement", chosen_movement]
        flag = self._TARGET_FILTER_FLAG.get(self._target_var.get())
        if flag:
            args += ["--target", flag]
        if self._dry_var.get():
            args += ["--dry-run"]
        if self._verb_var.get():
            args += ["--verbose"]
        self._log(f"Starting bot — {method.label}", "accent")
        if method.shiny_locked:
            self._log("Note: target is shiny-locked. Bot will run but "
                      "this Pokémon cannot legitimately be shiny.",
                      "warn")
        self._bot.start(args)
        self._set_running(True)
        # Focus Azahar so keystrokes land there, not on the launcher
        # itself. The subprocess takes a moment to spin up, so give it
        # ~750ms before we steal foreground.
        self.after(750, self._focus_azahar)

    def _focus_azahar(self):
        try:
            from pokebot.platform_utils import focus_azahar
            ok = focus_azahar()
        except Exception as e:
            ok = False
            self._log(f"focus_azahar failed: {e}", "warn")
        if ok:
            self._log("Focused Azahar — keystrokes will go there now.",
                      "muted")
        else:
            self._log("Couldn't auto-focus Azahar. Click into the Azahar "
                      "window once if the bot's keys aren't landing.",
                      "warn")

    def _stop_bot(self):
        self._log("Stopping bot...", "warn")
        self._bot.stop()

    def _run_find_offsets(self):
        if self._bot.running:
            messagebox.showwarning(
                "Bot running",
                "Stop the bot before scanning for offsets.")
            return
        if self._offset_proc and self._offset_proc.poll() is None:
            messagebox.showinfo("Already scanning",
                                "Offset scan is already running.")
            return
        self._log("Starting offset scan (this may take several minutes)...",
                  "accent")
        self._log("Keep Azahar open with your game on the overworld.", "muted")
        self._scan_btn.config(state="disabled")
        cfg_path = ROOT / "config.yaml"
        # Pass --save-config so find_offsets writes the discovered values
        # straight into config.yaml. The user never has to edit YAML.
        self._offset_proc = subprocess.Popen(
            [sys.executable, "-m", "pokebot.find_offsets",
             "--save-config", str(cfg_path)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=str(ROOT),
        )
        wrote_offsets = {"ok": False, "summary": ""}
        def _drain():
            assert self._offset_proc and self._offset_proc.stdout
            for line in self._offset_proc.stdout:
                line = line.rstrip()
                # find_offsets emits "WROTE_OFFSETS: ..." on success.
                if "WROTE_OFFSETS:" in line:
                    wrote_offsets["ok"] = True
                    wrote_offsets["summary"] = line.split("WROTE_OFFSETS:", 1)[1].strip()
                    self._log_thread(line, "good")
                else:
                    self._log_thread(line, "muted")
            self._offset_proc.wait()
            if wrote_offsets["ok"]:
                self._log_thread(
                    "Offsets saved to config.yaml automatically. "
                    "You can press Start Bot now.", "good")
                # Force the launcher to re-read the config so the
                # offset preflight check passes immediately.
                try:
                    self._cfg = _load_config()
                except Exception:
                    pass
                self.after(0, self._refresh_offset_status)
            else:
                self._log_thread(
                    "Scan finished but couldn't auto-identify offsets. "
                    "Make sure your party has at least one Pokémon "
                    "and you're standing in the overworld, then re-scan.",
                    "warn")
            self.after(0, lambda: self._scan_btn.config(state="normal"))
        threading.Thread(target=_drain, daemon=True).start()

    # ---- Bot callbacks -----------------------------------------------------

    def _on_bot_line(self, line: str):
        # Structured event line emitted by dashboard_server.broadcast.
        if line.startswith("EVENT: "):
            try:
                import json as _json
                evt = _json.loads(line[len("EVENT: "):])
            except Exception:
                evt = None
            if evt:
                self.after(0, self._dispatch_event, evt)
                return  # don't pollute the log view with raw event JSON
        tag = ""
        ll = line.lower()
        if "error" in ll or "traceback" in ll or "exception" in ll:
            tag = "error"
        elif "warning" in ll or "warn" in ll:
            tag = "warn"
        elif "target hit" in ll or "shiny" in ll:
            tag = "accent"
        self._log_thread(line, tag)

    def _dispatch_event(self, evt: dict):
        kind = evt.get("type", "")
        # Visible event trace so it's clear what the bot is doing
        # without needing to dig through the bot's debug log.
        if kind == "ready":
            self._log("  ✓ event pipeline ok — bot is ready", "good")
        elif kind == "candidate":
            sp = evt.get("species", "?")
            star = " ★" if evt.get("shiny") else ""
            ivs = evt.get("ivs") or {}
            iv_sum = sum(int(v) for v in ivs.values()) if ivs else 0
            self._log(f"  → candidate: species #{sp}{star} (IV sum {iv_sum})",
                      "muted")
        elif kind == "encounter":
            sp = evt.get("species", "?")
            self._log(f"  → encounter: species #{sp}", "muted")
        elif kind == "target_hit":
            self._log(f"  ★ TARGET HIT — {evt.get('reason', '?')}", "good")
        elif kind == "read_failure":
            self._log(f"  ! read failure: {evt.get('reason', '?')}", "warn")
        elif kind == "offset_scan":
            st = evt.get("state", "?")
            target = evt.get("target", "party_base")
            if st == "started":
                self._log(f"  ⌕ scanning memory for {target}…", "muted")
            elif st == "ok":
                addr = evt.get(target, evt.get("party_base", 0))
                self._log(f"  ✓ found {target} = {addr:#010x}", "good")
            elif st == "fail":
                self._log(f"  ✗ {target} scan failed", "warn")
        elif kind == "soft_reset_attempt":
            self._log(f"  ↻ attempt #{evt.get('count', '?')}", "muted")

        # Encounters and starter candidates both populate the table.
        if kind in ("encounter", "candidate", "target_hit"):
            try:
                self._seen.add_pokemon(evt)
            except Exception as e:
                self._log(f"[seen] failed to render: {e}", "warn")
            # Also reflect slot 0 in the party strip up top.
            if kind == "candidate":
                try:
                    self._party.update_from_candidate(evt)
                except Exception as e:
                    self._log(f"[party] update failed: {e}", "warn")
        elif kind == "party":
            try:
                self._party.update_from_party(evt.get("slots", []))
            except Exception as e:
                self._log(f"[party] update failed: {e}", "warn")

    def _on_bot_exit(self, code: int):
        self._log_thread(
            f"Bot stopped (exit code {code})",
            "good" if code == 0 else "warn")
        self.after(0, self._set_running, False)

    # ---- Helpers -----------------------------------------------------------

    def _set_running(self, running: bool):
        if running:
            self._dot.config(fg=_GOOD)
            self._status_lbl.config(text="running", fg=_GOOD)
            self._start_btn.config(state="disabled",
                                   bg=_PANEL3, fg=_MUTED,
                                   activebackground=_PANEL3)
            self._stop_btn.config(state="normal",
                                  bg=_DANGER, fg="white",
                                  activebackground=_DANGER)
        else:
            self._dot.config(fg=_MUTED)
            self._status_lbl.config(text="stopped", fg=_MUTED)
            self._start_btn.config(state="normal",
                                   bg=_ACCENT, fg="white",
                                   activebackground=_ACCENT)
            self._stop_btn.config(state="disabled",
                                  bg=_PANEL3, fg=_MUTED,
                                  activebackground=_PANEL3)

    def _log(self, text: str, tag: str = ""):
        self._log_box.config(state="normal")
        self._log_box.insert("end", text + "\n", tag)
        self._log_box.see("end")
        self._log_box.config(state="disabled")

    def _log_thread(self, text: str, tag: str = ""):
        self.after(0, self._log, text, tag)

    def _clear_log(self):
        self._log_box.config(state="normal")
        self._log_box.delete("1.0", "end")
        self._log_box.config(state="disabled")

    def _on_close(self):
        self._poll_alive = False
        if self._bot.running:
            if messagebox.askyesno("Quit", "The bot is running. Stop it and quit?"):
                self._bot.stop()
                self.destroy()
        else:
            self.destroy()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    app = _App()
    app.mainloop()


if __name__ == "__main__":
    main()
