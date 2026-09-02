"""The idle timer: speak up during long stretches of quiet tool work.

A grep/read/run streak carries no thinking and no answer, so it never reaches
the usual flush gate — the user just hears silence while the agent works.
"""

import asyncio
from collections.abc import AsyncIterator

import pytest

from voice_copilot.commentator.format import build_narration_user
from voice_copilot.commentator.pipeline import Commentator, _Batch, _SessionContext
from voice_copilot.core.bus import EventBus
from voice_copilot.core.config import CommentatorConfig
from voice_copilot.core.events import Event, EventKind


class _FakeLLM:
    prompt_style = "api"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def stream_chat(
        self,
        messages: list,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        self.prompts.append(messages[0].content)

        async def gen() -> AsyncIterator[str]:
            yield "Читаем парсеры."

        return gen()


def _tool_event(tool: str = "Read", **input_kwargs: str) -> Event:
    return Event(
        kind=EventKind.TOOL_CALL_STARTED,
        source="anthropic.proxy",
        payload={"tool": tool, "input": input_kwargs or {"file_path": "a.py"}},
    )


def _batch(*events: Event) -> _Batch:
    return _Batch(events=list(events))


def test_idle_trigger_narrates_tool_only_batches() -> None:
    commentator = Commentator(EventBus(), CommentatorConfig(), "en", llm=_FakeLLM())  # type: ignore[arg-type]
    ctx = _SessionContext()

    assert (
        commentator._should_flush(_batch(_tool_event()), ctx, trigger="idle", word_count=0) is True
    )
    # The same batch is not worth waking anyone for on the normal path.
    assert (
        commentator._should_flush(_batch(_tool_event()), ctx, trigger="normal", word_count=0)
        is False
    )


def test_idle_trigger_stays_quiet_when_only_a_turn_boundary_happened() -> None:
    commentator = Commentator(EventBus(), CommentatorConfig(), "en", llm=_FakeLLM())  # type: ignore[arg-type]
    turn_end = Event(kind=EventKind.TURN_ENDED, source="anthropic.proxy", payload={})
    assert (
        commentator._should_flush(_batch(turn_end), _SessionContext(), trigger="idle", word_count=0)
        is False
    )


def test_opening_prompt_asks_for_the_task_first() -> None:
    prompt = build_narration_user(
        user_query="почини парсер",
        summary=None,
        events=[_tool_event()],
        opening=True,
    )
    assert "первая реплика" in prompt
    assert "почини парсер" in prompt

    later = build_narration_user(user_query="почини парсер", summary=None, events=[_tool_event()])
    assert "первая реплика" not in later


@pytest.mark.asyncio
async def test_idle_timer_fires_and_opens_with_the_task() -> None:
    bus = EventBus()
    llm = _FakeLLM()
    cfg = CommentatorConfig(idle_narration_ms=120, debounce_ms=200)
    commentator = Commentator(bus, cfg, "ru", llm=llm)  # type: ignore[arg-type]

    async with bus.subscribe() as q:
        runner = asyncio.create_task(commentator.run())
        await asyncio.sleep(0.05)  # let the commentator subscribe
        await bus.publish(
            Event(
                kind=EventKind.USER_MESSAGE,
                source="anthropic.proxy",
                payload={"text": "почини парсер", "delivery": "observed"},
            )
        )
        await bus.publish(_tool_event("Grep", pattern="extract_user_query"))
        await bus.publish(_tool_event("Read", file_path="src/proxy/openai.py"))

        try:
            spoken = None
            async with asyncio.timeout(3):
                while spoken is None:
                    event = await q.get()
                    if event.kind is EventKind.COMMENTATOR_UTTERANCE and not event.payload.get(
                        "streaming"
                    ):
                        spoken = event.payload["text"]
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)

    assert spoken == "Читаем парсеры."
    # Nothing had been said yet, so the line was asked to name the task first.
    assert llm.prompts and "первая реплика" in llm.prompts[0]
    assert "почини парсер" in llm.prompts[0]
    assert "- searched: extract_user_query" in llm.prompts[0]
