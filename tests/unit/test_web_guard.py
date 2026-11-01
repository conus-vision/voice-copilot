"""The panel must refuse requests a web page could forge against loopback.

Any page open in the user's browser can reach 127.0.0.1: a cross-site form
POST needs no CORS preflight, WebSockets have no same-origin policy, and DNS
rebinding makes a hostile domain same-origin with itself. The panel's API
launches terminals, writes PATH shims and streams the agent's activity, so
all three have to be shut out while the panel's own requests keep working.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from voice_copilot.core.bus import EventBus
from voice_copilot.core.config import Config
from voice_copilot.web import server as web_server

PANEL = "http://127.0.0.1:8765"
#: TestClient opens WebSockets against ws://testserver unless given a full URL.
PANEL_WS = "ws://127.0.0.1:8765/ws"


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def fake_launch(profile_id: str, cfg: Config, **_: Any) -> dict[str, Any]:
        calls.append(profile_id)
        return {"ok": True}

    monkeypatch.setattr(web_server, "launch_cli_profile", fake_launch)
    return calls


def _client(bind_host: str | None = None) -> TestClient:
    app = web_server.create_app(EventBus(), Config(), proxy_port=8766, bind_host=bind_host)
    return TestClient(app, base_url=PANEL)


def test_cross_site_form_post_cannot_launch_a_cli(launched: list[str]) -> None:
    res = _client().post(
        "/api/proxy/cli-shims/terminal/launch",
        headers={
            "Origin": "https://evil.example",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    assert res.status_code == 403
    assert launched == []


def test_opaque_origin_is_refused(launched: list[str]) -> None:
    res = _client().post("/api/proxy/cli-shims/terminal/launch", headers={"Origin": "null"})
    assert res.status_code == 403
    assert launched == []


def test_same_origin_post_from_the_panel_goes_through(launched: list[str]) -> None:
    res = _client().post("/api/proxy/cli-shims/terminal/launch", headers={"Origin": PANEL})
    assert res.status_code == 200
    assert launched == ["terminal"]


def test_post_without_origin_goes_through(launched: list[str]) -> None:
    # curl and scripts send no Origin; browsers always do on cross-site POSTs.
    res = _client().post("/api/proxy/cli-shims/terminal/launch")
    assert res.status_code == 200
    assert launched == ["terminal"]


def test_other_local_port_is_a_different_origin(launched: list[str]) -> None:
    res = _client().post(
        "/api/proxy/cli-shims/terminal/launch", headers={"Origin": "http://127.0.0.1:3000"}
    )
    assert res.status_code == 403
    assert launched == []


def test_dns_rebinding_host_is_refused_even_for_reads() -> None:
    res = _client().get("/api/config", headers={"Host": "attacker.example:8765"})
    assert res.status_code == 403


@pytest.mark.parametrize(
    "host", ["127.0.0.1:8765", "localhost:8765", "[::1]:8765", "10.0.0.5:8765"]
)
def test_loopback_names_and_ip_literals_are_served(host: str) -> None:
    assert _client().get("/api/info", headers={"Host": host}).status_code == 200


def test_the_name_the_server_was_bound_to_is_served() -> None:
    client = _client(bind_host="devbox.local")
    assert client.get("/api/info", headers={"Host": "devbox.local:8765"}).status_code == 200
    assert client.get("/api/info", headers={"Host": "other.local:8765"}).status_code == 403


def test_cross_site_websocket_is_refused() -> None:
    with (
        pytest.raises(WebSocketDisconnect),
        _client().websocket_connect(PANEL_WS, headers={"Origin": "https://evil.example"}),
    ):
        pass


def test_panel_websocket_still_connects() -> None:
    with _client().websocket_connect(PANEL_WS, headers={"Origin": PANEL}) as ws:
        ws.send_text(json.dumps({"type": "ping"}))
        assert json.loads(ws.receive_text()) == {"type": "pong"}
