"""OpenAI TTS provider."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from voice_copilot.core.secrets import get_secret
from voice_copilot.providers.registry import register
from voice_copilot.providers.tts.base import TTSChunk, TTSProvider


@register("tts", "openai")
class OpenAITTSProvider(TTSProvider):
    name = "openai"
    output_format = "mp3"

    def __init__(
        self, model: str = "gpt-4o-mini-tts", voice: str = "alloy", api_key: str | None = None
    ) -> None:
        self._model = model
        self._voice = voice
        self._api_key = api_key or get_secret("OPENAI_API_KEY")
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=self._api_key)
        return self._client

    async def synthesize(
        self,
        text: str,
        *,
        language: str,
        voice: str | None = None,
    ) -> AsyncIterator[TTSChunk]:
        client = self._get_client()
        async with client.audio.speech.with_streaming_response.create(
            model=self._model,
            voice=voice or self._voice,
            input=text,
            response_format="mp3",
        ) as resp:
            async for chunk in resp.iter_bytes(chunk_size=4096):
                if chunk:
                    yield TTSChunk(format="mp3", data=chunk)
        yield TTSChunk(format="mp3", data=b"", is_last=True)
