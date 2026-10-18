"""
pokebot/stream_overlay.py: the local server OBS browser sources read.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot import stream_overlay  # noqa: E402


@pytest.fixture
def server():
    srv = stream_overlay.OverlayServer(port=0)      # any free port
    yield srv
    srv.close()


def get(srv, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}{path}",
                                timeout=5) as res:
        return res.status, res.headers.get("Content-Type"), res.read()


def test_serves_the_published_state(server):
    server.publish({"phase": 1746, "chance": 0.347})
    status, ctype, body = get(server, "/state.json")
    assert status == 200 and ctype == "application/json"
    assert json.loads(body) == {"phase": 1746, "chance": 0.347}


def test_state_is_empty_until_something_is_published(server):
    assert json.loads(get(server, "/state.json")[2]) == {}


@pytest.mark.parametrize("path", ["/bar", "/panel", "/bar?x=1"])
def test_both_displays_are_served(server, path):
    status, ctype, body = get(server, path)
    assert status == 200 and ctype.startswith("text/html")
    assert b"pokebot-3ds overlay" in body


def test_anything_else_is_404(server):
    with pytest.raises(urllib.error.HTTPError) as err:
        get(server, "/config.yaml")
    assert err.value.code == 404


def test_only_this_machine_can_connect(server):
    assert server._httpd.server_address[0] == "127.0.0.1"
    assert server.url("bar") == f"http://127.0.0.1:{server.port}/bar"


def test_a_busy_port_gives_none_not_a_crash(server):
    assert stream_overlay.start(server.port) is None
