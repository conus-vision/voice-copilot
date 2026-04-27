"""faster-whisper — local STT via CTranslate2."""

from __future__ import annotations

from voice_copilot.providers.registry import register
from voice_copilot.providers.stt.base import NotInstalled

try:
    import faster_whisper  # noqa: F401

    _HAVE = True
except ImportError:
    _HAVE = False


if _HAVE:
    import asyncio
    import tempfile
    from pathlib import Path

    from faster_whisper import WhisperModel

    from voice_copilot.providers.stt.base import AudioContainer, STTProvider, STTResult

    @register("stt", "faster-whisper")
    class FasterWhisperProvider(STTProvider):
        name = "faster-whisper"

        def __init__(
            self, model: str = "small", device: str = "auto", compute_type: str = "int8"
        ) -> None:
            self._model_name = model
            self._device = device
            self._compute_type = compute_type
            self._model: WhisperModel | None = None

        def _load(self) -> WhisperModel:
            if self._model is None:
                self._model = WhisperModel(
                    self._model_name, device=self._device, compute_type=self._compute_type
                )
            return self._model

        async def transcribe(
            self,
            audio: bytes,
            *,
            container: AudioContainer,
            language: str | None = None,
            sample_rate: int = 48_000,
        ) -> STTResult:
            # faster-whisper wants a file path or file-like; writing a temp is safest
            # because some containers (webm) need ffmpeg.
            def _run() -> STTResult:
                with tempfile.NamedTemporaryFile(suffix=f".{container}", delete=False) as f:
                    f.write(audio)
                    path = Path(f.name)
                try:
                    model = self._load()
                    segments, info = model.transcribe(str(path), language=language, vad_filter=True)
                    text = "".join(seg.text for seg in segments).strip()
                    return STTResult(
                        text=text, language=info.language, confidence=info.language_probability
                    )
                finally:
                    path.unlink(missing_ok=True)

            return await asyncio.to_thread(_run)

else:

    @register("stt", "faster-whisper")
    class _FWStub(NotInstalled):
        def __init__(self, **_: object) -> None:
            super().__init__(name="faster-whisper", extra="local-stt")
