"""Reverse-proxy server that tees API traffic into the event bus.

Client CLIs set `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` to point here; we
forward requests to the real upstream and, for SSE responses, parse chunks
live and emit `AGENT_TEXT` / `AGENT_THINKING` / `TOOL_CALL_STARTED` events.

No TLS interception, no CA cert plumbing — the client just talks plaintext
HTTP to us and we talk HTTPS to the upstream.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any, Protocol

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

from voice_copilot.core.bus import EventBus
from voice_copilot.proxy.anthropic import AnthropicSSEParser
from voice_copilot.proxy.openai import OpenAISSEParser

log = logging.getLogger(__name__)

_HOP_BY_HOP = {"host", "content-length", "connection", "keep-alive",
               "transfer-encoding", "upgrade", "proxy-authorization",
               "proxy-authenticate", "te", "trailer"}


class _SSEParser(Protocol):
    async def feed(self, chunk: bytes) -> None: ...
    async def close(self) -> None: ...


def create_proxy_app(bus: EventBus) -> FastAPI:
    app = FastAPI(title="voice-copilot proxy")
    app.state.bus = bus

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.api_route("/anthropic/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def anthropic_proxy(path: str, req: Request) -> Response:
        return await _forward(
            req,
            upstream=f"https://api.anthropic.com/{path}",
            parser_factory=lambda: AnthropicSSEParser(bus),
        )

    @app.api_route("/openai/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def openai_proxy(path: str, req: Request) -> Response:
        return await _forward(
            req,
            upstream=f"https://api.openai.com/{path}",
            parser_factory=lambda: OpenAISSEParser(bus),
        )

    return app


async def _forward(
    req: Request,
    *,
    upstream: str,
    parser_factory,  # type: ignore[no-untyped-def]
) -> Response:
    body = await req.body()
    headers = {k: v for k, v in req.headers.items() if k.lower() not in _HOP_BY_HOP}
    params = dict(req.query_params)

    client = httpx.AsyncClient(timeout=None)
    try:
        upstream_req = client.build_request(req.method, upstream, content=body, headers=headers, params=params)
        upstream_resp = await client.send(upstream_req, stream=True)
    except Exception as e:  # noqa: BLE001
        await client.aclose()
        log.warning("proxy upstream error: %s", e)
        return Response(status_code=502, content=f"upstream error: {e}".encode())

    ctype = upstream_resp.headers.get("content-type", "")
    is_sse = ctype.startswith("text/event-stream")
    parser: _SSEParser | None = parser_factory() if is_sse else None

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


async def serve_proxy(bus: EventBus, *, host: str, port: int) -> None:
    app = create_proxy_app(bus)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    await server.serve()


def base_urls_for(host: str, port: int) -> dict[str, str]:
    """Env-var values that point a subprocess at this proxy."""
    root = f"http://{host}:{port}"
    return {
        "ANTHROPIC_BASE_URL": f"{root}/anthropic",
        "OPENAI_BASE_URL": f"{root}/openai/v1",
    }
