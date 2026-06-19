"""FastAPI app factory + uvicorn runner.

The web layer has three responsibilities:
  * serve the popup + settings SPA from static/,
  * expose a WebSocket for bidirectional events (bus → browser, browser → bus),
  * expose a minimal REST API for config read/write.

No audio heavy lifting here — that's wired in Э9.
"""

from __future__ import annotations

import asyncio
import base64
import webbrowser
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, cast

import uvicorn
from fastapi import FastAPI, HTTPException, Request
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
from voice_copilot.providers.stt.base import AudioContainer, STTProvider
from voice_copilot.providers.tts.base import TTSProvider
from voice_copilot.proxy.cli_shims import (
    choose_cli_working_directory,
    describe_cli_shims,
    install_cli_shim,
    launch_cli_profile,
    restore_cli_shim,
)
from voice_copilot.proxy.session import SessionRegistry
from voice_copilot.web.ws import register_ws

STATIC_DIR = Path(__file__).parent / "static"


class ManagedServer(uvicorn.Server):
    """A uvicorn server that leaves process signal handling to the caller.

    We run one or more of these as asyncio tasks under a single ``asyncio.run()``.
    uvicorn's default ``capture_signals()`` installs its own SIGINT/SIGTERM
    handlers; with several servers (web + proxy) plus ``asyncio.run()``'s own
    handler all fighting over the signal, Ctrl+C produces a noisy
    ``KeyboardInterrupt`` / ``CancelledError`` traceback. We let ``asyncio.run()``
    deliver the interrupt and shut these down cleanly via ``should_exit`` instead.
    """

    def install_signal_handlers(self) -> None:
        return None


