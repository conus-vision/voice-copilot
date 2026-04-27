"""Silero TTS — local, free, supports EN/ES/FR/UK/RU.

Lands fully in a follow-up (needs torch). Installed via [local-tts] extra.
For now we register a stub that explains what's missing.
"""

from __future__ import annotations

from voice_copilot.providers.registry import register
from voice_copilot.providers.tts.base import NotInstalled


@register("tts", "silero")
class SileroStub(NotInstalled):
    def __init__(self, **_: object) -> None:
        super().__init__(name="silero", extra="local-tts")
