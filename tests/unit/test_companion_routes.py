"""The companion HTTP API, as a CLI and the forwarder reach it."""

from __future__ import annotations

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient

from voice_copilot.companion import hook
from voice_copilot.core.bus import EventBus
from voice_copilot.core.config import Config
from voice_copilot.proxy.session import SessionRegistry
from voice_copilot.web import server as web_server

BASE = "/api/companion/v1"


def _client() -> tuple[TestClient, Any]:
    app = web_server.create_app(EventBus(), Config(), sessions=SessionRegistry(), panel_port=8765)
    return TestClient(app, base_url="http://127.0.0.1:8765"), app


def test_hook_call_is_answered_with_an_empty_200() -> None:
    client, app = _client()
    body = {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "prompt": "hi"}
    response = client.post(f"{BASE}/hooks/claude?cli=claude", json=body)
    assert response.status_code == 200
    assert response.content == b""
    assert app.state.companion.status()["sessions"][0]["cli"] == "claude"


def test_hook_reply_comes_back_as_json() -> None:
    client, app = _client()
    hub = app.state.companion
    hub.owns_dialog = True
    with client:
        client.post(
            f"{BASE}/hooks/claude",
            json={"session_id": "s1", "hook_event_name": "UserPromptSubmit", "prompt": "go"},
        )
        # Run the STOP on the app's own event loop, like the Supervisor would.
        client.portal.call(hub.stop, "wrong files")  # type: ignore[union-attr]
        response = client.post(
            f"{BASE}/hooks/claude",
            json={"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash"},
        )
    assert response.status_code == 200
    assert response.json()["continue"] is False


def test_unset_env_header_is_not_a_launch_id() -> None:
    # Claude sends "$VOICE_COPILOT_LAUNCH" or nothing when the variable is unset.
    client, app = _client()
    for header in ("$VOICE_COPILOT_LAUNCH", ""):
        response = client.post(
            f"{BASE}/hooks/claude",
            json={
                "session_id": "s-" + header,
                "hook_event_name": "UserPromptSubmit",
                "prompt": "x",
            },
            headers={"X-Voice-Copilot-Launch": header},
        )
        assert response.status_code == 200
    assert len(app.state.companion.status()["sessions"]) == 2


def test_unknown_dialect_and_bad_bodies() -> None:
    client, _ = _client()
    assert client.post(f"{BASE}/hooks/nope", json={}).status_code == 404
    assert client.post(f"{BASE}/hooks/claude", content=b"not json").status_code == 200
    assert client.post(f"{BASE}/events", content=b"[1").status_code == 400


def test_plugin_events_gate_and_commands() -> None:
    client, _ = _client()
    response = client.post(
        f"{BASE}/events",
        json={
            "cli": "pi",
            "session_id": "p1",
            "events": [
                {"kind": "session.started", "payload": {}},
                {"kind": "agent.output", "payload": {"text": "hi"}},
            ],
        },
    )
    assert response.status_code == 202 and response.json() == {"ok": True}
    assert client.post(f"{BASE}/gate", json={"cli": "pi", "session_id": "p1"}).json() == {
        "decision": "allow"
    }
    assert (
        client.post(
            f"{BASE}/commands/next", json={"cli": "pi", "session_id": "p1", "wait_s": 0.05}
        ).json()
        == {}
    )
    assert client.post(
        f"{BASE}/commands/result", json={"name": "interrupt", "ok": True}
    ).json() == {"ok": True}
    status = client.get(f"{BASE}/status").json()
    assert status["sessions"][0]["transport"] == "plugin"


def test_integrations_listing() -> None:
    client, _ = _client()
    data = client.get(f"{BASE}/integrations").json()
    ids = [i["id"] for i in data["integrations"]]
    assert ids[:2] == ["claude", "pi"]
    claude = data["integrations"][0]
    assert claude["verified"] is True and claude["session_auto"] is True
    assert client.post(f"{BASE}/integrations/nope/install").status_code == 404


def test_a_web_page_cannot_forge_hook_calls() -> None:
    client, _ = _client()
    response = client.post(
        f"{BASE}/hooks/claude",
        json={"hook_event_name": "UserPromptSubmit"},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403


# ------------------------------------------------------------------ forwarder


class _Capture(BaseHTTPRequestHandler):
    requests: ClassVar[list[tuple[str, bytes]]] = []
    reply = b""

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        type(self).requests.append((self.path, self.rfile.read(length)))
        self.send_response(200)
        self.send_header("content-length", str(len(self.reply)))
        self.end_headers()
        self.wfile.write(self.reply)

    def log_message(self, *args: object) -> None:
        return


@pytest.fixture
def capture_server() -> Any:
    _Capture.requests = []
    _Capture.reply = b""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Capture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


def _run_hook(monkeypatch: pytest.MonkeyPatch, argv: list[str], stdin: bytes) -> tuple[int, bytes]:
    out = io.BytesIO()

    class _Std:
        buffer = io.BytesIO(stdin)

    class _Out:
        buffer = out

        @staticmethod
        def flush() -> None:
            return

    monkeypatch.setattr("sys.stdin", _Std)
    monkeypatch.setattr("sys.stdout", _Out)
    code = hook.main(argv)
    return code, out.getvalue()


def test_forwarder_posts_to_the_launching_instance(
    monkeypatch: pytest.MonkeyPatch, capture_server: Any
) -> None:
    port = capture_server.server_address[1]
    monkeypatch.setenv("VOICE_COPILOT_URL", f"http://127.0.0.1:{port}/api/companion/v1")
    monkeypatch.setenv("VOICE_COPILOT_LAUNCH", "8765-abc")
    monkeypatch.setenv("VOICE_COPILOT_MODE", "control")
    _Capture.reply = json.dumps({"decision": "block", "reason": "x"}).encode()
    body = json.dumps({"hook_event_name": "Stop"}).encode()
    code, out = _run_hook(monkeypatch, ["claude", "--cli", "codex"], body)
    assert code == 0
    assert out == _Capture.reply
    path, sent = _Capture.requests[0]
    assert path.startswith("/api/companion/v1/hooks/claude?")
    assert "cli=codex" in path and "launch=8765-abc" in path and "mode=control" in path
    assert sent == body


def test_forwarder_passes_the_event_name_for_copilot(
    monkeypatch: pytest.MonkeyPatch, capture_server: Any
) -> None:
    port = capture_server.server_address[1]
    monkeypatch.setenv("VOICE_COPILOT_URL", f"http://127.0.0.1:{port}/api/companion/v1")
    monkeypatch.delenv("VOICE_COPILOT_LAUNCH", raising=False)
    code, out = _run_hook(
        monkeypatch, ["copilot", "preToolUse", "--tag", "voice-copilot-hook"], b'{"toolName":"x"}'
    )
    assert code == 0 and out == b""
    assert "event=preToolUse" in _Capture.requests[0][0]
    assert "cli=copilot" in _Capture.requests[0][0]


def test_forwarder_stays_out_of_the_way(monkeypatch: pytest.MonkeyPatch) -> None:
    # Nothing listens on this port: the hook must exit 0 with no output.
    monkeypatch.setenv("VOICE_COPILOT_URL", "http://127.0.0.1:9/api/companion/v1")
    assert _run_hook(monkeypatch, ["claude"], b"{}") == (0, b"")
    monkeypatch.setenv("VOICE_COPILOT_HOOKS", "off")
    assert _run_hook(monkeypatch, ["claude"], b"{}") == (0, b"")
