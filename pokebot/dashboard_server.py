"""
Event sink that prints to the terminal instead of running a websocket
server.

Used to be a full websocket dashboard; the user only ever wanted live
encounter data in the terminal, so the socket bits are gone. Same
``DashboardServer`` class name + ``broadcast/start/stop/inbox`` API so
the bot modes don't need to change — they still call
``ctx.dashboard.broadcast(...)`` and we render it as a human-readable
line plus the ``EVENT: <json>`` line the launcher's Recently Seen tab
still parses.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from queue import Queue

log = logging.getLogger(__name__)


_IV_ORDER = ("HP", "Atk", "Def", "SpA", "SpD", "Spe")


def _format_encounter(f: dict) -> str:
    sp = f.get("species", "?")
    lvl = f.get("level")
    shiny = " *SHINY*" if f.get("shiny") else ""
    nature = f.get("nature", "?")
    gender = f.get("gender", "G")
    ivs = f.get("ivs") or {}
    iv_str = "/".join(str(int(ivs.get(s, 0))) for s in _IV_ORDER)
    iv_sum = sum(int(ivs.get(s, 0)) for s in _IV_ORDER)
    pid = int(f.get("pid", 0))
    nick = f.get("nickname") or f"#{sp}"
    lvl_str = f"Lv{lvl}" if lvl is not None else "Lv?"

    extras: list[str] = []
    markings = f.get("markings") or []
    if markings:
        extras.append(f"marks={'+'.join(markings)}")
    pokerus = f.get("pokerus") or {}
    if pokerus.get("infected"):
        extras.append(f"PKRS infected ({pokerus.get('days_left', '?')}d)")
    elif pokerus.get("cured"):
        extras.append("PKRS cured")
    enc_type = f.get("encounter_type")
    enc_type_id = f.get("encounter_type_id", 0)
    # Skip the noisy default-zero "Egg / Hatched / Special Event" tag
    # for live wild encounters where it's almost always 0.
    if enc_type and enc_type_id != 0:
        extras.append(f"via={enc_type}")
    ot_game = f.get("ot_game")
    ot_game_id = f.get("ot_game_id", 0)
    if ot_game and ot_game_id != 0 and "unknown" not in ot_game:
        extras.append(f"OT-game={ot_game}")
    extras_line = ("  " + "  ".join(extras)) if extras else ""

    try:
        from .abilities import ability_name
        ab = ability_name(f.get("ability_id"))
    except Exception:
        ab = f"#{f.get('ability_id', '?')}"
    return (f"  [enc #{f.get('count', '?')}] {nick} {lvl_str} {gender}{shiny}\n"
            f"             nature={nature}  IVs {iv_str} ({iv_sum})  "
            f"PID={pid:08X}  ability={ab}"
            + (f"\n             {extras_line.strip()}" if extras_line else ""))


def _format_target_hit(f: dict) -> str:
    return (f"  *** TARGET HIT *** enc#{f.get('count', '?')}: "
            f"{f.get('reason', '?')}")


def _format_offset_scan(f: dict) -> str | None:
    target = f.get("target", "party_base")
    state = f.get("state", "?")
    if state == "started":
        return f"  [scan {target}] started"
    if state == "ok":
        addr = f.get(target, f.get("party_base", 0))
        return f"  [scan {target}] OK -> {int(addr):#010x}"
    if state == "fail":
        return f"  [scan {target}] FAILED"
    return None


def _format_candidate(f: dict) -> str:
    sp = f.get("species", "?")
    star = " ★" if f.get("shiny") else ""
    ivs = f.get("ivs") or {}
    iv_sum = sum(int(ivs.get(s, 0)) for s in _IV_ORDER) if ivs else 0
    return f"  [candidate] species #{sp}{star} IV-sum {iv_sum}"


def _format_read_failure(f: dict) -> str:
    return f"  [read-fail] {f.get('reason', '?')}"


_FORMATTERS = {
    "encounter":    _format_encounter,
    "target_hit":   _format_target_hit,
    "offset_scan":  _format_offset_scan,
    "candidate":    _format_candidate,
    "read_failure": _format_read_failure,
}


class DashboardServer:
    """Terminal event sink. Drop-in replacement for the old
    websocket-backed DashboardServer.

    Keeps the same surface area:
      - ``start()`` / ``stop()`` (no-ops, kept for API compatibility)
      - ``broadcast(msg_type, **fields)`` prints a human-readable line +
        an ``EVENT: <json>`` line for the launcher to parse
      - ``inbox`` Queue (unused now that there's no client connection,
        but kept so ``Bot._dashboard_command_loop`` doesn't break)
    """

    def __init__(self, **_unused: object):
        self._stop = threading.Event()
        self.inbox: Queue = Queue()

    # --- lifecycle (no-ops) -----------------------------------------------
    def start(self) -> None:
        log.info("Event sink: terminal-only (no websocket).")

    def stop(self) -> None:
        self._stop.set()

    # --- broadcasting ------------------------------------------------------
    def broadcast(self, msg_type: str, **fields: object) -> None:
        """Print one readable line for humans + an EVENT: JSON line for
        the launcher's encounter table.
        """
        # Human-readable line first.
        formatter = _FORMATTERS.get(msg_type)
        if formatter is not None:
            try:
                line = formatter(fields)
            except Exception as e:
                line = f"  [{msg_type}] (format error: {e})"
            if line:
                try:
                    sys.stdout.write(line + "\n")
                    sys.stdout.flush()
                except Exception:
                    pass

        # EVENT: JSON line — preserves the launcher's Recently Seen
        # rendering. Skip status/ready/party to keep the JSON channel
        # focused on encounter data only.
        body = {"type": msg_type, "ts": time.time(), **fields}
        try:
            payload = json.dumps(body, default=str)
        except Exception as e:
            log.error(f"broadcast({msg_type!r}) JSON-encode failed: {e}")
            return
        try:
            sys.stdout.write("EVENT: " + payload + "\n")
            sys.stdout.flush()
        except Exception:
            pass
