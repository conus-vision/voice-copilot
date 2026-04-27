"""OpenAI Whisper API — light default STT."""

from __future__ import annotations

import io
from typing import Any

from voice_copilot.core.secrets import get_secret
from voice_copilot.providers.registry import register
from voice_copilot.providers.stt.base import AudioContainer, STTProvider, STTResult

_EXT = {"webm": "webm", "ogg": "ogg", "wav": "wav", "mp3": "mp3", "raw_pcm16": "wav"}


@register("stt", "openai-whisper-api")
class WhisperAPIProvider(STTProvider):
    name = "openai-whisper-api"

    def __init__(self, model: str = "whisper-1", api_key: str | None = None) -> None:
        self._model = model
        self._api_key = api_key or get_secret("OPENAI_API_KEY")
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=self._api_key)
        return self._client

    async def transcribe(
        self,
        audio: bytes,
        *,
        container: AudioContainer,
        language: str | None = None,
        sample_rate: int = 48_000,
    ) -> STTResult:
        ext = _EXT.get(container, "webm")
        buf = io.BytesIO(audio)
        buf.name = f"audio.{ext}"

        client = self._get_client()
        kwargs: dict[str, object] = {"model": self._model, "file": buf}
        if language:
            kwargs["language"] = language
        resp = await client.audio.transcriptions.create(**kwargs)
        text = getattr(resp, "text", "") or ""
        return STTResult(text=text, language=language)
