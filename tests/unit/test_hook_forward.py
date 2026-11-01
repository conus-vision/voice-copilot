"""The hook process: fast, and unable to hurt the agent whatever goes wrong."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from voice_copilot.adapters.hooks import HookAdapter
from voice_copilot.companion import hook_forward
from voice_copilot.companion.runtime import (
    NO_HOOKS_ENV,
    RUNTIME_DIR_ENV,
    TOKEN_HEADER,
    RuntimeInfo,
    clear_runtime,
    is_switched_off,
    read_runtime,
    runtime_file,
    set_switched_off,
    write_runtime,
)
from voice_copilot.core.bus import EventBus
from voice_copilot.proxy.session import SessionRegistry
from voice_copilot.web.hooks_api import register_hook_routes


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(RUNTIME_DIR_ENV, str(tmp_path))
    monkeypatch.delenv(NO_HOOKS_ENV, raising=False)


class _Stdin:
    def __init__(self, data: bytes) -> None:
        self.buffer = io.BytesIO(data)


def _run(monkeypatch: pytest.MonkeyPatch, capsys: Any, payload: Any, cli: str = "claude") -> str:
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    monkeypatch.setattr(sys, "stdin", _Stdin(raw))
    assert hook_forward.main([cli]) == 0
    return str(capsys.readouterr().out)


@pytest.fixture
def companion() -> Iterator[dict[str, Any]]:
    """A stand-in server that records requests and answers from a script."""
    state: dict[str, Any] = {"requests": [], "reply": b"", "status": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            state["requests"].append((self.path, dict(self.headers), b""))
            body = state.get("get_reply", b'{"working": false}')
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            state["requests"].append((self.path, dict(self.headers), body))
            self.send_response(state["status"])
            self.send_header("Content-Length", str(len(state["reply"])))
            self.end_headers()
            self.wfile.write(state["reply"])

        def log_message(self, *args: Any) -> None:
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    write_runtime(RuntimeInfo("127.0.0.1", server.server_port, "secret", 1))
    yield state
    server.shutdown()
    server.server_close()


def test_forwards_the_payload_and_prints_the_reply(
    companion: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    companion["reply"] = b'{"decision": "block", "reason": "go on"}'
    out = _run(monkeypatch, capsys, {"hook_event_name": "Stop", "session_id": "s"}, cli="codex")
    assert json.loads(out)["decision"] == "block"
    path, headers, body = companion["requests"][0]
    assert path == "/api/hooks/codex"
    assert headers[TOKEN_HEADER] == "secret"
    assert json.loads(body)["hook_event_name"] == "Stop"


def test_empty_reply_prints_nothing(
    companion: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    assert _run(monkeypatch, capsys, {"hook_event_name": "PreToolUse"}) == ""


def test_vc_own_children_do_not_report(
    companion: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setenv(NO_HOOKS_ENV, "1")
    assert _run(monkeypatch, capsys, {"hook_event_name": "PreToolUse"}) == ""
    assert companion["requests"] == []


def test_server_error_is_silent_mid_session(
    companion: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    companion["status"] = 500
    assert _run(monkeypatch, capsys, {"hook_event_name": "PreToolUse"}) == ""


def test_garbage_stdin_does_not_break_the_agent(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    assert _run(monkeypatch, capsys, b"not json") == ""


def test_nothing_running_mid_session_stays_quit(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    def must_not_spawn() -> None:
        raise AssertionError("must not start the companion mid-session")

    monkeypatch.setattr(hook_forward, "_spawn_companion", must_not_spawn)
    assert _run(monkeypatch, capsys, {"hook_event_name": "PreToolUse"}) == ""


def test_stale_runtime_file_is_dropped(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    write_runtime(RuntimeInfo("127.0.0.1", 1, "old", 1))
    _run(monkeypatch, capsys, {"hook_event_name": "PreToolUse"})
    assert read_runtime() is None


def test_session_start_starts_the_companion(
    companion: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    info = read_runtime()
    assert info is not None
    runtime_file().unlink()
    spawned: list[bool] = []

    def spawn() -> None:
        spawned.append(True)
        write_runtime(info)

    monkeypatch.setattr(hook_forward, "_spawn_companion", spawn)
    _run(monkeypatch, capsys, {"hook_event_name": "SessionStart", "session_id": "s"})
    assert spawned == [True]
    assert companion["requests"]


def test_failed_start_tells_the_user_once(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    monkeypatch.setattr(hook_forward, "_spawn_companion", lambda: None)
    monkeypatch.setattr(hook_forward, "_START_TIMEOUT_S", 0.05)
    out = _run(monkeypatch, capsys, {"hook_event_name": "SessionStart", "session_id": "s"})
    assert "not narrating" in json.loads(out)["systemMessage"]


def test_second_starter_waits_instead_of_spawning(monkeypatch: pytest.MonkeyPatch) -> None:
    assert hook_forward._take_start_lock() is not None
    assert hook_forward._take_start_lock() is None


def test_clear_runtime_leaves_a_newer_run_alone() -> None:
    write_runtime(RuntimeInfo("127.0.0.1", 2, "newer", 2))
    clear_runtime(token="older")
    assert read_runtime() is not None
    clear_runtime(token="newer")
    assert read_runtime() is None


def test_hook_process_stays_light() -> None:
    """It runs on every tool call: importing the server here would cost the agent."""
    code = (
        "import sys; import voice_copilot.companion.hook_forward;"
        "print([m for m in ('pydantic','fastapi','httpx','anthropic','openai','http.client','dataclasses') if m in sys.modules])"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


# ---------------------------------------------------------------------- the switch


def _prompt(text: str) -> dict[str, Any]:
    return {"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt": text}


def _agent_runs(command: str) -> dict[str, Any]:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": "s",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


def test_vc_off_in_the_chat_never_reaches_the_model(
    companion: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    out = json.loads(_run(monkeypatch, capsys, _prompt("  VC off "), cli="codex"))
    assert out == {"decision": "block", "reason": out["reason"]}
    assert "off" in out["reason"]
    assert is_switched_off("s")
    # The companion is told, and the prompt itself isn't forwarded as a hook.
    assert [path for path, _, _ in companion["requests"]][-1] == "/api/switch"
    assert not any(path == "/api/hooks/codex" for path, _, _ in companion["requests"])


def test_claude_is_asked_not_to_repeat_the_prompt(
    companion: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    out = json.loads(_run(monkeypatch, capsys, _prompt("vc off"), cli="claude"))
    assert out["hookSpecificOutput"]["suppressOriginalPrompt"] is True


def test_talking_about_the_switch_is_not_the_switch(
    companion: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    for text in ("vc off please", "what does vc off do?", "the vc off command"):
        assert _run(monkeypatch, capsys, _prompt(text)) == ""
    assert not is_switched_off()
    assert all(path == "/api/hooks/claude" for path, _, _ in companion["requests"])


def test_off_silences_every_hook_and_starts_nothing(
    companion: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    def must_not_spawn() -> None:
        raise AssertionError("off means off")

    set_switched_off(True)
    runtime_file().unlink()
    monkeypatch.setattr(hook_forward, "_spawn_companion", must_not_spawn)
    for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "Stop"):
        assert _run(monkeypatch, capsys, {"hook_event_name": event, "session_id": "s"}) == ""
    assert companion["requests"] == []


def test_vc_on_starts_the_companion_and_says_where_the_panel_is(
    companion: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    info = read_runtime()
    assert info is not None
    set_switched_off(True)
    runtime_file().unlink()
    monkeypatch.setattr(hook_forward, "_spawn_companion", lambda: write_runtime(info))
    out = json.loads(_run(monkeypatch, capsys, _prompt("vc on")))
    assert not is_switched_off("s")
    assert out["decision"] == "block"
    assert f"http://127.0.0.1:{info.port}/" in out["reason"]


def test_vc_status_changes_nothing(
    companion: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    assert "is on" in json.loads(_run(monkeypatch, capsys, _prompt("vc status")))["reason"]
    set_switched_off(True)
    assert "is off" in json.loads(_run(monkeypatch, capsys, _prompt("vc status")))["reason"]
    assert is_switched_off()
    assert companion["requests"] == []


def test_the_agent_can_switch_it_and_the_user_is_told(
    companion: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    out = json.loads(_run(monkeypatch, capsys, _agent_runs("voice-copilot switch off")))
    output = out["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "is off" in output["permissionDecisionReason"]
    assert "agent" in out["systemMessage"]
    assert is_switched_off("s")
    assert json.loads(companion["requests"][-1][2]) == {"by_agent": True, "session_id": "s"}


def test_the_switch_works_for_the_agent_even_while_off(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    set_switched_off(True)
    out = json.loads(_run(monkeypatch, capsys, _agent_runs("voice-copilot switch status")))
    assert "is off" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "systemMessage" not in out


def test_only_the_exact_command_is_taken_for_the_switch(
    companion: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    for command in ("voice-copilot switch off && rm -rf x", "echo voice-copilot switch off"):
        assert _run(monkeypatch, capsys, _agent_runs(command)) == ""
    assert not is_switched_off()


def test_a_refusal_leaves_it_on(
    companion: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    companion["reply"] = b'{"refused": "only the user can"}'
    out = json.loads(_run(monkeypatch, capsys, _agent_runs("voice-copilot switch off")))
    assert "only the user can" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert not is_switched_off()


def test_off_with_nothing_running_still_sticks() -> None:
    assert "is off" in hook_forward.apply_switch("off")
    assert is_switched_off()


# ---------------------------------------------------------------------- endpoint


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    register_hook_routes(app, HookAdapter(EventBus(), SessionRegistry()), "secret")
    return TestClient(app)


def test_switch_endpoint_needs_the_token(client: TestClient) -> None:
    assert client.post("/api/switch", json={}).status_code == 401
    assert client.post("/api/switch", json={}, headers={TOKEN_HEADER: "secret"}).status_code == 204


@pytest.mark.parametrize(
    ("by_agent", "refused"),
    [(True, True), (False, False)],
)
def test_supervisor_plus_can_only_be_ended_by_the_user(by_agent: bool, refused: bool) -> None:
    app = FastAPI()
    adapter = HookAdapter(EventBus(), SessionRegistry())
    register_hook_routes(app, adapter, "secret", lambda: "guard")
    response = TestClient(app).post(
        "/api/switch", json={"by_agent": by_agent}, headers={TOKEN_HEADER: "secret"}
    )
    assert ("refused" in response.text) is refused


def test_endpoint_rejects_a_missing_or_wrong_token(client: TestClient) -> None:
    body = {"hook_event_name": "PreToolUse", "session_id": "s"}
    assert client.post("/api/hooks/claude", json=body).status_code == 401
    wrong = {TOKEN_HEADER: "nope"}
    assert client.post("/api/hooks/claude", json=body, headers=wrong).status_code == 401


def test_endpoint_rejects_a_non_object(client: TestClient) -> None:
    headers = {TOKEN_HEADER: "secret"}
    assert client.post("/api/hooks/claude", json=[1], headers=headers).status_code == 400
    assert client.post("/api/hooks/claude", content=b"x", headers=headers).status_code == 400


def test_endpoint_answers_204_when_there_is_nothing_to_say(client: TestClient) -> None:
    body = {"hook_event_name": "PreToolUse", "session_id": "s"}
    response = client.post("/api/hooks/claude", json=body, headers={TOKEN_HEADER: "secret"})
    assert response.status_code == 204
    assert response.content == b""


def test_vc_check_in_the_chat_never_reaches_the_model(
    companion: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    companion["reply"] = b'{"text": "Checking now."}'
    out = json.loads(_run(monkeypatch, capsys, _prompt("vc check"), cli="codex"))
    assert out == {"decision": "block", "reason": "Checking now."}
    assert [path for path, _, _ in companion["requests"]][-1] == "/api/command"


def test_vc_check_while_off_stays_off(
    companion: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    set_switched_off(True)
    out = json.loads(_run(monkeypatch, capsys, _prompt("vc check"), cli="codex"))
    assert "off" in out["reason"]
    assert not any(path == "/api/command" for path, _, _ in companion["requests"])


def test_mail_and_command_endpoints_need_the_token(client: TestClient) -> None:
    assert client.get("/api/hooks/claude/mail", params={"session_id": "s"}).status_code == 401
    assert client.post("/api/command", json={"verb": "check"}).status_code == 401
    good = {TOKEN_HEADER: "secret"}
    mail = client.get("/api/hooks/claude/mail", params={"session_id": "s"}, headers=good)
    assert mail.json() == {"messages": [], "notices": []}


def test_a_check_typed_during_a_codex_turn_doesnt_end_the_turn(
    companion: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    companion["get_reply"] = b'{"working": true}'
    companion["reply"] = b'{"text": "Checking now."}'
    out = json.loads(_run(monkeypatch, capsys, _prompt("vc check"), cli="codex"))
    assert "decision" not in out
    assert out["systemMessage"] == "Checking now."
    assert "Carry on" in out["hookSpecificOutput"]["additionalContext"]


def test_only_codex_is_asked_about_the_turn(
    companion: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    companion["get_reply"] = b'{"working": true}'
    companion["reply"] = b'{"text": "Checking now."}'
    out = json.loads(_run(monkeypatch, capsys, _prompt("vc check"), cli="claude"))
    assert out["decision"] == "block"
    assert not any("/working" in path for path, _, _ in companion["requests"])
