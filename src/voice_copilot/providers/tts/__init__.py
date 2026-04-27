from voice_copilot.providers.tts import edge as _edge  # noqa: F401
from voice_copilot.providers.tts import elevenlabs as _elevenlabs  # noqa: F401
from voice_copilot.providers.tts import openai as _openai  # noqa: F401
from voice_copilot.providers.tts import piper as _piper  # noqa: F401
from voice_copilot.providers.tts import silero as _silero  # noqa: F401
from voice_copilot.providers.tts.base import TTSChunk, TTSProvider

__all__ = ["TTSChunk", "TTSProvider"]
