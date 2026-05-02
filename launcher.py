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


class _App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("pokebot-3ds")
        self.geometry("1000x660")
        self.minsize(780, 480)
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

        # ── Game ────────────────────────────────────────────────────────────
        self._lbl(p, "GAME")
        try:
            from pokebot import games as gmod
            keys = list(gmod.GAMES.keys())
        except Exception:
            keys = []
        default_game = self._cfg.get("game", keys[0] if keys else "")
        self._game_var = tk.StringVar(value=default_game)
        self._game_cb = ttk.Combobox(p, textvariable=self._game_var,
                                     values=keys, state="readonly", width=24,
                                     style="Dark.TCombobox")
        self._game_cb.pack(padx=12, pady=2)
        self._game_var.trace_add("write", self._on_game_change)

        # ── Mode ─────────────────────────────────────────────────────────────
        self._lbl(p, "MODE")
        self._mode_var = tk.StringVar(value=self._cfg.get("mode", "observe"))
        for m in ("observe", "encounter", "soft_reset"):
            tk.Radiobutton(p, text=m, variable=self._mode_var, value=m,
                           bg=_PANEL, fg=_TEXT, selectcolor=_PANEL,
                           activebackground=_PANEL,
                           font=("Segoe UI", 10)).pack(anchor="w", padx=16)
        self._mode_var.trace_add("write", self._refresh_starter_visibility)

        # ── Starter (only meaningful for soft_reset) ────────────────────────
        self._starter_frame = tk.Frame(p, bg=_PANEL)
        self._starter_frame.pack(fill="x", pady=(4, 0))
        self._lbl(self._starter_frame, "STARTER (soft-reset)")
        self._starter_var = tk.StringVar(value="(any)")
        self._starter_cb = ttk.Combobox(self._starter_frame,
                                        textvariable=self._starter_var,
                                        values=["(any)"],
                                        state="readonly", width=24,
                                        style="Dark.TCombobox")
        self._starter_cb.pack(padx=12, pady=2)
        self._refresh_starter_options()
        self._refresh_starter_visibility()

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
        tk.Checkbutton(p, text="Dry run (no keypresses)",
                       variable=self._dry_var,
                       bg=_PANEL, fg=_TEXT, selectcolor=_PANEL,
                       activebackground=_PANEL,
                       font=("Segoe UI", 10)).pack(anchor="w", padx=16, pady=2)

        self._verb_var = tk.BooleanVar(value=False)
        tk.Checkbutton(p, text="Verbose logging",
                       variable=self._verb_var,
                       bg=_PANEL, fg=_TEXT, selectcolor=_PANEL,
                       activebackground=_PANEL,
                       font=("Segoe UI", 10)).pack(anchor="w", padx=16, pady=2)

        # ── Start / Stop ─────────────────────────────────────────────────────
        self._sep(p)
        self._start_btn = self._btn(p, "▶  Start Bot", self._start_bot,
                                    bg=_ACCENT)
        self._stop_btn  = self._btn(p, "■  Stop Bot",  self._stop_bot,
                                    bg=_PANEL2, fg=_MUTED, state="disabled",
                                    subtle=True)

        # ── Dashboard ────────────────────────────────────────────────────────
        self._sep(p)
        self._btn(p, "Open Dashboard in Browser",
                  self._open_dashboard, bg=_GOOD)

        # ── Tooling ──────────────────────────────────────────────────────────
        self._sep(p)
        self._lbl(p, "TOOLS")
        self._scan_btn = self._btn(p, "🔍  Find Offsets (scan RAM)",
                                   self._run_find_offsets,
                                   bg=_PANEL2, fg=_ACCENT2, subtle=True)
        self._btn(p, "⚙  Edit config.yaml",
                  self._open_config, bg=_PANEL2, fg=_TEXT, subtle=True)

        # ── Offset status ────────────────────────────────────────────────────
        self._sep(p)
        self._offset_lbl = tk.Label(p, text="", bg=_PANEL, fg=_WARN,
                                    font=("Segoe UI", 9), anchor="w",
                                    wraplength=205, justify="left")
        self._offset_lbl.pack(fill="x", padx=12, pady=4)
        self._refresh_offset_status()

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
        hdr = tk.Frame(parent, bg=_PANEL)
        hdr.pack(fill="x", padx=10, pady=(8, 2))
        tk.Label(hdr, text="LOG", bg=_PANEL, fg=_MUTED,
                 font=("Segoe UI", 9, "bold")).pack(side="left")
        tk.Button(hdr, text="Clear", command=self._clear_log,
                  bg=_BORDER, fg=_TEXT, relief="flat",
                  font=("Segoe UI", 8), cursor="hand2",
                  padx=5, pady=2).pack(side="right")

        self._log_box = scrolledtext.ScrolledText(
            parent, bg="#0a0d12", fg=_TEXT,
            font=("Consolas", 10), relief="flat", bd=0,
            state="disabled", wrap="word",
        )
        self._log_box.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        for tag, col in (("good", _GOOD), ("warn", _WARN),
                         ("error", _DANGER), ("accent", _ACCENT),
                         ("muted", _MUTED)):
            self._log_box.tag_config(tag, foreground=col)

    # ---- Game / starter helpers --------------------------------------------

    def _on_game_change(self, *_):
        self._refresh_offset_status()
        self._refresh_starter_options()

    def _refresh_starter_options(self):
        try:
            from pokebot.games import starters_for
            names = list(starters_for(self._game_var.get()).keys())
        except Exception:
            names = []
        values = ["(any)"] + names
        self._starter_cb.configure(values=values)
        if self._starter_var.get() not in values:
            self._starter_var.set("(any)")

    def _refresh_starter_visibility(self, *_):
        if self._mode_var.get() == "soft_reset":
            self._starter_frame.pack(fill="x", pady=(4, 0))
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
            self._last_detected_game = None
            return
        if state == "running":
            self._azahar_lbl.config(text="● Azahar running", fg=_WARN)
            self._game_detect_lbl.config(
                text="No Pokémon Gen 6/7 process loaded yet.")
            self._last_detected_game = None
            return
        if state == "game":
            self._azahar_lbl.config(text="● Azahar + game ready", fg=_GOOD)
            self._game_detect_lbl.config(
                text=f"Detected: {status.get('game', '?')}")
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

    # ---- Offset status helper ----------------------------------------------

    def _refresh_offset_status(self, *_):
        try:
            from pokebot import games as gmod
            g = gmod.GAMES.get(self._game_var.get())
        except Exception:
            g = None

        cfg_off = self._cfg.get("offsets") or {}
        has_cfg = False
        for v in cfg_off.values():
            if not v:
                continue
            try:
                n = int(v, 0) if isinstance(v, str) else int(v)
            except (TypeError, ValueError):
                continue
            if n:
                has_cfg = True
                break

        if has_cfg:
            self._offset_lbl.config(
                fg=_GOOD,
                text="✓ Offsets set in config.yaml")
        elif g and (g.offsets.party_base or g.offsets.foe_base):
            self._offset_lbl.config(
                fg=_GOOD,
                text="✓ Offsets in game registry")
        else:
            self._offset_lbl.config(
                fg=_WARN,
                text="⚠ No offsets found.\nRun 'Find Offsets', then paste\nresults into config.yaml")

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
        args = ["--mode", self._mode_var.get()]
        game = self._game_var.get()
        if game:
            args += ["--game", game]
        if self._mode_var.get() == "soft_reset":
            starter = self._starter_var.get()
            if starter and starter != "(any)":
                args += ["--starter", starter]
        flag = self._TARGET_FILTER_FLAG.get(self._target_var.get())
        if flag:
            args += ["--target", flag]
        if self._dry_var.get():
            args += ["--dry-run"]
        if self._verb_var.get():
            args += ["--verbose"]
        self._log("Starting bot...", "accent")
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
        self._offset_proc = subprocess.Popen(
            [sys.executable, "-m", "pokebot.find_offsets"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=str(ROOT),
        )
        def _drain():
            assert self._offset_proc and self._offset_proc.stdout
            for line in self._offset_proc.stdout:
                self._log_thread(line.rstrip(), "muted")
            self._offset_proc.wait()
            self._log_thread(
                "Scan complete. Copy the party_base/foe_base addresses above "
                "into the offsets: section of config.yaml, then restart the bot.",
                "good")
            self.after(0, lambda: self._scan_btn.config(state="normal"))
        threading.Thread(target=_drain, daemon=True).start()

    # ---- Bot callbacks -----------------------------------------------------

    def _on_bot_line(self, line: str):
        tag = ""
        ll = line.lower()
        if "error" in ll or "traceback" in ll or "exception" in ll:
            tag = "error"
        elif "warning" in ll or "warn" in ll:
            tag = "warn"
        elif "target hit" in ll or "shiny" in ll:
            tag = "accent"
        self._log_thread(line, tag)

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
