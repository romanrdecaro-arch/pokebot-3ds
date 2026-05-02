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
import platform
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).parent


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
        cmd = [sys.executable, str(ROOT / "run.py")] + extra_args
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=str(ROOT),
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

# Pure-black palette. Panels are barely-grey so cards still read against
# the background without going full GitHub-dark blue.
_BG     = "#000000"
_PANEL  = "#0a0a0a"
_PANEL2 = "#121212"   # slightly raised cards (e.g. log, status pill)
_BORDER = "#1f1f1f"
_TEXT   = "#f1f1f1"
_MUTED  = "#7a7a7a"
_ACCENT = "#ee1515"   # Pokéball red — primary brand accent
_ACCENT2 = "#3aa0ff"  # secondary cool accent for info chips
_GOOD   = "#38d977"
_WARN   = "#e2b53f"
_DANGER = "#ff4757"


def _draw_pokeball(canvas: tk.Canvas, size: int = 32) -> None:
    """Render a Pokéball glyph on a square Tk canvas."""
    pad = 2
    a, b = pad, size - pad
    mid_y_top    = (size // 2) - 2
    mid_y_bottom = (size // 2) + 2
    # Top red half + bottom white half. We use create_arc so each half
    # gets the proper rounded shape.
    canvas.create_arc(a, a, b, b, start=0,   extent=180,
                      fill=_ACCENT, outline="#0d0d0d", width=2)
    canvas.create_arc(a, a, b, b, start=180, extent=180,
                      fill="#f4f4f4", outline="#0d0d0d", width=2)
    # Equator band (covers the seam between the two halves).
    canvas.create_rectangle(a + 1, mid_y_top, b - 1, mid_y_bottom,
                            fill="#0d0d0d", outline="")
    # Center button: outer black, inner white.
    cx, cy = size // 2, size // 2
    canvas.create_oval(cx - 5, cy - 5, cx + 5, cy + 5,
                       fill="#0d0d0d", outline="")
    canvas.create_oval(cx - 3, cy - 3, cx + 3, cy + 3,
                       fill="#f4f4f4", outline="")


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


class _RecentlySeen(tk.Frame):
    """Sprite-rich encounter table that mirrors the pokebot-gen3 dashboard.

    Each row is a Frame containing: sprite, gender icon, level, PID,
    Shiny Value (PSV), ability id, nature, IV breakdown with per-stat
    colour, and Hidden Power type/power.
    """

    MAX_ROWS = 100  # how many recent encounters to keep on screen

    # Column layout: (label, weight, anchor, monospace?)
    _HEADERS = (
        ("",            0, "w", False),  # sprite
        ("",            0, "center", False),  # sex
        ("",            0, "center", False),  # level
        ("PID",         0, "center", True),
        ("Shiny Value", 0, "center", True),
        ("Ability",     1, "w", False),
        ("Nature",      0, "center", False),
        ("IVs",         1, "w", True),
        ("Hidden Power", 0, "w", False),
    )

    def __init__(self, parent):
        super().__init__(parent, bg=_PANEL)
        self.pack(fill="both", expand=True)

        # Title bar
        title_bar = tk.Frame(self, bg=_PANEL)
        title_bar.pack(fill="x", padx=14, pady=(10, 4))
        tk.Label(title_bar, text="Recently Seen",
                 bg=_PANEL, fg=_TEXT,
                 font=("Segoe UI", 13, "bold")).pack(side="left")
        self._counter_lbl = tk.Label(title_bar, text="0 encounters",
                                     bg=_PANEL, fg=_MUTED,
                                     font=("Segoe UI", 9))
        self._counter_lbl.pack(side="right")

        # Header row
        header = tk.Frame(self, bg=_PANEL)
        header.pack(fill="x", padx=14)
        for i, (text, w, anchor, _) in enumerate(self._HEADERS):
            header.grid_columnconfigure(i, weight=w)
            tk.Label(header, text=text, bg=_PANEL, fg=_MUTED,
                     font=("Segoe UI", 9, "bold"),
                     anchor=anchor).grid(row=0, column=i,
                                         padx=8, pady=(0, 6),
                                         sticky="ew")
        tk.Frame(self, bg=_BORDER, height=1).pack(fill="x", padx=14)

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
        self._count = 0

        # Empty placeholder
        self._empty = tk.Label(self._rows_frame,
                               text="No encounters yet — start a hunt.",
                               bg=_PANEL, fg=_MUTED,
                               font=("Segoe UI", 10, "italic"))
        self._empty.pack(pady=24)

    # ---- public API --------------------------------------------------------

    def add_pokemon(self, evt: dict):
        if self._empty:
            self._empty.destroy()
            self._empty = None
        self._count += 1
        self._counter_lbl.config(text=f"{self._count} encounters")

        row = self._build_row(evt)
        row.pack(fill="x", padx=4, pady=2)
        self._rows.insert(0, row)
        # Move newest row to top
        for r in self._rows:
            r.pack_forget()
        for r in self._rows[: self.MAX_ROWS]:
            r.pack(fill="x", padx=4, pady=2)
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
        row = tk.Frame(self._rows_frame, bg=bg, padx=6, pady=4,
                       highlightthickness=1,
                       highlightbackground="#ffd86b" if shiny else _BORDER)
        for i, (_, w, _, _) in enumerate(self._HEADERS):
            row.grid_columnconfigure(i, weight=w)

        # Sprite
        sprite_lbl = tk.Label(row, bg=bg)
        sprite_lbl.grid(row=0, column=0, padx=4, pady=2, sticky="w")
        self._load_sprite_async(species_id, shiny, sprite_lbl)

        # Sex
        sex_color = {"M": "#5fa9ff", "F": "#ff7eb6", "G": _MUTED}.get(gender, _MUTED)
        sex_glyph = {"M": "♂", "F": "♀", "G": "—"}.get(gender, "—")
        tk.Label(row, text=sex_glyph, bg=bg, fg=sex_color,
                 font=("Segoe UI", 11, "bold")).grid(row=0, column=1, padx=4)

        # Level
        lvl = f"Lv {level}" if level is not None else "Lv ?"
        tk.Label(row, text=lvl, bg=bg, fg=_TEXT,
                 font=("Segoe UI", 10, "bold")).grid(row=0, column=2, padx=8)

        # PID
        tk.Label(row, text=f"{pid:08X}", bg=bg, fg=_TEXT,
                 font=("Consolas", 10)).grid(row=0, column=3, padx=8)

        # Shiny Value (PSV) — gold pill if shiny, plain text otherwise
        psv_text = f"{int(psv):05d}" if psv is not None else "—"
        if shiny:
            sv_lbl = tk.Label(row, text=f"★ {psv_text}", bg=bg,
                              fg="#ffd86b", font=("Consolas", 10, "bold"))
        else:
            sv_lbl = tk.Label(row, text=f"— {psv_text}", bg=bg,
                              fg=_MUTED, font=("Consolas", 10))
        sv_lbl.grid(row=0, column=4, padx=8)

        # Ability
        ab_text = self._ability_text(ability_id, ability_num)
        tk.Label(row, text=ab_text, bg=bg, fg=_TEXT,
                 font=("Segoe UI", 10), anchor="w").grid(row=0, column=5,
                                                         padx=8, sticky="w")

        # Nature
        nat_text = nature if isinstance(nature, str) and nature else \
            (_NATURE_NAMES[int(nature)] if isinstance(nature, int)
             and 0 <= int(nature) < 25 else "?")
        tk.Label(row, text=nat_text, bg=bg, fg=_TEXT,
                 font=("Segoe UI", 10)).grid(row=0, column=6, padx=8)

        # IVs — per-stat coloured (red=31, blue=0, white otherwise)
        iv_frame = tk.Frame(row, bg=bg)
        iv_frame.grid(row=0, column=7, padx=8, sticky="w")
        order = ("HP", "Atk", "Def", "Spe", "SpA", "SpD")
        for i, stat in enumerate(order):
            v = int(ivs.get(stat, 0))
            color = _DANGER if v == 31 else (_ACCENT2 if v == 0 else _TEXT)
            weight = "bold" if v in (0, 31) else "normal"
            tk.Label(iv_frame, text=str(v), bg=bg, fg=color,
                     font=("Consolas", 10, weight),
                     width=3, anchor="center").pack(side="left", padx=1)
        iv_sum = sum(int(ivs.get(s, 0)) for s in order)
        tk.Label(iv_frame, text=f"({iv_sum})", bg=bg, fg=_MUTED,
                 font=("Consolas", 10)).pack(side="left", padx=(6, 0))

        # Hidden Power (type pill + power)
        hp_frame = tk.Frame(row, bg=bg)
        hp_frame.grid(row=0, column=8, padx=8, sticky="w")
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
        if not species_id:
            target.config(text="?", fg=_MUTED, width=4, height=2,
                          font=("Segoe UI", 12, "bold"))
            return
        cache_key = species_id * 2 + (1 if shiny else 0)
        if cache_key in self._sprites:
            target.config(image=self._sprites[cache_key])
            return
        # Spinner placeholder while we fetch
        target.config(text="…", fg=_MUTED, width=4, height=2,
                      font=("Segoe UI", 12))

        def _worker():
            try:
                from pokebot.sprites import get_sprite_path
                p = get_sprite_path(species_id, shiny=shiny)
            except Exception:
                p = None
            if p:
                try:
                    img = tk.PhotoImage(file=str(p))
                    self._sprites[cache_key] = img
                    target.after(0, lambda: target.config(image=img, text=""))
                    return
                except Exception:
                    pass
            target.after(0, lambda: target.config(
                text=f"#{species_id}", fg=_MUTED,
                font=("Consolas", 9)))

        threading.Thread(target=_worker, daemon=True).start()

    # ---- small helpers -----------------------------------------------------

    @staticmethod
    def _ability_text(ability_id, ability_num) -> str:
        if ability_id is None:
            return "?"
        suffix = ""
        if ability_num == 4:
            suffix = " (HA)"
        elif ability_num == 1:
            suffix = " (1)"
        elif ability_num == 2:
            suffix = " (2)"
        return f"#{ability_id}{suffix}"


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
        # Pokéball logo
        logo = tk.Canvas(hdr, width=34, height=34,
                         bg=_PANEL, highlightthickness=0)
        logo.pack(side="left", padx=(0, 12))
        _draw_pokeball(logo, 34)
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

        # Thin divider under the header
        tk.Frame(self, bg=_BORDER, height=1).pack(fill="x")

        # ── Body: sidebar + log ─────────────────────────────────────────────
        body = tk.Frame(self, bg=_BG)
        body.pack(fill="both", expand=True, padx=10, pady=10)

        side = tk.Frame(body, bg=_PANEL, width=260)
        side.pack(side="left", fill="y", padx=(0, 10))
        side.pack_propagate(False)
        self._build_sidebar(side)

        right = tk.Frame(body, bg=_PANEL)
        right.pack(side="left", fill="both", expand=True)
        self._build_log(right)

    def _lbl(self, parent, text):
        tk.Label(parent, text=text, bg=_PANEL, fg=_MUTED,
                 font=("Segoe UI", 9, "bold"),
                 anchor="w").pack(fill="x", padx=12, pady=(12, 2))

    def _sep(self, parent):
        tk.Frame(parent, bg=_BORDER, height=1).pack(fill="x", padx=8, pady=4)

    def _build_sidebar(self, p):
        # ── Azahar live status ──────────────────────────────────────────────
        self._lbl(p, "AZAHAR STATUS")
        self._azahar_lbl = tk.Label(p, text="● checking…",
                                    bg=_PANEL, fg=_MUTED,
                                    font=("Segoe UI", 10), anchor="w")
        self._azahar_lbl.pack(fill="x", padx=12, pady=(0, 1))
        self._game_detect_lbl = tk.Label(p, text="",
                                         bg=_PANEL, fg=_MUTED,
                                         font=("Segoe UI", 9), anchor="w",
                                         wraplength=205, justify="left")
        self._game_detect_lbl.pack(fill="x", padx=12, pady=(0, 4))

        # ── Game (auto-detected from Azahar) ────────────────────────────────
        self._lbl(p, "GAME")
        # The game is detected from Azahar's process list; the var still
        # backs the starter sub-dropdown filtering and CLI args, but the
        # user no longer picks it manually.
        self._game_var = tk.StringVar(value="")
        self._game_display_lbl = tk.Label(
            p, text="Waiting for Azahar…", bg=_PANEL, fg=_MUTED,
            font=("Segoe UI", 10, "bold"), anchor="w",
            wraplength=235, justify="left")
        self._game_display_lbl.pack(fill="x", padx=12, pady=2)
        self._game_var.trace_add("write", self._on_game_change)

        # ── Method ──────────────────────────────────────────────────────────
        self._lbl(p, "METHOD")
        self._method_var = tk.StringVar(value="")
        self._method_cb = ttk.Combobox(p, textvariable=self._method_var,
                                       values=[],
                                       state="readonly", width=30,
                                       style="Dark.TCombobox")
        self._method_cb.pack(padx=12, pady=2)
        # Inline shiny-locked / contextual warning under the dropdown
        self._method_warn = tk.Label(p, text="", bg=_PANEL, fg=_WARN,
                                     font=("Segoe UI", 9), anchor="w",
                                     wraplength=235, justify="left")
        self._method_warn.pack(fill="x", padx=12, pady=(2, 0))
        self._method_var.trace_add("write", self._on_method_change)

        # ── Starter sub-dropdown (visible when method=Starters) ─────────────
        self._starter_frame = tk.Frame(p, bg=_PANEL)
        self._lbl(self._starter_frame, "STARTER")
        self._starter_var = tk.StringVar(value="")
        self._starter_cb = ttk.Combobox(self._starter_frame,
                                        textvariable=self._starter_var,
                                        values=[],
                                        state="readonly", width=24,
                                        style="Dark.TCombobox")
        self._starter_cb.pack(padx=12, pady=2)
        self._starter_hint = tk.Label(self._starter_frame, text="",
                                      bg=_PANEL, fg=_MUTED,
                                      font=("Segoe UI", 9), anchor="w",
                                      wraplength=235, justify="left")
        self._starter_hint.pack(fill="x", padx=12, pady=(2, 0))
        self._refresh_method_options()

        # ── Target filter (replaces YAML editing for common cases) ──────────
        self._lbl(p, "TARGET FILTER")
        self._target_choices = [
            "From config.yaml",
            "Any (first match)",
            "Shiny only",
            "Perfect IVs (6×31)",
            "5+ perfect IVs",
            "Shiny + 4+ perfect IVs",
        ]
        self._target_var = tk.StringVar(value="From config.yaml")
        self._target_cb = ttk.Combobox(p, textvariable=self._target_var,
                                       values=self._target_choices,
                                       state="readonly", width=24,
                                       style="Dark.TCombobox")
        self._target_cb.pack(padx=12, pady=2)

        # ── Options ──────────────────────────────────────────────────────────
        self._sep(p)
        self._dry_var = tk.BooleanVar(value=False)
        tk.Checkbutton(p, text="Dry run",
                       variable=self._dry_var,
                       bg=_PANEL, fg=_TEXT, selectcolor=_PANEL,
                       activebackground=_PANEL,
                       font=("Segoe UI", 10)).pack(anchor="w", padx=16,
                                                    pady=(2, 0))
        tk.Label(p, bg=_PANEL, fg=_MUTED,
                 font=("Segoe UI", 8, "italic"),
                 anchor="w", wraplength=235, justify="left",
                 text="Bot logs the keys it would press but doesn't "
                      "actually send them to Azahar. Use to verify "
                      "memory reads work without controlling the game."
                 ).pack(anchor="w", padx=34, pady=(0, 4))

        self._verb_var = tk.BooleanVar(value=False)
        tk.Checkbutton(p, text="Verbose logging",
                       variable=self._verb_var,
                       bg=_PANEL, fg=_TEXT, selectcolor=_PANEL,
                       activebackground=_PANEL,
                       font=("Segoe UI", 10)).pack(anchor="w", padx=16,
                                                    pady=(2, 0))
        tk.Label(p, bg=_PANEL, fg=_MUTED,
                 font=("Segoe UI", 8, "italic"),
                 anchor="w", wraplength=235, justify="left",
                 text="DEBUG-level log output: every RPC call, parse "
                      "result, and decision. Noisy but invaluable for "
                      "diagnosing why the bot isn't behaving."
                 ).pack(anchor="w", padx=34, pady=(0, 4))

        # ── Start / Stop ─────────────────────────────────────────────────────
        self._sep(p)
        self._start_btn = self._btn(p, "▶  Start Bot", self._start_bot,
                                    bg=_ACCENT)
        self._stop_btn  = self._btn(p, "■  Stop Bot",  self._stop_bot,
                                    bg=_PANEL2, fg=_MUTED, state="disabled",
                                    subtle=True)

        # ── Tooling ──────────────────────────────────────────────────────────
        self._sep(p)
        self._btn(p, "Open Dashboard in Browser",
                  self._open_dashboard, bg=_PANEL2, fg=_GOOD, subtle=True)
        self._btn(p, "⚙  Edit config.yaml",
                  self._open_config, bg=_PANEL2, fg=_TEXT, subtle=True)
        # Hidden manual-override scan button; kept so power users can
        # rerun find_offsets via the existing _run_find_offsets path.
        # Auto-discovery covers the common case so it's not in the UI.
        self._scan_btn = tk.Button(p, command=self._run_find_offsets)
        self._offset_lbl = tk.Label(p)  # legacy; no longer packed

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
                  foreground=[("selected", _TEXT)])
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
        """Repopulate the starter sub-dropdown for the current game."""
        try:
            from pokebot.games import starters_for
            names = list(starters_for(self._game_var.get()).keys())
        except Exception:
            names = []
        self._starter_cb.configure(values=names)
        if self._starter_var.get() not in names:
            self._starter_var.set(names[0] if names else "")

    def _on_method_change(self, *_):
        m = self._selected_method()
        warn_parts = []
        if m and m.shiny_locked:
            warn_parts.append("⚠ Shiny-locked: this Pokémon cannot be "
                              "shiny in this game (see README).")
        if m and m.notes:
            warn_parts.append(m.notes)
        self._method_warn.config(text="\n".join(warn_parts))

        # Show / hide the starter sub-dropdown based on method.
        if m and m.label == "Starters":
            self._starter_frame.pack(fill="x", pady=(4, 0))
            self._starter_hint.config(
                text="Save in front of the starter table — see "
                     "TUTORIAL.md for the exact position per game.")
        else:
            self._starter_frame.pack_forget()

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
        "From config.yaml":         None,
        "Any (first match)":        "any",
        "Shiny only":               "shiny",
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
            chosen_starter = picked
        else:
            chosen_starter = method.starter
        args = ["--mode", method.mode]
        game = self._game_var.get()
        if game:
            args += ["--game", game]
        if chosen_starter:
            args += ["--starter", chosen_starter]
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

    def _stop_bot(self):
        self._log("Stopping bot...", "warn")
        self._bot.stop()

    def _open_dashboard(self):
        html = ROOT / "dashboard" / "dashboard.html"
        if html.exists():
            webbrowser.open("file:///" + str(html).replace("\\", "/"))
            self._log("Opened dashboard in browser.", "good")
        else:
            messagebox.showerror("Not found",
                                 f"dashboard.html not found at:\n{html}")

    def _open_config(self):
        cfg = ROOT / "config.yaml"
        if not cfg.exists():
            messagebox.showinfo("Not found", f"config.yaml not found at:\n{cfg}")
            return
        try:
            system = platform.system()
            if system == "Windows":
                os.startfile(str(cfg))  # type: ignore[attr-defined]
            elif system == "Darwin":
                subprocess.Popen(["open", str(cfg)])
            else:
                subprocess.Popen(["xdg-open", str(cfg)])
        except Exception as exc:
            messagebox.showerror("Could not open",
                                 f"Failed to open config.yaml: {exc}")

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
        # Encounters and starter candidates both populate the table.
        if kind in ("encounter", "candidate", "target_hit"):
            try:
                self._seen.add_pokemon(evt)
            except Exception as e:
                self._log(f"[seen] failed to render: {e}", "warn")

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
                                   bg=_PANEL2, fg=_MUTED,
                                   activebackground=_PANEL2)
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
                                  bg=_PANEL2, fg=_MUTED,
                                  activebackground=_PANEL2)

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
