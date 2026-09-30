"""Piper: a voice that runs on this computer, fetched once per language.

The engine (sherpa-onnx) is an optional extra, so these tests cover
everything around it: which voice a language gets, the WAV it returns, the
one-time download and the checks on the archive. A real synthesis runs when
the extra is installed and a voice folder is given in PIPER_TEST_VOICE.
"""

from __future__ import annotations

import asyncio
import io
import os
import tarfile
import threading
import wave
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from voice_copilot.providers import registry
from voice_copilot.providers import tts as _tts  # noqa: F401
from voice_copilot.providers.tts import piper


def _voice_files(folder: Path, name: str = "xx_XX-test-medium") -> Path:
    folder.mkdir(parents=True)
    (folder / f"{name}.onnx").write_bytes(b"onnx")
    (folder / "tokens.txt").write_text("_ 0\n", encoding="utf-8")
    (folder / "espeak-ng-data").mkdir()
    (folder / "espeak-ng-data" / "phontab").write_bytes(b"x")
    return folder


def test_every_narration_language_has_a_voice() -> None:
    tts = registry.build("tts", "piper", {"voices": {"uk": "uk_UA-custom"}})
    assert isinstance(tts, piper.PiperTTS)
    assert tts.output_format == "wav"
    assert tts.voice_for("en") == "en_US-lessac-medium"
    assert tts.voice_for("uk") == "uk_UA-custom"
    assert tts.voice_for("de") == "en_US-lessac-medium"
    assert tts.voice_for("fr", "fr_FR-other") == "fr_FR-other"
    assert set(piper.DEFAULT_VOICES) == {"en", "es", "fr", "uk", "ru"}


def test_wav_is_16_bit_mono_with_a_silent_tail() -> None:
    data = piper.to_wav([0.0, 0.5, -1.5], 16000)
    with wave.open(io.BytesIO(data)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16000)
        frames = w.readframes(w.getnframes())
    assert w.getnframes() == 3 + int(16000 * piper._TAIL_SILENCE_S)
    assert frames[2:4] == (16383).to_bytes(2, "little", signed=True)
    assert frames[4:6] == (-32767).to_bytes(2, "little", signed=True)  # clipped


async def test_a_voice_already_on_disk_is_used_as_is(tmp_path: Path) -> None:
    _voice_files(tmp_path / "vits-piper-en_US-lessac-medium")
    tts = piper.PiperTTS(models_dir=str(tmp_path), download=False)
    assert await tts._voice_folder("en_US-lessac-medium") == tmp_path / (
        "vits-piper-en_US-lessac-medium"
    )
    custom = _voice_files(tmp_path / "my-voice")
    assert await tts._voice_folder(str(custom)) == custom


async def test_without_download_a_missing_voice_is_reported(tmp_path: Path) -> None:
    tts = piper.PiperTTS(models_dir=str(tmp_path), download=False)
    with pytest.raises(RuntimeError, match="not in"):
        await tts._voice_folder("en_US-lessac-medium")


def _archive(path: Path, voice: str, *, evil: bool = False) -> None:
    source = _voice_files(path.parent / "src" / f"vits-piper-{voice}", voice)
    with tarfile.open(path, "w:bz2") as tar:
        tar.add(source, arcname=source.name)
        if evil:
            info = tarfile.TarInfo("../escaped.txt")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))


@pytest.fixture
def release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "release"
    root.mkdir()
    handler = partial(SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a: None  # type: ignore[attr-defined]
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setattr(piper, "RELEASE_URL", f"http://127.0.0.1:{httpd.server_address[1]}")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    try:
        yield root
    finally:
        httpd.shutdown()


async def test_a_voice_is_downloaded_once_and_unpacked(tmp_path: Path, release: Path) -> None:
    _archive(release / "vits-piper-es_ES-test-medium.tar.bz2", "es_ES-test-medium")
    models = tmp_path / "voices"
    tts = piper.PiperTTS(models_dir=str(models))
    folders = await asyncio.gather(
        tts._voice_folder("es_ES-test-medium"), tts._voice_folder("es_ES-test-medium")
    )
    assert folders[0] == folders[1] == models / "vits-piper-es_ES-test-medium"
    assert piper._complete(folders[0])
    # Nothing but the voice is left behind.
    assert [p.name for p in models.iterdir()] == ["vits-piper-es_ES-test-medium"]


async def test_an_unknown_voice_names_the_release(tmp_path: Path, release: Path) -> None:
    tts = piper.PiperTTS(models_dir=str(tmp_path / "voices"))
    with pytest.raises(RuntimeError, match="no piper voice named 'xx_XX-nope'"):
        await tts._voice_folder("xx_XX-nope")


async def test_an_archive_that_escapes_its_folder_is_refused(tmp_path: Path, release: Path) -> None:
    _archive(release / "vits-piper-fr_FR-test-medium.tar.bz2", "fr_FR-test-medium", evil=True)
    tts = piper.PiperTTS(models_dir=str(tmp_path / "voices"))
    with pytest.raises(RuntimeError, match="unexpected entry"):
        await tts._voice_folder("fr_FR-test-medium")
    assert not (tmp_path / "escaped.txt").exists()


async def test_without_the_extra_the_error_says_what_to_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "sherpa_onnx", None)  # import fails
    folder = _voice_files(tmp_path / "vits-piper-en_US-lessac-medium")
    tts = piper.PiperTTS(voice=str(folder))
    with pytest.raises(RuntimeError, match=r"voice-copilot\[local-tts\]"):
        async for _ in tts.synthesize("hi", language="en"):
            pass


@pytest.mark.skipif(
    not os.environ.get("PIPER_TEST_VOICE"), reason="set PIPER_TEST_VOICE to a voice folder"
)
async def test_real_synthesis() -> None:
    pytest.importorskip("sherpa_onnx")
    tts = piper.PiperTTS(voice=os.environ["PIPER_TEST_VOICE"])
    chunks = [c async for c in tts.synthesize("Two edits in the parser.", language="en")]
    with wave.open(io.BytesIO(chunks[0].data)) as w:
        assert w.getnframes() / w.getframerate() > 0.8
    assert chunks[-1].is_last
