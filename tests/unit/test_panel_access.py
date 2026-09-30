"""Other devices need the panel's token; this computer does not.

With the panel on a network address (to open it from a phone), anyone on
that network reached an API that launches terminals and stores API keys.
Requests from this computer keep working without a token, so the CLI
plugins and hooks need no change; VOICE_COPILOT_TOKEN extends the check to
this computer too, for machines shared with other people.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from voice_copilot.core.bus import EventBus
from voice_copilot.core.config import Config
from voice_copilot.web import server as web_server
from voice_copilot.web.access import COOKIE, PanelAccess, is_local_client

LAN_PANEL = "http://192.168.1.10:8765"
PHONE = ("192.168.1.23", 51000)
THIS_COMPUTER = ("127.0.0.1", 51000)


def _client(
    access: PanelAccess, *, client: tuple[str, int] = PHONE, base_url: str = LAN_PANEL
) -> TestClient:
    app = web_server.create_app(
        EventBus(), Config(), proxy_port=8766, bind_host="0.0.0.0", access=access
    )
    return TestClient(app, base_url=base_url, client=client, follow_redirects=False)


def test_another_device_without_the_token_is_refused() -> None:
    client = _client(PanelAccess("s3cret"))
    assert client.get("/api/sessions").status_code == 401
    page = client.get("/", headers={"Accept": "text/html"})
    assert page.status_code == 401
    assert "access link" in page.text
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("ws://192.168.1.10:8765/ws"):
        pass


def test_the_link_is_traded_for_a_cookie_and_leaves_the_address_bar() -> None:
    client = _client(PanelAccess("s3cret", everywhere=False))
    res = client.get("/?token=s3cret&mini=1", headers={"Accept": "text/html"})
    assert res.status_code == 303
    assert res.headers["location"] == "/?mini=1"
    assert f"{COOKIE}=s3cret" in res.headers["set-cookie"]
    assert "HttpOnly" in res.headers["set-cookie"]
    # The browser now sends the cookie with every request, the socket included.
    assert client.get("/api/sessions").status_code == 200
    with client.websocket_connect("ws://192.168.1.10:8765/ws") as ws:
        ws.send_text("ping")


def test_a_wrong_token_is_refused() -> None:
    client = _client(PanelAccess("s3cret"))
    assert client.get("/?token=guess", headers={"Accept": "text/html"}).status_code == 401
    assert (
        client.get("/api/sessions", headers={"X-Voice-Copilot-Token": "guess"}).status_code == 401
    )


def test_scripts_can_send_the_token_as_a_header() -> None:
    client = _client(PanelAccess("s3cret"))
    for headers in ({"Authorization": "Bearer s3cret"}, {"X-Voice-Copilot-Token": "s3cret"}):
        assert client.get("/api/sessions", headers=headers).status_code == 200


def test_this_computer_needs_no_token_unless_one_is_set() -> None:
    local = _client(PanelAccess(None, everywhere=False), client=THIS_COMPUTER)
    assert local.get("/api/sessions").status_code == 200
    # A companion hook from a CLI on this computer.
    hook = local.post(
        "/api/companion/v1/hooks/claude?cli=claude",
        json={"hook_event_name": "Stop", "session_id": "s1"},
    )
    assert hook.status_code == 200

    shared_machine = _client(PanelAccess("s3cret"), client=THIS_COMPUTER)
    assert shared_machine.get("/api/sessions").status_code == 401
    assert (
        shared_machine.get("/api/sessions", headers={"X-Voice-Copilot-Token": "s3cret"}).status_code
        == 200
    )


def test_the_token_setting_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICE_COPILOT_TOKEN", "from-env")
    access = PanelAccess()
    assert access.everywhere and access.token == "from-env"
    assert access.browser_url("127.0.0.1", 8765) == "http://127.0.0.1:8765/?token=from-env"
    monkeypatch.delenv("VOICE_COPILOT_TOKEN")
    assert not PanelAccess().everywhere


def test_the_generated_token_is_kept_private_and_reused(tmp_path: Path) -> None:
    path = tmp_path / "panel-token"
    first = PanelAccess(None, token_file=path).token
    assert len(first) >= 24
    assert PanelAccess(None, token_file=path).token == first
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_links() -> None:
    access = PanelAccess("t", everywhere=False)
    assert access.browser_url("0.0.0.0", 8765) == "http://127.0.0.1:8765/"
    assert access.network_url("127.0.0.1", 8765) is None
    assert access.network_url("localhost", 8765) is None
    assert access.network_url("192.168.1.10", 8765) == "http://192.168.1.10:8765/?token=t"


@pytest.mark.parametrize(
    ("host", "local"),
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("::ffff:127.0.0.1", True),
        (None, True),  # a Unix socket
        ("192.168.1.23", False),
        ("10.0.0.5", False),
        ("::ffff:192.168.1.23", False),
    ],
)
def test_local_clients(host: str | None, local: bool) -> None:
    assert is_local_client(host) is local
