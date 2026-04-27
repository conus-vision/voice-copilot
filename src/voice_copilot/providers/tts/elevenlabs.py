"""ElevenLabs TTS — premium cloud voices."""

from __future__ import annotations

from collections.abc import AsyncIterator

from voice_copilot.core.secrets import get_secret
from voice_copilot.providers.registry import register
from voice_copilot.providers.tts.base import NotInstalled, TTSChunk, TTSProvider

try:
    from elevenlabs.client import AsyncElevenLabs

    _HAVE_ELEVEN = True
except ImportError:
    _HAVE_ELEVEN = False


if _HAVE_ELEVEN:

    @register("tts", "elevenlabs")
    class ElevenLabsProvider(TTSProvider):
        name = "elevenlabs"
        output_format = "mp3"

        def __init__(
            self,
            voice_id: str = "EXAVITQu4vr4xnSDxMaL",
            model_id: str = "eleven_multilingual_v2",
            api_key: str | None = None,
        ) -> None:
            self._voice_id = voice_id
            self._model_id = model_id
            self._client = AsyncElevenLabs(api_key=api_key or get_secret("ELEVENLABS_API_KEY"))

        async def synthesize(
            self,
            text: str,
            *,
            language: str,
            voice: str | None = None,
        ) -> AsyncIterator[TTSChunk]:
            stream = self._client.text_to_speech.stream(
                voice_id=voice or self._voice_id,
                model_id=self._model_id,
                text=text,
                output_format="mp3_44100_128",
            )
            async for chunk in stream:
                if chunk:
                    yield TTSChunk(format="mp3", data=chunk)
            yield TTSChunk(format="mp3", data=b"", is_last=True)
else:

    @register("tts", "elevenlabs")
    class _ElevenLabsMissing(NotInstalled):
        def __init__(self, **_: object) -> None:
            super().__init__(name="elevenlabs", extra="elevenlabs")
