"""Narration that reached the Trace must also reach the speakers.

The driver kept its own query counter fed by every request the proxy observed
— including an agent's internal side calls, like codex asking the model for a
session title mid-turn. That bumped the version, and the narration produced
for the user's actual question was then discarded as "stale": the line showed
up in the Trace and was never spoken.
"""

from __future__ import annotations

import asyncio

import pytest

from voice_copilot.audio.hub import AudioHub
from voice_copilot.audio.tts_driver import TTSDriver
from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import Event, EventKind

SESSION = "sess-1"
PROXY = "openai-chatgpt.proxy"


class _FakeTTS:
    output_format = "mp3"

    def __init__(self) -> None:
        self.spoken: list[str] = []

    async def synthesize(self, text: str, *, language: str | None = None):  # type: ignore[no-untyped-def]
        self.spoken.append(text)
        return
        yield


def _user(text: str) -> Event:
    return Event(
        kind=EventKind.USER_MESSAGE,
        source=PROXY,
        payload={"text": text, "session_id": SESSION, "delivery": "observed"},
    )


def _narration(version: int) -> Event:
    return Event(
        kind=EventKind.COMMENTATOR_UTTERANCE,
        source="commentator",
        payload={
            "text": "The agent is reading the project.",
            "streaming": False,
            "language": "ru",
            "session_id": SESSION,
            "query_version": version,
        },
    )


async def _play(events: list[Event]) -> list[str]:
    bus, tts = EventBus(), _FakeTTS()
    driver = TTSDriver(bus, AudioHub(), tts, "ru")
    run = asyncio.create_task(driver.run())
    await asyncio.sleep(0.05)
    for ev in events:
        await bus.publish(ev)
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.2)
    run.cancel()
    return tts.spoken


@pytest.mark.asyncio
async def test_narration_is_spoken_on_a_plain_turn() -> None:
    assert await _play([_user("оцени проект"), _narration(1)])


@pytest.mark.asyncio
async def test_an_agents_internal_request_does_not_mute_the_turn() -> None:
    spoken = await _play(
        [
            _user("оцени проект"),
            # codex generates a session title through the same endpoint; the
            # proxy sees it as another user message on the same session.
            _user("Generate a concise, single-line task title of at most 36 characters."),
            _narration(1),
        ]
    )
    assert spoken, "narration for the real question must still be spoken"


@pytest.mark.asyncio
async def test_a_new_question_still_clears_what_is_queued() -> None:
    # Barge-in is unchanged: the queue is dropped the moment a new question
    # lands, so nothing from the previous one is waiting to play.
    bus, tts = EventBus(), _FakeTTS()
    driver = TTSDriver(bus, AudioHub(), tts, "ru")
    driver.set_muted(True)  # keep the speaker loop from draining the queue
    run = asyncio.create_task(driver.run())
    await asyncio.sleep(0.05)
    await bus.publish(_user("q1"))
    await asyncio.sleep(0.05)
    driver.set_muted(False)
    await bus.publish(_narration(1))
    await asyncio.sleep(0.01)
    await bus.publish(_user("q2"))
    await asyncio.sleep(0.2)
    run.cancel()
    assert driver._pending == []


@pytest.mark.asyncio
async def test_a_cli_side_request_does_not_cut_a_line_that_is_playing() -> None:
    # codex asks the model for a session title over the same session while a
    # line is being read; the driver must not treat that as a new question
    # and abort the synthesis in flight.
    bus, tts = EventBus(), _FakeTTS()
    driver = TTSDriver(bus, AudioHub(), tts, "ru")
    run = asyncio.create_task(driver.run())
    await asyncio.sleep(0.05)
    await bus.publish(_user("оцени проект"))
    await asyncio.sleep(0.05)
    versions_before = dict(driver._query_versions)
    await bus.publish(
        _user(
            "Generate a concise, single-line task title of at most 36 characters. "
            "Do not answer the request. User prompt: оцени проект"
        )
    )
    await asyncio.sleep(0.05)
    await bus.publish(_user("оцени проект"))  # the same turn, re-sent
    await asyncio.sleep(0.05)
    run.cancel()
    assert driver._query_versions == versions_before
