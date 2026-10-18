"""
Post to a Discord channel through a webhook when a shiny turns up.

The webhook URL is a secret (anyone holding it can post as the hook), so
it lives in ``discord.local.yaml``, which git ignores and the updater
preserves, and it is never logged.
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

_HOSTS = ("discord.com", "discordapp.com", "ptb.discord.com",
          "canary.discord.com")
GOLD = 0xFFD86B


class DiscordError(RuntimeError):
    """The webhook refused the message or could not be reached."""


def valid_webhook(url: str) -> bool:
    """Only an https Discord webhook URL; anything else is refused, so a
    pasted typo or another site never receives the hunt's posts."""
    url = (url or "").strip()
    return any(url.startswith(f"https://{host}/api/webhooks/")
               for host in _HOSTS) and len(url) < 300


def shiny_message(name: str, sprite: str, phase: int, shinies: int,
                  nature: str = "", ivs: Optional[list] = None,
                  game: str = "") -> dict:
    fields = [{"name": "Phase", "value": f"{phase:,} encounters", "inline": True},
              {"name": "Shinies so far", "value": f"{shinies:,}", "inline": True}]
    if nature:
        fields.append({"name": "Nature", "value": nature, "inline": True})
    if ivs:
        fields.append({"name": "IVs", "value": "/".join(str(v) for v in ivs),
                       "inline": True})
    embed = {"title": f"Shiny {name}!", "color": GOLD, "fields": fields,
             "thumbnail": {"url": sprite}}
    if game:
        embed["footer"] = {"text": f"pokebot-3ds · {game}"}
    return {"username": "pokebot-3ds", "embeds": [embed]}


def send(url: str, message: dict, timeout: float = 10.0) -> None:
    if not valid_webhook(url):
        raise DiscordError("that is not a Discord webhook URL "
                           "(https://discord.com/api/webhooks/...)")
    req = urllib.request.Request(
        url.strip(), data=json.dumps(message).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "pokebot-3ds"})
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            pass
    except urllib.error.HTTPError as exc:
        raise DiscordError(f"Discord refused the message (HTTP {exc.code})") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise DiscordError(f"could not reach Discord ({exc})") from exc


def send_async(url: str, message: dict,
               on_done: Callable[[Optional[str]], None]) -> None:
    """Send off the caller's thread; ``on_done(None)`` or ``on_done(error)``."""
    def run():
        try:
            send(url, message)
            on_done(None)
        except DiscordError as exc:
            on_done(str(exc))
    threading.Thread(target=run, daemon=True, name="DiscordPost").start()


def load_webhook(path: Path) -> str:
    """The saved webhook URL, or "" if none is saved."""
    try:
        from .config_io import load_config
        return str((load_config(path) or {}).get("webhook_url") or "").strip()
    except Exception as exc:
        log.warning(f"could not read {path.name}: {exc}")
        return ""


def save_webhook(path: Path, webhook_url: str) -> None:
    # json.dumps gives a quoted string, which is valid YAML for any URL
    path.write_text(f"webhook_url: {json.dumps(webhook_url.strip())}\n",
                    encoding="utf-8")
