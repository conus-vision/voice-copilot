"""Deepgram STT — cloud, fast, streaming-capable."""

from __future__ import annotations

from voice_copilot.providers.registry import register
from voice_copilot.providers.stt.base import NotInstalled


@register("stt", "deepgram")
class DeepgramStub(NotInstalled):
    def __init__(self, **_: object) -> None:
        super().__init__(name="deepgram", extra="deepgram")