_AUDIO_CONTAINERS = ("webm", "ogg", "wav", "mp3", "raw_pcm16")
_TTS_TEST_PHRASES = {
    "en": "This is a Voice Copilot speech test.",
    "es": "Esta es una prueba de voz de Voice Copilot.",
    "fr": "Ceci est un test vocal de Voice Copilot.",
    "uk": "Це голосовий тест Voice Copilot.",
    "ru": "Это голосовой тест Voice Copilot.",
}


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
    sessions: SessionRegistry | None = None,
    proxy_port: int | None = None,
) -> FastAPI:
    app = FastAPI(title="voice-copilot", version="0.0.2", lifespan=_lifespan)
    app.state.bus = bus
    app.state.config = config
    app.state.audio_hub = audio_hub or AudioHub()
    app.state.stt_provider = stt_provider
    app.state.human_language = config.human_language
    app.state.commentator_language = config.commentator_language
    app.state.sessions = sessions
    app.state.proxy_port = proxy_port
    app.state.commentator = None  # set by cli.py after Commentator is created

    def _proxy_port_or_none() -> int | None:
        return cast(int | None, app.state.proxy_port)

    def _require_proxy_port() -> int:
        proxy_port = _proxy_port_or_none()
        if proxy_port is None:
            raise HTTPException(
                400,
                "proxy is not running; restart with `voice-copilot serve --proxy` or `voice-copilot proxy`",
            )
        return proxy_port

    register_ws(app)

    @app.get("/api/info")
    async def get_info() -> dict[str, Any]:
        return {
            "proxy_port": app.state.proxy_port,
            "proxy_host": "127.0.0.1",
        }

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        cfg: Config = app.state.config
        return cfg.model_dump()

    @app.post("/api/config")
    async def post_config(payload: dict[str, Any]) -> dict[str, Any]:
        new_cfg = Config.model_validate(payload)
        save_config(new_cfg)
        app.state.config = new_cfg
        app.state.human_language = new_cfg.human_language
        app.state.commentator_language = new_cfg.commentator_language
        commentator = app.state.commentator
        if commentator is not None:
            commentator.update_config(new_cfg.commentator, new_cfg.commentator_language)
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
        except Exception as e:
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
        provider_kind = cast(Literal["llm", "tts", "stt"], kind)
        try:
            provider = provider_registry.build(provider_kind, name, dict(options))
        except Exception as e:
            return {"ok": False, "where": "build", "error": str(e)}

        try:
            if kind == "llm":
                stream = cast(
                    AsyncIterator[str],
                    provider.stream_chat(
                        [
                            LLMMessage(
                                role="user",
                                content="Say one short sentence about what you can narrate.",
                            )
                        ],
                        system="Reply in one short sentence. No bullets.",
                        max_tokens=48,
                        temperature=0.0,
                    ),
                )
                chunks: list[str] = []
                async for delta in stream:
                    if delta:
                        chunks.append(delta)
                response = "".join(chunks).strip()
                return {
                    "ok": True,
                    "preview": response[:64],
                    "response": response,
                }
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
        except Exception as e:
            return {"ok": False, "where": "probe", "error": str(e)}

    @app.post("/api/providers/test-tts")
    async def test_tts_provider() -> dict[str, Any]:
        cfg: Config = app.state.config
        try:
            provider = cast(
                TTSProvider, provider_registry.build("tts", cfg.tts.name, dict(cfg.tts.options))
            )
        except Exception as e:
            return {"ok": False, "where": "build", "error": str(e)}

        text = _TTS_TEST_PHRASES.get(cfg.commentator_language, _TTS_TEST_PHRASES["en"])
        audio = bytearray()
        audio_format = provider.output_format
        try:
            stream = provider.synthesize(text, language=cfg.commentator_language)
            async for chunk in stream:
                audio_format = chunk.format
                if chunk.data:
                    audio.extend(chunk.data)
            if not audio:
                return {"ok": False, "where": "probe", "error": "provider returned no audio"}
            return {
                "ok": True,
                "text": text,
                "format": audio_format,
                "audio_base64": base64.b64encode(bytes(audio)).decode("ascii"),
            }
        except Exception as e:
            return {"ok": False, "where": "probe", "error": str(e)}

    @app.post("/api/providers/test-stt")
    async def test_stt_provider(request: Request, container: str) -> dict[str, Any]:
        cfg: Config = app.state.config
        if container not in _AUDIO_CONTAINERS:
            raise HTTPException(400, f"unsupported audio container {container!r}")
        audio = await request.body()
        if not audio:
            raise HTTPException(400, "missing audio payload")
        try:
            provider = cast(
                STTProvider, provider_registry.build("stt", cfg.stt.name, dict(cfg.stt.options))
            )
        except Exception as e:
            return {"ok": False, "where": "build", "error": str(e)}

        try:
            result = await provider.transcribe(
                audio,
                container=cast(AudioContainer, container),
                language=cfg.human_language,
            )
            return {
                "ok": True,
                "text": result.text,
                "language": result.language,
                "confidence": result.confidence,
            }
        except Exception as e:
            return {"ok": False, "where": "probe", "error": str(e)}

    @app.get("/api/sessions")
    async def get_sessions() -> dict[str, Any]:
        reg: SessionRegistry | None = app.state.sessions
        if reg is None:
            return {"sessions": [], "active": None}
        return {
            "sessions": [s.to_dict() for s in reg.all()],
            "active": reg.get_active_id(),
        }

    @app.post("/api/sessions/active")
    async def set_active_session(payload: dict[str, Any]) -> dict[str, Any]:
        reg: SessionRegistry | None = app.state.sessions
        if reg is None:
            raise HTTPException(400, "proxy not running — no sessions")
        sid = payload.get("id")
        if sid is not None and not isinstance(sid, str):
            raise HTTPException(400, "id must be string or null")
        ok = reg.set_active_id(sid)
        if not ok:
            raise HTTPException(404, f"unknown session id {sid!r}")
        return {"ok": True, "active": reg.get_active_id()}

    @app.get("/api/proxy/cli-shims")
    async def get_cli_shims() -> dict[str, Any]:
        proxy_port = _proxy_port_or_none()
        status = describe_cli_shims(
            app.state.config,
            host="127.0.0.1",
            port=proxy_port or 8766,
        )
        status["proxy_available"] = proxy_port is not None
        return status

    @app.post("/api/proxy/cli-shims/pick-directory")
    async def post_cli_pick_directory(payload: dict[str, Any]) -> dict[str, Any]:
        initial_dir = str(payload.get("initial_dir") or "").strip() or None
        try:
            path = await asyncio.to_thread(choose_cli_working_directory, initial_dir)
        except RuntimeError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True, "path": path}

    @app.post("/api/proxy/cli-shims/{profile_id}/install")
    async def post_cli_shim_install(profile_id: str) -> dict[str, Any]:
        try:
            proxy_port = _require_proxy_port()
            return install_cli_shim(
                profile_id,
                app.state.config,
                host="127.0.0.1",
                port=proxy_port,
            )
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except RuntimeError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/proxy/cli-shims/{profile_id}/restore")
    async def post_cli_shim_restore(profile_id: str) -> dict[str, Any]:
        try:
            return restore_cli_shim(
                profile_id,
                app.state.config,
                host="127.0.0.1",
                port=app.state.proxy_port or 8766,
            )
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except RuntimeError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/proxy/cli-shims/{profile_id}/launch")
    async def post_cli_launch(profile_id: str) -> dict[str, Any]:
        try:
            proxy_port = _require_proxy_port()
            return launch_cli_profile(
                profile_id,
                app.state.config,
                host="127.0.0.1",
                port=proxy_port,
            )
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except RuntimeError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/")
    async def root() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/settings")
    async def settings_page() -> FileResponse:
        # Settings now live as tabs in the main SPA — serve the same page.
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/mini")
    async def mini_page() -> FileResponse:
        # Compact popup window: same SPA, flipped into mini mode via query
        # string. Kept as a route so window.open() URLs stay stable.
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


async def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    """Entry used by `voice-copilot run` — creates a fresh bus + loads config."""
    bus = EventBus()
    config = load_config()
    app = create_app(bus, config)

    server_config = uvicorn.Config(app, host=host, port=port, log_level="info", access_log=False)
    server = ManagedServer(server_config)

    if open_browser:
        url = f"http://{host}:{port}/"
        asyncio.get_event_loop().call_later(0.5, lambda: webbrowser.open(url))

    await server.serve()
