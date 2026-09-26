"""STT provider interface.

Audio arrives from the browser as webm/opus or wav PCM, already containing its
own container header. Providers accept a `bytes` blob + a hint about the
container type + an optional language hint.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

AudioContainer = Literal["webm", "ogg", "wav", "mp3", "raw_pcm16"]


@dataclass
class STTResult:
    text: str
    language: str | None = None
    confidence: float | None = None


class STTProvider(ABC):
    name: str = "unknown"

    @abstractmethod
    async def transcribe(
        self,
        audio: bytes,
        *,
        container: AudioContainer,
        language: str | None = None,
        sample_rate: int = 48_000,
    ) -> STTResult:
        raise NotImplementedError


class NotInstalled(STTProvider):
    """`planned=True`: registered, but no engine yet (see the TTS twin)."""

    def __init__(self, *, name: str, extra: str, planned: bool = False) -> None:
        self.name = name
        self._extra = extra
        self._planned = planned

    async def transcribe(
        self,
        audio: bytes,
        *,
        container: AudioContainer,
        language: str | None = None,
        sample_rate: int = 48_000,
    ) -> STTResult:
        if self._planned:
            raise RuntimeError(
                f"STT provider {self.name!r} is not implemented yet. "
                "Use openai-whisper-api or faster-whisper."
            )
        raise RuntimeError(
            f"STT provider {self.name!r} needs extra dependencies. "
            f"Install with: pipx install 'voice-copilot[{self._extra}]'"
        )
