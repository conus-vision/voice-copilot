"""Piper TTS — local, fast, ONNX-based."""

from __future__ import annotations

from voice_copilot.providers.registry import register
from voice_copilot.providers.tts.base import NotInstalled


@register("tts", "piper")
class PiperStub(NotInstalled):
    def __init__(self, **_: object) -> None:
        super().__init__(name="piper", extra="local-tts")
