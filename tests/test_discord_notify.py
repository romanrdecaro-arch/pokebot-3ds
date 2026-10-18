"""
pokebot/discord_notify.py: shiny posts to a Discord webhook.
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot import discord_notify as dn  # noqa: E402

HOOK = "https://discord.com/api/webhooks/123456789/abcDEF-token"


@pytest.mark.parametrize("url,ok", [
    (HOOK, True),
    ("https://discordapp.com/api/webhooks/1/x", True),
    ("  " + HOOK + "  ", True),
    ("http://discord.com/api/webhooks/1/x", False),          # not https
    ("https://discord.com.evil.example/api/webhooks/1/x", False),
    ("https://example.com/api/webhooks/1/x", False),
    ("", False),
    (HOOK + "x" * 400, False),
])
def test_only_https_discord_webhooks_are_accepted(url, ok):
    assert dn.valid_webhook(url) is ok


def test_shiny_message_carries_the_hunt_details():
    msg = dn.shiny_message("Fennekin", "https://img/x.gif", 3108, 484,
                           "Timid", [31, 20, 15, 31, 5, 9], "Y (USA)")
    embed = msg["embeds"][0]
    assert embed["title"] == "Shiny Fennekin!"
    assert embed["thumbnail"]["url"] == "https://img/x.gif"
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields == {"Phase": "3,108 encounters", "Shinies so far": "484",
                      "Nature": "Timid", "IVs": "31/20/15/31/5/9"}
    assert "Y (USA)" in embed["footer"]["text"]


def test_send_posts_json_to_the_webhook(monkeypatch):
    sent = {}

    def fake_urlopen(req, timeout):
        sent.update(url=req.full_url, method=req.get_method(),
                    body=json.loads(req.data), ctype=req.get_header("Content-type"))
        return io.BytesIO(b"")
    monkeypatch.setattr(dn.urllib.request, "urlopen", fake_urlopen)
    dn.send(HOOK, {"content": "hi"})
    assert sent == {"url": HOOK, "method": "POST", "body": {"content": "hi"},
                    "ctype": "application/json"}


def test_send_refuses_a_bad_url_without_any_request(monkeypatch):
    monkeypatch.setattr(dn.urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("network used"))
    with pytest.raises(dn.DiscordError):
        dn.send("https://example.com/hook", {})


def test_a_refusal_is_reported_without_the_url(monkeypatch):
    def refuse(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
    monkeypatch.setattr(dn.urllib.request, "urlopen", refuse)
    with pytest.raises(dn.DiscordError) as err:
        dn.send(HOOK, {})
    assert "401" in str(err.value) and "token" not in str(err.value)


def test_webhook_round_trips_through_the_local_file(tmp_path):
    path = tmp_path / "discord.local.yaml"
    assert dn.load_webhook(path) == ""
    dn.save_webhook(path, HOOK)
    assert dn.load_webhook(path) == HOOK


def test_the_local_file_is_kept_out_of_git():
    ignore = (REPO / ".gitignore").read_text(encoding="utf-8")
    assert "*.local.yaml" in ignore
