"""FastAPI app factory + uvicorn runner.

The web layer has three responsibilities:
  * serve the popup + settings SPA from static/,
  * expose a WebSocket for bidirectional events (bus → browser, browser → bus),
  * expose a minimal REST API for config read/write.

No audio heavy lifting here — that's wired in Э9.
"""

from __future__ import annotations

import asyncio
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from voice_copilot.audio.hub import AudioHub
from voice_copilot.core.bus import EventBus
from voice_copilot.core.config import Config, load_config, save_config
from voice_copilot.core.secrets import (
    KNOWN_SECRETS,
    delete_secret,
    list_known_present,
    set_secret,
)
from voice_copilot.providers import registry as provider_registry
from voice_copilot.providers.llm.base import LLMMessage
from voice_copilot.providers.stt.base import STTProvider
from voice_copilot.web.ws import register_ws

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def _lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    # Stash shared state on app.state so routes and WS handlers can reach it.
    bus: EventBus = app.state.bus
    yield
    # Nothing to tear down yet — bus has no background tasks.
    _ = bus


def create_app(
    bus: EventBus,
    config: Config,
    *,
    audio_hub: AudioHub | None = None,
    stt_provider: STTProvider | None = None,
) -> FastAPI:
    app = FastAPI(title="voice-copilot", version="0.0.1", lifespan=_lifespan)
    app.state.bus = bus
    app.state.config = config
    app.state.audio_hub = audio_hub or AudioHub()
    app.state.stt_provider = stt_provider
    app.state.language = config.language

    register_ws(app)

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        return app.state.config.model_dump()

    @app.post("/api/config")
    async def post_config(payload: dict[str, Any]) -> dict[str, Any]:
        new_cfg = Config.model_validate(payload)
        save_config(new_cfg)
        app.state.config = new_cfg
        return new_cfg.model_dump()

    @app.get("/api/secrets")
    async def get_secrets() -> dict[str, Any]:
        """Return which known secrets are set. Values never leave the server."""
        return {"known": list(KNOWN_SECRETS), "present": list_known_present()}

    @app.post("/api/secrets")
    async def post_secret(payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        value = payload.get("value")
        if not name:
            raise HTTPException(400, "missing secret name")
        if not isinstance(value, str) or not value:
            raise HTTPException(400, "missing secret value")
        try:
            set_secret(name, value)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"keyring write failed: {e}") from e
        return {"ok": True, "name": name}

    @app.delete("/api/secrets/{name}")
    async def delete_secret_ep(name: str) -> dict[str, Any]:
        delete_secret(name)
        return {"ok": True, "name": name}

    @app.post("/api/providers/test")
    async def test_provider(payload: dict[str, Any]) -> dict[str, Any]:
        kind = str(payload.get("kind") or "").strip()
        name = str(payload.get("name") or "").strip()
        options = payload.get("options") or {}
        if kind not in ("llm", "tts", "stt"):
            raise HTTPException(400, f"unknown kind {kind!r}")
        if not name:
            raise HTTPException(400, "missing provider name")
        try:
            provider = provider_registry.build(kind, name, dict(options))
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "where": "build", "error": str(e)}

        try:
            if kind == "llm":
                stream = provider.stream_chat(
                    [LLMMessage(role="user", content="ping")],
                    system="Reply with one short word.",
                    max_tokens=8,
                    temperature=0.0,
                )
                first: str | None = None
                async for delta in stream:
                    first = delta
                    break
                return {"ok": True, "preview": (first or "").strip()[:64]}
            if kind == "tts":
                got = 0
                async for chunk in provider.synthesize("ok", language="en"):
                    if chunk.data:
                        got += len(chunk.data)
                    if got >= 64:
                        break
                return {"ok": True, "bytes": got}
            # stt — we can't probe without audio, confirm instantiation.
            return {"ok": True, "note": "provider constructed; no probe audio sent"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "where": "probe", "error": str(e)}

    @app.get("/")
    async def root() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/settings")
    async def settings_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "settings.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


async def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    """Entry used by `voice-copilot run` — creates a fresh bus + loads config."""
    bus = EventBus()
    config = load_config()
    app = create_app(bus, config)

    server_config = uvicorn.Config(app, host=host, port=port, log_level="info", access_log=False)
    server = uvicorn.Server(server_config)

    if open_browser:
        url = f"http://{host}:{port}/"
        asyncio.get_event_loop().call_later(0.5, lambda: webbrowser.open(url))

    await server.serve()
