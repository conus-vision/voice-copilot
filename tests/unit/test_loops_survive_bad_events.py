"""One event that breaks a handler must not end narration for the whole run.

The commentator and the TTS driver each run one loop over the bus; before,
an exception anywhere in it ended the loop and the rest of the session was
silent until a restart.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from voice_copilot.audio.hub import AudioHub
from voice_copilot.audio.tts_driver import TTSDriver
from voice_copilot.commentator.pipeline import Commentator
from voice_copilot.core.bus import EventBus
from voice_copilot.core.config import CommentatorConfig
from voice_copilot.core.events import Event, EventKind


class _FakeTTS:
    output_format = "mp3"

    def __init__(self) -> None:
        self.spoken: list[str] = []

    async def synthesize(self, text: str, *, language: str | None = None):  # type: ignore[no-untyped-def]
        self.spoken.append(text)
        return
        yield


def _line(text: str) -> Event:
    return Event(
        kind=EventKind.COMMENTATOR_UTTERANCE,
        source="commentator",
        payload={"text": text, "streaming": False, "language": "en"},
    )


async def test_tts_driver_keeps_speaking_after_a_failing_event() -> None:
    bus, tts = EventBus(), _FakeTTS()
    driver = TTSDriver(bus, AudioHub(), tts, "en")
    real_build = driver._build_utterance
    calls = {"n": 0}

    def flaky(ev: Event) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("unexpected payload")
        return real_build(ev)

    driver._build_utterance = flaky  # type: ignore[method-assign]
    run = asyncio.create_task(driver.run())
    await asyncio.sleep(0.05)
    await bus.publish(_line("first line breaks"))
    await asyncio.sleep(0.05)
    await bus.publish(_line("second line is spoken"))
    await asyncio.sleep(0.3)
    run.cancel()
    await asyncio.gather(run, return_exceptions=True)
    assert tts.spoken == ["second line is spoken"]


class _FakeLLM:
    prompt_style = "api"

    def __init__(self) -> None:
        self.calls = 0

    def stream_chat(self, messages: list[Any], **kwargs: Any) -> AsyncIterator[str]:
        self.calls += 1

        async def gen() -> AsyncIterator[str]:
            yield "Reading the parser."

        return gen()


async def test_commentator_keeps_narrating_after_a_failing_event() -> None:
    bus, llm = EventBus(), _FakeLLM()
    commentator = Commentator(bus, CommentatorConfig(debounce_ms=50), "en", llm=llm)  # type: ignore[arg-type]
    real_admit = commentator._admit
    calls = {"n": 0}

    def flaky(ev: Event) -> str | None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyError("payload without the expected field")
        return real_admit(ev)

    commentator._admit = flaky  # type: ignore[method-assign]
    spoken: list[str] = []
    async with bus.subscribe() as q:
        run = asyncio.create_task(commentator.run())
        await asyncio.sleep(0.05)
        edit = Event(kind=EventKind.FILE_EDITED, source="proxy", payload={"path": "a.py"})
        await bus.publish(edit)  # breaks the handler
        await bus.publish(
            Event(kind=EventKind.FILE_EDITED, source="proxy", payload={"path": "b.py"})
        )
        try:
            async with asyncio.timeout(3):
                while not spoken:
                    ev = await q.get()
                    if ev.kind is EventKind.COMMENTATOR_UTTERANCE and not ev.payload.get(
                        "streaming"
                    ):
                        spoken.append(ev.payload["text"])
        finally:
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
    assert spoken == ["Reading the parser."]
    assert not run.cancelled() or run.done()
