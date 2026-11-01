"""Two copies of the same CLI behind one proxy are two sessions.

They send the same user agent and the same credentials; before, the proxy
keyed on those alone and merged them, so one terminal's narration mixed in
the other's. Most CLIs name their session on every model request, and the
companion hub keys the same session the same way when a plugin reports it.
"""

from __future__ import annotations

import json
import time

import pytest

from voice_copilot.companion import dialects
from voice_copilot.companion.hub import CompanionHub
from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import EventKind
from voice_copilot.proxy import session as session_module
from voice_copilot.proxy.session import SessionRegistry, client_session_id, session_key

_CLAUDE_UA = "claude-cli/2.1.285 (external, cli)"
_KEY = "sk-ant-api03-same-key-for-both"


def _claude(session: str | None) -> dict[str, str]:
    headers = {"User-Agent": _CLAUDE_UA, "x-api-key": _KEY}
    if session:
        headers["X-Claude-Code-Session-Id"] = session
    return headers


def test_two_claude_sessions_with_one_key_stay_apart() -> None:
    reg = SessionRegistry()
    first = reg.identify(_claude("11111111-aaaa"), provider="anthropic")
    second = reg.identify(_claude("22222222-bbbb"), provider="anthropic")
    again = reg.identify(_claude("11111111-aaaa"), provider="anthropic")
    assert first.id != second.id
    assert again is first
    assert first.request_count == 2
    assert len(reg.all()) == 2


def test_without_a_session_id_the_old_key_still_applies() -> None:
    reg = SessionRegistry()
    a = reg.identify(_claude(None), provider="anthropic")
    b = reg.identify(_claude(None), provider="anthropic")
    other_key = reg.identify(
        {"User-Agent": _CLAUDE_UA, "x-api-key": "sk-ant-api03-another"}, provider="anthropic"
    )
    assert a is b
    assert other_key.id != a.id


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"x-claude-code-session-id": "c1"}, "c1"),
        ({"session-id": "codex-root"}, "codex-root"),
        ({"session_id": "old-codex"}, "old-codex"),
        ({"x-session-id": "ses_abc", "x-session-affinity": "ses_abc"}, "ses_abc"),
        ({"x-task-id": "cline-task"}, "cline-task"),
        ({"x-opencode-session": "zen"}, "zen"),
        ({"user-agent": "aider/0.80"}, None),
    ],
)
def test_session_id_headers(headers: dict[str, str], expected: str | None) -> None:
    assert client_session_id(headers) == expected


def test_a_codex_sub_agent_joins_its_parents_session() -> None:
    # A sub-agent (or an internal request) may send its own `session-id`;
    # the turn metadata always names the session it belongs to.
    reg = SessionRegistry()
    ua = "codex_cli_rs/0.200.0 (Linux; x86_64)"
    root = reg.identify(
        {
            "user-agent": ua,
            "session-id": "root-1",
            "x-codex-turn-metadata": json.dumps({"session_id": "root-1", "thread_id": "root-1"}),
        },
        provider="openai-chatgpt",
    )
    sub = reg.identify(
        {
            "user-agent": ua,
            "session-id": "review:root-1",
            "x-codex-turn-metadata": json.dumps(
                {"session_id": "root-1", "thread_id": "t-2", "parent_thread_id": "root-1"}
            ),
        },
        provider="openai-chatgpt",
    )
    assert sub is root
    assert root.id == session_key("codex", "root-1")


def test_proxy_and_plugin_name_one_session_alike() -> None:
    reg = SessionRegistry()
    sess = reg.identify(_claude("251de23f-353e"), provider="anthropic")
    assert sess.id == session_key("claude", "251de23f-353e")
    assert sess.cli_id == "claude"


def test_idle_sessions_are_forgotten_when_a_new_one_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reg = SessionRegistry()
    old = reg.identify(_claude("old"), provider="anthropic")  # also the active one
    stale = reg.identify(_claude("stale"), provider="anthropic")
    plugin = reg.register_external("claude-plugin1", label="claude (plugin)", cli_id="claude")
    for sess in (old, stale, plugin):
        sess.last_seen = time.time() - session_module.IDLE_FORGET_S - 60
    reg.identify(_claude("new"), provider="anthropic")
    ids = {s.id for s in reg.all()}
    assert stale.id not in ids
    # The session being narrated stays, and plugin sessions end through the hub.
    assert {old.id, plugin.id} <= ids


def test_the_narration_follows_the_terminal_the_user_typed_into() -> None:
    reg = SessionRegistry()
    a = reg.identify(_claude("a"), provider="anthropic")
    reg.observe_request(a.id, method="POST", path="/x", request_bytes=1, query="fix the parser")
    b = reg.identify(_claude("b"), provider="anthropic")
    reg.observe_request(b.id, method="POST", path="/x", request_bytes=1, query="write the docs")
    assert reg.get_active_id() == b.id
    # A keeps working: each of its tool rounds repeats its question.
    for _ in range(3):
        reg.observe_request(a.id, method="POST", path="/x", request_bytes=1, query="fix the parser")
    assert reg.get_active_id() == b.id
    reg.observe_request(a.id, method="POST", path="/x", request_bytes=1, query="now run tests")
    assert reg.get_active_id() == a.id


