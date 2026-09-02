"""Reverse-proxy server that tees API traffic into the event bus.

Client CLIs point base-URL env vars or runtime config overrides here; we
forward requests to the real upstream and, for SSE responses, parse chunks
live and emit `AGENT_TEXT` / `AGENT_THINKING` / `TOOL_CALL_STARTED` events.

No TLS interception, no CA cert plumbing — the client just talks plaintext
HTTP to us and we talk HTTPS to the upstream.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol

import httpx
import uvicorn
import zstandard
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import Event, EventKind
from voice_copilot.core.user_query import clean_user_query
from voice_copilot.proxy.anthropic import AnthropicSSEParser
from voice_copilot.proxy.body_sniffer import extract_user_query
from voice_copilot.proxy.ollama_native import OllamaNativeParser
from voice_copilot.proxy.openai import OpenAISSEParser
from voice_copilot.proxy.session import SessionRegistry

log = logging.getLogger(__name__)

#: How long a server waits for open connections on shutdown before closing them.
_GRACEFUL_SHUTDOWN_S = 3

#: Ceiling on a decompressed request body we only ever read to find the user's
#: question — a zstd bomb must not be able to balloon the proxy's memory.
_MAX_SNIFF_BYTES = 32 * 1024 * 1024

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
#:
#: Limitation: the anthropic route targets api.anthropic.com only. Claude Code
#: run through Bedrock (`CLAUDE_CODE_USE_BEDROCK`), Vertex
#: (`ANTHROPIC_VERTEX_BASE_URL`), or Azure Foundry uses different base-URL env
#: vars and hosts we don't intercept, so those sessions aren't narrated. Adding
#: them means new routes + upstreams keyed off those env vars.
_PROVIDERS: dict[str, tuple[str, Any]] = {
    "anthropic": ("https://api.anthropic.com", "anthropic"),
    "openai": ("https://api.openai.com", "openai"),
    # Codex signed in with a ChatGPT plan (`auth_mode: chatgpt` in
    # ~/.codex/auth.json) does not talk to api.openai.com at all — its
    # Responses traffic goes to the ChatGPT backend, carrying an OAuth bearer
    # and a `chatgpt-account-id` header the public API would reject. Same wire
    # format, different host, so it gets its own route.
    "openai-chatgpt": ("https://chatgpt.com/backend-api/codex", "openai"),
    "openrouter": ("https://openrouter.ai/api", "openai"),
    "groq": ("https://api.groq.com/openai", "openai"),
    "mistral": ("https://api.mistral.ai", "openai"),
    "deepseek": ("https://api.deepseek.com", "openai"),
    "ollama": ("http://127.0.0.1:11434", "ollama"),
    "gemini": ("https://generativelanguage.googleapis.com", None),  # passthrough
    "opencode-zen": ("https://opencode.ai/zen/v1", "opencode_zen"),
}


def provider_has_narration(provider: str) -> bool:
    """Whether the proxy narrates this provider's stream, or just forwards it.

    Providers wired with a parser (anthropic, openai-shaped, ollama, …) get
    live `AGENT_TEXT`/`TOOL_CALL_STARTED` events. A `None` parser factory
    (currently gemini) is a blind passthrough: requests are proxied but nothing
    is narrated. The launch banner uses this to tell the user the truth.
    """
    entry = _PROVIDERS.get(provider)
    return entry is not None and entry[1] is not None


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


def _sniffable_body(headers: Any, body: bytes) -> bytes:
    """The request body as plaintext, for query sniffing only.

    Codex sends its Responses payload zstd-compressed, so the raw bytes are
    binary and `extract_user_query` finds nothing in them. We decode a copy
    here; the bytes forwarded upstream stay exactly as the client sent them.
    Anything we can't decode comes back unchanged — the sniffer is tolerant of
    garbage, and a failed sniff must never cost the user their request.
    """
    encoding = str(headers.get("content-encoding") or "").strip().lower()
    if encoding != "zstd" or not body:
        return body
    try:
        return zstandard.ZstdDecompressor().decompress(body, max_output_size=_MAX_SNIFF_BYTES)
    except Exception as e:
        log.debug("proxy: could not zstd-decode request body for sniffing: %s", e)
        return body


def _is_subagent_request(headers: Any) -> bool:
    """Whether this request comes from one of the CLI's forked sub-agents.

    Codex tags every request with `x-codex-turn-metadata` JSON: the main
    thread has `agent_name` "/root" and no `parent_thread_id`; a spawned
    sub-agent ("/root/architecture", ...) carries its parent. A sub-agent's
    final answer is not the end of the user's turn — treating it as one had
    the narrator announcing completion and the supervisor reviewing a job
    that was still running.
    """
    raw = headers.get("x-codex-turn-metadata")
    if not raw:
        return bool(headers.get("x-openai-subagent"))
    try:
        meta = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(meta, dict):
        return False
    if meta.get("parent_thread_id"):
        return True
    name = meta.get("agent_name")
    return isinstance(name, str) and name not in ("", "/root")


def _wants_websocket(req: Request) -> bool:
    """Whether the client is asking to upgrade this request to a WebSocket.

    Reached only because the proxy runs uvicorn with ``ws="none"``, which hands
    upgrade requests to the HTTP app instead of answering them itself.
    """
    return "websocket" in req.headers.get("upgrade", "").lower()


def _make_parser_factory(
    bus: EventBus,
    kind: str | None,
    session_id: str,
    *,
    internal: bool = False,
    subagent: bool = False,
) -> Callable[[], _SSEParser] | None:
    if kind == "anthropic":
        return lambda: AnthropicSSEParser(
            bus, session_id=session_id, internal=internal, subagent=subagent
        )
    if kind == "openai":
        return lambda: OpenAISSEParser(
            bus, session_id=session_id, internal=internal, subagent=subagent
        )
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
            if _wants_websocket(req):
                # Codex prefers a WebSocket for /responses
                # (`openai-beta: responses_websockets=...`). We are an HTTP
                # reverse proxy and cannot narrate a socket we don't speak, so
                # refuse the upgrade: the client then retries the same call as
                # a normal streaming POST, which we do parse.
                log.info("proxy: refusing websocket upgrade for /%s/%s", provider, path)
                return Response(status_code=426, content=b"voice-copilot proxy speaks HTTP only")
            body = await req.body()
            sess = app.state.registry.identify(req.headers, provider=provider)
            kind = _pick_parser_kind(parser_kind, path)
            # Sniff the user's latest message before forwarding. This anchors
            # every narration against "what the user actually asked" and is
            # the only way the commentator knows the question — the proxy
            # otherwise only sees the model's reply stream.
            query = None
            internal = False
            if req.method == "POST" and body:
                try:
                    query = extract_user_query(
                        _sniffable_body(req.headers, body), provider=provider, path=path
                    )
                except Exception:
                    query = None
                # A request whose "user" text is the CLI's own scaffolding is the
                # CLI talking to itself; everything it streams back is tagged so
                # the narrator and supervisor ignore it and the Trace folds it.
                internal = bool(query) and clean_user_query(query or "") is None
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
                parser_factory=_make_parser_factory(
                    bus,
                    kind,
                    sess.id,
                    internal=internal,
                    subagent=_is_subagent_request(req.headers),
                ),
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
    log.debug(
        "proxy %s %s -> %s | req content-encoding=%r | resp content-type=%r encoding=%r",
        req.method,
        upstream,
        upstream_resp.status_code,
        req.headers.get("content-encoding"),
        ctype,
        upstream_resp.headers.get("content-encoding"),
    )
    # Run the parser whenever one is registered and the body is a text-ish
    # stream. SSE is `text/event-stream`; Ollama native `/api/chat` is
    # `application/x-ndjson`. Parsers are format-tolerant — if nothing matches
    # their shape they just emit nothing.
    # An absent content-type counts: the ChatGPT backend Codex talks to streams
    # plain `event:`/`data:` SSE without declaring one, and gating on the header
    # alone silently dropped every codex turn on the floor.
    is_streamable = (
        not ctype
        or ctype.startswith("text/event-stream")
        or ctype.startswith("application/x-ndjson")
        or ctype.startswith("application/json")
    )
    parser: _SSEParser | None = (
        parser_factory() if (is_streamable and parser_factory is not None) else None
    )

    async def iter_chunks() -> AsyncIterator[bytes]:
        try:
            # aiter_bytes() yields httpx-decoded (decompressed) bytes, so the
            # parser sees plaintext SSE even when the upstream gzips the
            # response. We forward those same decoded bytes to the client and
            # drop content-encoding/length below, so the client reads plaintext
            # too. (aiter_raw would hand the parser compressed bytes it can't
            # parse — which silently produced no narration events.)
            async for chunk in upstream_resp.aiter_bytes():
                if parser is not None:
                    await parser.feed(chunk)
                yield chunk
        finally:
            if parser is not None:
                await parser.close()
            await upstream_resp.aclose()
            await client.aclose()

    # Body is now decoded, so the upstream's content-encoding/length no longer
    # describe it — dropping them lets the client read the plaintext stream.
    _resp_drop = _HOP_BY_HOP | {"content-encoding", "content-length"}
    resp_headers = {k: v for k, v in upstream_resp.headers.items() if k.lower() not in _resp_drop}
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
    quiet: bool = False,
) -> uvicorn.Server:
    """Build (but don't start) the proxy's uvicorn server.

    Returned as a :class:`ManagedServer` so the caller can run it as one of
    several tasks under a single ``asyncio.run()`` and drive a clean shutdown
    via ``should_exit`` on Ctrl+C.

    ``quiet`` (used by ``vc``) passes ``log_config=None`` so uvicorn does not
    install its own stderr handlers; its loggers then propagate to the root
    logger, which the caller has redirected to a file, keeping the child
    terminal's console clean.
    """
    from voice_copilot.web.server import ManagedServer

    app = create_proxy_app(bus, registry=registry)
    # ws="none" keeps uvicorn from answering upgrade requests itself, so a
    # client that asks for a WebSocket (Codex does, for /responses) lands in
    # the normal HTTP route and gets our 426 — which makes it retry over plain
    # streaming HTTP that we can actually narrate.
    # A bounded drain on shutdown: an orphaned sub-agent of the wrapped CLI
    # can still be mid-stream through us, and uvicorn would otherwise wait
    # for that connection forever — `vc` then never exited after codex did.
    if quiet:
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_config=None,
            access_log=False,
            ws="none",
            timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_S,
        )
    else:
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
            ws="none",
            timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_S,
        )
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
        "OPENAI_CHATGPT_BASE_URL": f"{root}/openai-chatgpt",
        "OPENROUTER_BASE_URL": f"{root}/openrouter/v1",
        "GROQ_BASE_URL": f"{root}/groq/v1",
        "MISTRAL_BASE_URL": f"{root}/mistral/v1",
        "DEEPSEEK_BASE_URL": f"{root}/deepseek/v1",
        "OLLAMA_BASE_URL": f"{root}/ollama",
        "GEMINI_BASE_URL": f"{root}/gemini",
        "OPENCODE_ZEN_BASE_URL": f"{root}/opencode-zen",
    }
