"""Stopping the TTS driver while it speaks must finish, and a cut line must not stop the queue.

The speaker loop swallowed every CancelledError around the line it was
waiting on, its own included, so shutting down (Ctrl+C) during a line left
`serve` and `vc` waiting forever.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from voice_copilot.audio.hub import AudioHub
from voice_copilot.audio.tts_driver import TTSDriver
from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import Event, EventKind
from voice_copilot.providers.tts.base import TTSChunk, TTSProvider


class _SlowVoice(TTSProvider):
    output_format = "mp3"

    def __init__(self) -> None:
        self.started: list[str] = []
        self.finished: list[str] = []

    async def synthesize(
        self, text: str, *, language: str, voice: str | None = None
    ) -> AsyncIterator[TTSChunk]:
        self.started.append(text)
        await asyncio.sleep(0.5 if text.startswith("long") else 0)
        self.finished.append(text)
        yield TTSChunk(format="mp3", data=b"x")


def _line(text: str) -> Event:
    return Event(
        kind=EventKind.COMMENTATOR_UTTERANCE,
        source="commentator",
        payload={"text": text, "streaming": False, "language": "en"},
    )


async def _until(predicate, timeout: float = 2.0) -> None:  # type: ignore[no-untyped-def]
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_stopping_during_a_line_finishes() -> None:
    bus, voice = EventBus(), _SlowVoice()
    run = asyncio.create_task(TTSDriver(bus, AudioHub(), voice, "en").run())
    await asyncio.sleep(0.05)
    await bus.publish(_line("long line"))
    await _until(lambda: voice.started)
    run.cancel()
    done, _ = await asyncio.wait({run}, timeout=1.0)
    assert run in done, "the driver kept running after it was cancelled"


async def test_an_interrupted_line_does_not_stop_the_next_one() -> None:
    bus, voice = EventBus(), _SlowVoice()
    run = asyncio.create_task(TTSDriver(bus, AudioHub(), voice, "en").run())
    try:
        await asyncio.sleep(0.05)
        await bus.publish(_line("long line"))
        await _until(lambda: voice.started)
        await bus.publish(Event(kind=EventKind.USER_INTERRUPT, source="panel", payload={}))
        await asyncio.sleep(0.05)
        await bus.publish(_line("next line"))
        await _until(lambda: "next line" in voice.finished)
    finally:
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
    assert "long line" not in voice.finished
