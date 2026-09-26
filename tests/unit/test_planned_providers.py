"""Registered names without an engine must not send users to install extras.

Silero, Piper and Deepgram are placeholders: their error used to say
"install voice-copilot[local-tts]", which pulls in PyTorch and changes
nothing.
"""

from __future__ import annotations

import pytest

from voice_copilot.providers import registry
from voice_copilot.providers import stt as _stt  # noqa: F401
from voice_copilot.providers import tts as _tts  # noqa: F401


@pytest.mark.parametrize("name", ["silero", "piper"])
async def test_planned_tts_says_not_implemented(name: str) -> None:
    provider = registry.build("tts", name, {})
    with pytest.raises(RuntimeError, match="not implemented yet"):
        async for _ in provider.synthesize("hi", language="en"):
            pass


async def test_planned_stt_says_not_implemented() -> None:
    provider = registry.build("stt", "deepgram", {})
    with pytest.raises(RuntimeError, match="not implemented yet"):
        await provider.transcribe(b"x", container="wav")
