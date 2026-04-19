from voice_copilot.providers.stt.base import STTProvider, STTResult

from voice_copilot.providers.stt import whisper_api as _whisper_api  # noqa: F401
from voice_copilot.providers.stt import faster_whisper as _fw  # noqa: F401
from voice_copilot.providers.stt import deepgram as _dg  # noqa: F401

__all__ = ["STTProvider", "STTResult"]