def test_a_forgotten_session_that_comes_back_keeps_its_id() -> None:
    reg = SessionRegistry()
    sess = reg.identify(_claude("returning"), provider="anthropic")
    reg.remove(sess.id)
    assert reg.identify(_claude("returning"), provider="anthropic").id == sess.id


# ---------------------------------------------------------------- with the hub


class _Recorder:
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self.events: list[object] = []

    async def __aenter__(self) -> _Recorder:
        self._cm = self._bus.subscribe()
        self._q = await self._cm.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._cm.__aexit__(None, None, None)

    def drain(self) -> list[object]:
        while not self._q.empty():
            self.events.append(self._q.get_nowait())
        return self.events


def _hook(event: str, session: str, **fields: object) -> dict[str, object]:
    return {"hook_event_name": event, "session_id": session, "cwd": "/w", **fields}


async def test_a_plugin_session_defers_to_the_proxy_once_it_sees_the_traffic() -> None:
    bus, reg = EventBus(), SessionRegistry()
    hub = CompanionHub(bus, reg, port=8765)
    async with _Recorder(bus) as rec:
        await hub.handle_hook("claude", "claude", _hook("UserPromptSubmit", "s-1", prompt="go"))
        narrated_before = [e.kind for e in rec.drain()]  # type: ignore[attr-defined]
        # The same Claude session's model request reaches the proxy.
        proxied = reg.identify(_claude("s-1"), provider="anthropic")
        rec.events.clear()
        await hub.handle_hook(
            "claude",
            "claude",
            _hook("PreToolUse", "s-1", tool_name="Read", tool_input={"file_path": "a.py"}),
        )
        await hub.handle_hook(
            "claude",
            "claude",
            _hook("PermissionRequest", "s-1", tool_name="Bash", tool_input={"command": "ls"}),
        )
        after = rec.drain()
    assert EventKind.USER_MESSAGE in narrated_before
    # The proxy narrates the tool calls now; the plugin adds only the wait.
    assert [e.kind for e in after] == [EventKind.AGENT_AWAITING_INPUT]  # type: ignore[attr-defined]
    assert after[0].payload["session_id"] == proxied.id  # type: ignore[attr-defined]
    assert hub.status()["sessions"][0]["mode"] == "control"
    assert len(reg.all()) == 1


async def test_control_reaches_the_terminal_the_user_listens_to() -> None:
    # Two proxied Claude sessions launched by this instance: a pause goes to
    # the one being narrated, not to whichever reported last.
    bus, reg = EventBus(), SessionRegistry()
    hub = CompanionHub(bus, reg, port=8765)
    first = hub.new_launch("claude", proxied=True)
    second = hub.new_launch("claude", proxied=True)
    await hub.handle_hook("claude", "claude", _hook("UserPromptSubmit", "one"), launch=first.id)
    await hub.handle_hook("claude", "claude", _hook("UserPromptSubmit", "two"), launch=second.id)
    reg.identify(_claude("one"), provider="anthropic")
    reg.identify(_claude("two"), provider="anthropic")
    reg.set_active_id(session_key("claude", "one"))

    await hub.pause("user")
    paused = {s["key"]: s["paused"] for s in hub.status()["sessions"]}
    assert paused == {session_key("claude", "one"): True, session_key("claude", "two"): False}


def test_hook_session_ids_match_what_the_dialect_reads() -> None:
    call = dialects.parse("claude", "claude", _hook("Stop", "abc"), None)
    assert call.session_id == "abc"


def test_through_the_proxy_two_terminals_are_two_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from fastapi.testclient import TestClient

    from voice_copilot.proxy import server as proxy_server

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("content-length") or 0))
            body = b'event: message_stop\ndata: {"type": "message_stop"}\n\n'
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    providers = dict(proxy_server._PROVIDERS)
    providers["anthropic"] = (f"http://127.0.0.1:{httpd.server_address[1]}", "anthropic")
    monkeypatch.setattr(proxy_server, "_PROVIDERS", providers)
    reg = SessionRegistry()
    try:
        client = TestClient(proxy_server.create_proxy_app(EventBus(), reg))
        for session in ("term-1", "term-2", "term-1"):
            # Claude Code puts its reminders in front of what the user typed.
            content = [
                {"type": "text", "text": "<system-reminder>Context.</system-reminder>"},
                {"type": "text", "text": f"question in {session}"},
            ]
            body = {
                "model": "m",
                "stream": True,
                "messages": [{"role": "user", "content": content}],
            }
            res = client.post("/anthropic/v1/messages", json=body, headers=_claude(session))
            assert res.status_code == 200
    finally:
        httpd.shutdown()
    queries = {s.id: s.last_query for s in reg.all()}
    assert queries == {
        session_key("claude", "term-1"): "question in term-1",
        session_key("claude", "term-2"): "question in term-2",
    }
