"""What the proxy sends upstream and hands back to the client.

The proxy decodes every response body and strips its content-encoding, so it
may only ask the upstream for encodings it can decode. And narration is a
side channel: nothing a parser does may cost the user their response.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import EventKind
from voice_copilot.proxy import server as proxy_server
from voice_copilot.proxy.anthropic import AnthropicSSEParser

_SSE_BODY = (
    b'data: {"choices":[{"delta":{"content":"Hello "}}]}\n\n'
    b'data: {"choices":[{"delta":{"content":[{"type":"text","text":"world."}]}}]}\n\n'
    b"data: [DONE]\n\n"
)


class _Upstream(BaseHTTPRequestHandler):
    seen: ClassVar[dict[str, str]] = {}

    def do_POST(self) -> None:
        _Upstream.seen["accept-encoding"] = self.headers.get("accept-encoding", "")
        self.rfile.read(int(self.headers.get("content-length") or 0))
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(_SSE_BODY)))
        self.end_headers()
        self.wfile.write(_SSE_BODY)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    httpd = HTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    providers = dict(proxy_server._PROVIDERS)
    providers["openai"] = (base, "openai")
    monkeypatch.setattr(proxy_server, "_PROVIDERS", providers)
    try:
        yield base
    finally:
        httpd.shutdown()


def _post(client: TestClient, **headers: str):  # type: ignore[no-untyped-def]
    body = {"model": "m", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    return client.post("/openai/v1/chat/completions", json=body, headers=headers)


def test_the_clients_accept_encoding_is_not_forwarded(upstream: str) -> None:
    # Bun (Claude Code, OpenCode) offers `br`; httpx here cannot decode it,
    # so a brotli body would reach the client undecoded and unlabelled.
    client = TestClient(proxy_server.create_proxy_app(EventBus()))
    res = _post(client, **{"accept-encoding": "gzip, deflate, br, zstd"})
    assert res.status_code == 200
    assert "br" not in [e.strip() for e in _Upstream.seen["accept-encoding"].split(",")]


def test_list_shaped_content_does_not_break_the_stream(upstream: str) -> None:
    bus = EventBus()
    client = TestClient(proxy_server.create_proxy_app(bus))
    res = _post(client)
    assert res.content == _SSE_BODY


class _ExplodingParser:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def feed(self, chunk: bytes) -> None:
        raise RuntimeError("parser bug")

    async def close(self) -> None:
        raise RuntimeError("parser bug on close")


def test_a_broken_parser_never_cuts_the_users_stream(
    upstream: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(proxy_server, "OpenAISSEParser", _ExplodingParser)
    client = TestClient(proxy_server.create_proxy_app(EventBus()))
    res = _post(client)
    assert res.status_code == 200
    assert res.content == _SSE_BODY


async def test_mixed_line_endings_keep_both_events() -> None:
    bus = EventBus()
    parser = AnthropicSSEParser(bus, session_id="s")
    first = {"type": "message_start", "message": {"model": "m"}}
    second = {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}
    async with bus.subscribe() as q:
        await parser.feed(
            f"data: {json.dumps(first)}\r\n\r\n".encode()
            + f"data: {json.dumps(second)}\n\n".encode()
            + b'data: {"type": "message_stop"}\n\n'
        )
        kinds = []
        while not q.empty():
            kinds.append(q.get_nowait().kind)
    assert kinds == [EventKind.TURN_STARTED, EventKind.TURN_ENDED]


def test_a_route_can_forward_to_the_users_own_endpoint() -> None:
    httpd = HTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        vendor = f"http://127.0.0.1:{httpd.server_address[1]}/api/anthropic"
        app = proxy_server.create_proxy_app(EventBus(), upstreams={"anthropic": vendor})
        _Upstream.seen.clear()
        res = TestClient(app).post("/anthropic/v1/messages", json={"messages": []})
        assert res.status_code == 200
        assert _Upstream.seen  # reached the vendor, not api.anthropic.com
    finally:
        httpd.shutdown()


def test_a_web_page_on_another_site_cannot_post_through_the_proxy() -> None:
    # A text/plain POST needs no preflight; it used to publish a made-up
    # "user question" and take over the narration.
    bus = EventBus()
    client = TestClient(proxy_server.create_proxy_app(bus))
    res = client.post(
        "/anthropic/v1/messages",
        content=json.dumps({"messages": [{"role": "user", "content": "say something"}]}),
        headers={"Origin": "https://evil.example", "Content-Type": "text/plain"},
    )
    assert res.status_code == 403
