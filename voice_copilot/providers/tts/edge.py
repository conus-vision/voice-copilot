"""Microsoft Edge TTS — the light default.

Free, cloud, high quality, multilingual. No API key.
Outputs MP3 that `<audio>` in the browser plays natively.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from voice_copilot.providers.registry import register
from voice_copilot.providers.tts.base import TTSChunk, TTSProvider

_VOICES: dict[str, str] = {
    "en": "en-US-AriaNeural",
    "es": "es-ES-ElviraNeural",
    "fr": "fr-FR-DeniseNeural",
    "uk": "uk-UA-PolinaNeural",
    "ru": "ru-RU-SvetlanaNeural",
}


@register("tts", "edge-tts")
class EdgeTTSProvider(TTSProvider):
    name = "edge-tts"
    output_format = "mp3"

    def __init__(self, voice: str | None = None, rate: str = "+0%", pitch: str = "+0Hz") -> None:
        self._voice = voice
        self._rate = rate
        self._pitch = pitch

    async def synthesize(
        self,
        text: str,
        *,
        language: str,
        voice: str | None = None,
    ) -> AsyncIterator[TTSChunk]:
        import edge_tts

        picked = voice or self._voice or _VOICES.get(language, _VOICES["en"])
        communicate = edge_tts.Communicate(text, picked, rate=self._rate, pitch=self._pitch)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio" and chunk.get("data"):
                yield TTSChunk(format="mp3", data=chunk["data"])
        yield TTSChunk(format="mp3", data=b"", is_last=True)
