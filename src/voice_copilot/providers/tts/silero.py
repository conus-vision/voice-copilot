"""Silero TTS: a reserved name, not implemented.

Silero needs PyTorch (about 2 GB). The local voice Voice Copilot ships is
Piper (`piper.py`); this stub keeps configs that name Silero readable and
says so when it is picked.
"""

from __future__ import annotations

from voice_copilot.providers.registry import register
from voice_copilot.providers.tts.base import NotInstalled


@register("tts", "silero")
class SileroStub(NotInstalled):
    def __init__(self, **_: object) -> None:
        super().__init__(name="silero", extra="local-tts", planned=True)
