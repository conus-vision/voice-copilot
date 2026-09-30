"""Piper voices, run locally through sherpa-onnx: narration without the network.

Install with the `local-tts` extra (sherpa-onnx: ONNX Runtime and espeak-ng
in a few megabytes, no PyTorch). The first line in a language downloads its
voice once, about 65 MB, from the sherpa-onnx model releases on GitHub, into
`voices/` next to the config file. After that nothing leaves the machine.

Options (`tts.options` in the config):

* ``voice``: a voice name from that release (``en_US-amy-medium``), or the path
  of a folder unpacked from it. Without it each language gets a default.
* ``voices``: per-language overrides, ``{"uk": "uk_UA-ukrainian_tts-medium"}``.
* ``speaker``: speaker number for voices that have several (default 0).
* ``speed``: 1.0 is normal.
* ``threads``: CPU threads for synthesis (default 2).
* ``models_dir``: where voices are kept.
* ``download``: false to never fetch a voice (use folders you put there).
"""

from __future__ import annotations

import array
import asyncio
import io
import logging
import shutil
import tarfile
import tempfile
import threading
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from voice_copilot.providers.registry import register
from voice_copilot.providers.tts.base import TTSChunk, TTSProvider

log = logging.getLogger(__name__)

#: One voice per narration language; all from the sherpa-onnx model release,
#: each checked by transcribing its speech back with Whisper. The Ukrainian
#: `ukrainian_tts` voice spells by characters, which sherpa-onnx feeds espeak
#: phonemes instead: it comes out silent.
DEFAULT_VOICES: dict[str, str] = {
    "en": "en_US-lessac-medium",
    "es": "es_ES-davefx-medium",
    "fr": "fr_FR-siwis-medium",
    "uk": "uk_UA-lada-x_low",
    "ru": "ru_RU-irina-medium",
}
RELEASE_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models"
_DOWNLOAD_TIMEOUT_S = 600.0
#: Piper stops on the last phoneme; a little silence keeps players from
#: clipping the final word.
_TAIL_SILENCE_S = 0.15


def default_models_dir() -> Path:
    from voice_copilot.core.config import config_path

    return config_path().parent / "voices"


@register("tts", "piper")
class PiperTTS(TTSProvider):
    name = "piper"
    output_format = "wav"

    def __init__(
        self,
        voice: str | None = None,
        voices: dict[str, str] | None = None,
        speaker: int = 0,
        speed: float = 1.0,
        threads: int = 2,
        models_dir: str | None = None,
        download: bool = True,
    ) -> None:
        self._voice = voice
        self._voices = {**DEFAULT_VOICES, **(voices or {})}
        self._speaker = int(speaker)
        self._speed = float(speed)
        self._threads = max(1, int(threads))
        self._models_dir = Path(models_dir).expanduser() if models_dir else default_models_dir()
        self._download = download
        self._engines: dict[Path, Any] = {}
        self._engine_lock = threading.Lock()
        self._download_locks: dict[str, asyncio.Lock] = {}

    def voice_for(self, language: str, voice: str | None = None) -> str:
        return voice or self._voice or self._voices.get(language) or self._voices["en"]

    async def synthesize(
        self,
        text: str,
        *,
        language: str,
        voice: str | None = None,
    ) -> AsyncIterator[TTSChunk]:
        folder = await self._voice_folder(self.voice_for(language, voice))
        data = await asyncio.to_thread(self._synthesize_wav, folder, text)
        yield TTSChunk(format="wav", data=data)
        yield TTSChunk(format="wav", data=b"", is_last=True)

    # ------------------------------------------------------------ voices

    async def _voice_folder(self, voice: str) -> Path:
        given = Path(voice).expanduser()
        if given.is_dir():
            return given
        folder = self._models_dir / f"vits-piper-{voice}"
        if _complete(folder):
            return folder
        if not self._download:
            raise RuntimeError(f"piper voice {voice!r} is not in {self._models_dir}")
        lock = self._download_locks.setdefault(voice, asyncio.Lock())
        async with lock:
            if not _complete(folder):
                await _download_voice(voice, self._models_dir)
        return folder

    # ------------------------------------------------------------ synthesis

    def _engine(self, folder: Path) -> Any:
        try:
            import sherpa_onnx
        except ImportError as e:
            raise RuntimeError(
                "The piper voice needs the local-tts extra: pipx install 'voice-copilot[local-tts]'"
            ) from e
        with self._engine_lock:
            engine = self._engines.get(folder)
            if engine is None:
                model = next(folder.glob("*.onnx"))
                config = sherpa_onnx.OfflineTtsConfig(
                    model=sherpa_onnx.OfflineTtsModelConfig(
                        vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                            model=str(model),
                            tokens=str(folder / "tokens.txt"),
                            data_dir=str(folder / "espeak-ng-data"),
                        ),
                        num_threads=self._threads,
                        provider="cpu",
                    ),
                    max_num_sentences=1,
                )
                engine = sherpa_onnx.OfflineTts(config)
                self._engines[folder] = engine
            return engine

    def _synthesize_wav(self, folder: Path, text: str) -> bytes:
        engine = self._engine(folder)
        speaker = self._speaker if self._speaker < max(1, engine.num_speakers) else 0
        with self._engine_lock:
            audio = engine.generate(text, sid=speaker, speed=self._speed)
        if not audio.samples:
            raise RuntimeError("piper produced no audio")
        return to_wav(audio.samples, audio.sample_rate)


