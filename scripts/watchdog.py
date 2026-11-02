"""
Keep a hunt running while nobody is watching.

    python scripts/watchdog.py --mode gen7_encounter --game USUM-USA-1.2 --target shiny

Every 30 s it checks that a bot is alive and still logging encounters
(logs/events.jsonl). A bot that has exited, or gone 5 minutes without an
event, is stopped and started again with the arguments given here (pass
the same ones the launcher uses). It never restarts over a shiny: when the
last thing logged is a shiny or a target hit with no catch after it, the
battle is left on screen and the watchdog exits. More than 5 restarts in
an hour means something a restart cannot fix, so it gives up then too.

Its own log is logs/watchdog.log; a bot it starts writes to
logs/watchdog-bot.log. Close its window to stop it.

shortcut: a bot started here is not the launcher's child, so the
launcher's stats panel, overlay and Discord alerts stop following it until
Start is pressed there again.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EVENTS = REPO / "logs" / "events.jsonl"
LOG = REPO / "logs" / "watchdog.log"
BOT_LOG = REPO / "logs" / "watchdog-bot.log"
POLL_S, STALL_S = 30, 300
MAX_RESTARTS, WINDOW_S = 5, 3600
#: Stop reasons a restart cannot fix (the bot logs "stop requested: ...").
#: The breeding ones too: after "decline failed" a restart would take
#: another egg.
LEAVE = ("started in a battle", "not a Gen 7 game", "decline failed",
         "egg not shiny", "shiny egg declined", "no Masuda", "nursery",
         "not Ultra Sun", "no shiny egg ahead")


def log(msg: str) -> None:
    line = f"{time.strftime('%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def tail_events(n_bytes: int = 200_000) -> list[dict]:
    try:
        with EVENTS.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - n_bytes))
            lines = f.read().decode("utf-8", "replace").splitlines()[1:]
    except OSError:
        return []
    out = []
    for line in lines:                  # the first may be cut, so it is dropped
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def shiny_pending(events: list[dict]) -> bool:
    """The hunt last saw a shiny / target and did not catch it."""
    for e in reversed(events):
        kind = e.get("type")
        if kind == "encounter":
            return bool(e.get("shiny"))   # died before it could even report
        if kind in ("target_hit", "target_caught"):
            return kind == "target_hit"
    return False


def last_stop_reason(text: str) -> str | None:
    found = re.findall(r"stop requested: (.+)", text)
    return found[-1].strip() if found else None


def bots() -> list[int] | None:
    """PIDs of running bots (run.py with a --mode), the launcher's included;
    None when the process list could not be read."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
          "Where-Object { $_.CommandLine -match 'run\\.py.*--mode' } | "
          "ForEach-Object { $_.ProcessId }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None                     # never read "no bots" from a failure
    return [int(x) for x in r.stdout.split() if x.isdigit()]


def kill(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                   capture_output=True)


def start(args: list[str]) -> subprocess.Popen:
    out = BOT_LOG.open("a", encoding="utf-8")
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    return subprocess.Popen([sys.executable, "-u", str(REPO / "run.py"), *args],
                            cwd=REPO, stdout=out, stderr=subprocess.STDOUT,
                            env=env)


def main(args: list[str]) -> int:
    if not args:
        print(__doc__)
        return 2
    log(f"watching: run.py {' '.join(args)}")
    restarts: list[float] = []
    started_at, log_at = 0.0, 0
    while True:
        time.sleep(POLL_S)
        pids = bots()
        if pids is None:
            continue
        events = tail_events()
        if shiny_pending(events):
            log("a shiny / target is waiting in battle: leaving it for you. "
                "Watchdog stopped.")
            return 0
        if started_at and not pids and BOT_LOG.exists():
            with BOT_LOG.open("rb") as f:
                f.seek(log_at)
                reason = last_stop_reason(f.read().decode("utf-8", "replace"))
            if reason and reason.startswith(LEAVE):
                log(f"the bot stopped with '{reason}', which a restart cannot "
                    f"fix. Watchdog stopped.")
                return 1
        now = time.time()
        last = max(events[-1].get("ts", 0) if events else 0, started_at)
        if pids and now - last < STALL_S:
            continue
        why = (f"no event for {(now - last) / 60:.0f} min" if pids
               else "the bot is not running")
        restarts = [t for t in restarts if now - t < WINDOW_S]
        if len(restarts) >= MAX_RESTARTS:
            log(f"{why}, after {MAX_RESTARTS} restarts in an hour: giving up. "
                f"Watchdog stopped.")
            return 1
        for pid in pids:
            kill(pid)
        log(f"{why}: restarting ({len(restarts) + 1} this hour)")
        log_at = BOT_LOG.stat().st_size if BOT_LOG.exists() else 0
        start(args)
        started_at = time.time()
        restarts.append(started_at)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
