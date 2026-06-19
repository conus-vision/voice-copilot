"""Reverse-proxy server that tees API traffic into the event bus.

Client CLIs point base-URL env vars or runtime config overrides here; we
forward requests to the real upstream and, for SSE responses, parse chunks
live and emit `AGENT_TEXT` / `AGENT_THINKING` / `TOOL_CALL_STARTED` events.

No TLS interception, no CA cert plumbing — the client just talks plaintext
HTTP to us and we talk HTTPS to the upstream.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import Event, EventKind
from voice_copilot.proxy.anthropic import AnthropicSSEParser
from voice_copilot.proxy.body_sniffer import extract_user_query
from voice_copilot.proxy.ollama_native import OllamaNativeParser
from voice_copilot.proxy.openai import OpenAISSEParser
from voice_copilot.proxy.session import SessionRegistry

log = logging.getLogger(__name__)

_HOP_BY_HOP = {
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    "proxy-authorization",
    "proxy-authenticate",
    "te",
    "trailer",
}


class _SSEParser(Protocol):
    async def feed(self, chunk: bytes) -> None: ...
    async def close(self) -> None: ...


#: provider-slug → (upstream base, parser-factory builder)
#:   parser builder takes a session_id and returns an _SSEParser, or None to
#:   forward without parsing (pass-through for providers we haven't wired yet).
_PROVIDERS: dict[str, tuple[str, Any]] = {
    "anthropic": ("https://api.anthropic.com", "anthropic"),
    "openai": ("https://api.openai.com", "openai"),
    "openrouter": ("https://openrouter.ai/api", "openai"),
    "groq": ("https://api.groq.com/openai", "openai"),
    "mistral": ("https://api.mistral.ai", "openai"),
    "ollama": ("http://127.0.0.1:11434", "ollama"),
    "gemini": ("https://generativelanguage.googleapis.com", None),  # passthrough
    "opencode-zen": ("https://opencode.ai/zen/v1", "opencode_zen"),
}


def _pick_parser_kind(provider_kind: str | None, path: str) -> str | None:
    """Ollama exposes two shapes: /v1/* (OpenAI SSE) and /api/* (native NDJSON)."""
    if provider_kind == "ollama":
        if path.startswith("v1/") or path == "v1":
            return "openai"
        return "ollama_native"
    if provider_kind == "opencode_zen":
        normalized = path.lstrip("/")
        if normalized.startswith("v1/"):
            normalized = normalized[3:]
        if normalized.startswith("messages"):
            return "anthropic"
        if normalized.startswith("chat/completions") or normalized.startswith("responses"):
            return "openai"
        return None
    return provider_kind


def _make_parser_factory(
    bus: EventBus,
    kind: str | None,
    session_id: str,
) -> Callable[[], _SSEParser] | None:
    if kind == "anthropic":
        return lambda: AnthropicSSEParser(bus, session_id=session_id)
    if kind == "openai":
        return lambda: OpenAISSEParser(bus, session_id=session_id)
    if kind == "ollama_native":
        return lambda: OllamaNativeParser(bus, session_id=session_id)
    return None


def create_proxy_app(bus: EventBus, registry: SessionRegistry | None = None) -> FastAPI:
    app = FastAPI(title="voice-copilot proxy")
    app.state.bus = bus
    app.state.registry = registry or SessionRegistry()

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    def _make_route(
        provider: str,
        upstream_base: str,
        parser_kind: str | None,
    ) -> Callable[[str, Request], Awaitable[Response]]:
        async def route(path: str, req: Request) -> Response:
            body = await req.body()
            sess = app.state.registry.identify(req.headers, provider=provider)
            kind = _pick_parser_kind(parser_kind, path)
            # Sniff the user's latest message before forwarding. This anchors
            # every narration against "what the user actually asked" and is
            # the only way the commentator knows the question — the proxy
            # otherwise only sees the model's reply stream.
            query = None
            if req.method == "POST" and body:
                try:
                    query = extract_user_query(body, provider=provider, path=path)
                except Exception:
                    query = None
                if query:
                    sess.last_query = query
                    await bus.publish(
                        Event(
                            kind=EventKind.USER_MESSAGE,
                            source=f"{provider}.proxy",
                            payload={
                                "text": query,
                                "session_id": sess.id,
                                "delivery": "observed",
                            },
                        )
                    )
            app.state.registry.observe_request(
                sess.id,
                method=req.method,
                path=f"/{provider}/{path}" if path else f"/{provider}",
                request_bytes=len(body),
                query=query,
            )
            return await _forward(
                req,
                upstream=f"{upstream_base}/{path}",
                parser_factory=_make_parser_factory(bus, kind, sess.id),
                prefetched_body=body,
            )

        return route

    for provider, (upstream_base, parser_kind) in _PROVIDERS.items():
        app.add_api_route(
            f"/{provider}/{{path:path}}",
            _make_route(provider, upstream_base, parser_kind),
            methods=["GET", "POST", "PUT", "DELETE"],
            name=f"{provider}_proxy",
        )

    return app


async def _forward(
    req: Request,
    *,
    upstream: str,
    parser_factory: Any,
    prefetched_body: bytes | None = None,
) -> Response:
    body = prefetched_body if prefetched_body is not None else await req.body()
    headers = {k: v for k, v in req.headers.items() if k.lower() not in _HOP_BY_HOP}
    params = dict(req.query_params)

    client = httpx.AsyncClient(timeout=None)
    try:
        upstream_req = client.build_request(
            req.method, upstream, content=body, headers=headers, params=params
        )
        upstream_resp = await client.send(upstream_req, stream=True)
    except Exception as e:
        await client.aclose()
        log.warning("proxy upstream error: %s", e)
        return Response(status_code=502, content=f"upstream error: {e}".encode())

    ctype = upstream_resp.headers.get("content-type", "")
    # Run the parser whenever one is registered and the body is a text-ish
    # stream. SSE is `text/event-stream`; Ollama native `/api/chat` is
    # `application/x-ndjson`. Parsers are format-tolerant — if nothing matches
    # their shape they just emit nothing.
    is_streamable = (
        ctype.startswith("text/event-stream")
        or ctype.startswith("application/x-ndjson")
        or ctype.startswith("application/json")
    )
    parser: _SSEParser | None = (
        parser_factory() if (is_streamable and parser_factory is not None) else None
    )

    async def iter_chunks() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream_resp.aiter_raw():
                if parser is not None:
                    await parser.feed(chunk)
                yield chunk
        finally:
            if parser is not None:
                await parser.close()
            await upstream_resp.aclose()
            await client.aclose()

    resp_headers = {k: v for k, v in upstream_resp.headers.items() if k.lower() not in _HOP_BY_HOP}
    return StreamingResponse(
        iter_chunks(),
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        media_type=ctype or None,
    )


def build_proxy_server(
    bus: EventBus,
    *,
    host: str,
    port: int,
    registry: SessionRegistry | None = None,
) -> uvicorn.Server:
    """Build (but don't start) the proxy's uvicorn server.

    Returned as a :class:`ManagedServer` so the caller can run it as one of
    several tasks under a single ``asyncio.run()`` and drive a clean shutdown
    via ``should_exit`` on Ctrl+C.
    """
    from voice_copilot.web.server import ManagedServer

    app = create_proxy_app(bus, registry=registry)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    return ManagedServer(config)


async def serve_proxy(
    bus: EventBus,
    *,
    host: str,
    port: int,
    registry: SessionRegistry | None = None,
) -> None:
    await build_proxy_server(bus, host=host, port=port, registry=registry).serve()


def base_urls_for(host: str, port: int) -> dict[str, str]:
    """Env-var values that point a subprocess at this proxy."""
    root = f"http://{host}:{port}"
    return {
        "ANTHROPIC_BASE_URL": f"{root}/anthropic",
        "OPENAI_BASE_URL": f"{root}/openai/v1",
        "OPENROUTER_BASE_URL": f"{root}/openrouter/v1",
        "GROQ_BASE_URL": f"{root}/groq/v1",
        "MISTRAL_BASE_URL": f"{root}/mistral/v1",
        "OLLAMA_BASE_URL": f"{root}/ollama",
        "GEMINI_BASE_URL": f"{root}/gemini",
        "OPENCODE_ZEN_BASE_URL": f"{root}/opencode-zen",
    }
