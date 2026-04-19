"""TTS provider interface.

Synthesis is an async generator of `TTSChunk` (format header + opaque bytes).
Consumers pipe chunks over the WebSocket to the browser for playback.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal

AudioFormat = Literal["mp3", "wav", "ogg", "webm"]


@dataclass
class TTSChunk:
    format: AudioFormat
    data: bytes
    is_last: bool = False


class TTSProvider(ABC):
    name: str = "unknown"
    output_format: AudioFormat = "mp3"

    @abstractmethod
    async def synthesize(
        self,
        text: str,
        *,
        language: str,
        voice: str | None = None,
    ) -> AsyncIterator[TTSChunk]:
        raise NotImplementedError


class NotInstalled(TTSProvider):
    """Raises a friendly error when someone selects an un-installed backend."""

    def __init__(self, *, name: str, extra: str) -> None:
        self.name = name
        self._extra = extra

    async def synthesize(self, text: str, *, language: str, voice: str | None = None) -> AsyncIterator[TTSChunk]:
        raise RuntimeError(
            f"TTS provider {self.name!r} needs extra dependencies. "
            f"Install with: pipx install 'voice-copilot[{self._extra}]'"
        )
        yield  # pragma: no cover
