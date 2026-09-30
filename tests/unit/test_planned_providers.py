"""Registered names without an engine must not send users to install extras.

Silero and Deepgram are placeholders: their error used to say "install
voice-copilot[local-tts]", which changed nothing for them. The local voice
that does exist is Piper.
"""

from __future__ import annotations

import pytest

from voice_copilot.providers import registry
from voice_copilot.providers import stt as _stt  # noqa: F401
from voice_copilot.providers import tts as _tts  # noqa: F401


async def test_planned_tts_says_not_implemented_and_names_the_local_voice() -> None:
    provider = registry.build("tts", "silero", {})
    with pytest.raises(RuntimeError, match=r"not implemented yet.*piper"):
        async for _ in provider.synthesize("hi", language="en"):
            pass


async def test_planned_stt_says_not_implemented() -> None:
    provider = registry.build("stt", "deepgram", {})
    with pytest.raises(RuntimeError, match="not implemented yet"):
        await provider.transcribe(b"x", container="wav")