def to_wav(samples: Any, sample_rate: int) -> bytes:
    """Float samples in [-1, 1] as a 16-bit mono WAV file, with a short silent tail."""
    pcm = array.array("h", (int(max(-1.0, min(1.0, s)) * 32767) for s in samples))
    pcm.extend([0] * int(sample_rate * _TAIL_SILENCE_S))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(int(sample_rate))
        out.writeframes(pcm.tobytes())
    return buf.getvalue()


def _complete(folder: Path) -> bool:
    return (
        folder.is_dir()
        and any(folder.glob("*.onnx"))
        and (folder / "tokens.txt").is_file()
        and (folder / "espeak-ng-data").is_dir()
    )


async def _download_voice(voice: str, models_dir: Path) -> None:
    import httpx

    url = f"{RELEASE_URL}/vits-piper-{voice}.tar.bz2"
    models_dir.mkdir(parents=True, exist_ok=True)
    log.info("piper: downloading voice %s from %s", voice, url)
    with tempfile.TemporaryDirectory(dir=models_dir, prefix=".download-") as tmp:
        archive = Path(tmp) / "voice.tar.bz2"
        async with (
            httpx.AsyncClient(follow_redirects=True, timeout=_DOWNLOAD_TIMEOUT_S) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code == 404:
                raise RuntimeError(f"no piper voice named {voice!r} (see {RELEASE_URL})")
            response.raise_for_status()
            with archive.open("wb") as f:
                async for chunk in response.aiter_bytes(1 << 20):
                    f.write(chunk)
        await asyncio.to_thread(_unpack, archive, Path(tmp) / "unpacked")
        unpacked = Path(tmp) / "unpacked" / f"vits-piper-{voice}"
        if not _complete(unpacked):
            raise RuntimeError(f"the download of piper voice {voice!r} is incomplete")
        target = models_dir / unpacked.name
        if target.exists():
            shutil.rmtree(target)
        unpacked.rename(target)
    log.info("piper: voice %s ready in %s", voice, models_dir)


def _unpack(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with tarfile.open(archive, "r:bz2") as tar:
        members = tar.getmembers()
        for member in members:
            target = (dest / member.name).resolve()
            if not target.is_relative_to(root) or member.issym() or member.islnk():
                raise RuntimeError(f"unexpected entry {member.name!r} in the voice archive")
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, members=members, filter="data")
        else:  # before Python 3.11.4; every entry was checked above
            tar.extractall(dest, members=members)
