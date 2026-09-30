"""Stopping a loop at the moment an event reaches it.

On Python 3.11, `asyncio.wait_for` hands back the item and drops the
cancellation when both land in the same turn of the event loop. The narrator
then ran on after it was stopped (a Windows CI run hung on it), and a
plugin's command poll gave a command to a request that was already gone.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from voice_copilot.commentator.pipeline import Commentator
from voice_copilot.companion.hub import CompanionHub
from voice_copilot.core.bus import EventBus
from voice_copilot.core.config import CommentatorConfig
from voice_copilot.core.events import Event, EventKind
from voice_copilot.proxy.session import SessionRegistry


class _SilentLLM:
    prompt_style = "api"

    def stream_chat(self, messages: list[Any], **kwargs: Any) -> AsyncIterator[str]:
        async def gen() -> AsyncIterator[str]:
            if False:
                yield ""

        return gen()


def _thought() -> Event:
    return Event(
        kind=EventKind.AGENT_THINKING,
        source="anthropic.proxy",
        payload={"text": "Checking the parser module first now."},
    )


async def test_the_narrator_stops_when_an_event_comes_with_the_stop() -> None:
    bus = EventBus()
    cfg = CommentatorConfig(idle_narration_ms=10_000, debounce_ms=5_000)
    runner = asyncio.create_task(Commentator(bus, cfg, "en", llm=_SilentLLM()).run())  # type: ignore[arg-type]
    await asyncio.sleep(0.05)
    await bus.publish(_thought())  # opens the debounce window
    await asyncio.sleep(0.05)  # the narrator now waits inside it for more
    await bus.publish(_thought())  # publishing does not yield: the stop lands in the same turn
    runner.cancel()
    try:
        async with asyncio.timeout(2):
            await asyncio.gather(runner, return_exceptions=True)
    except TimeoutError:
        pytest.fail("the narrator was still running after it was cancelled")
    assert runner.cancelled()


async def test_a_command_stays_queued_when_its_poll_is_cancelled() -> None:
    bus = EventBus()
    hub = CompanionHub(bus, SessionRegistry(), port=8765, owns_dialog=True)
    await hub.handle_events("pi", "s1", [{"kind": "session.started", "payload": {}}])
    poll = asyncio.create_task(hub.next_command("pi", "s1", wait_s=10))
    await asyncio.sleep(0.05)
    assert await hub.send_message("also count the lines")
    poll.cancel()  # the plugin's request went away as the command arrived
    with pytest.raises(asyncio.CancelledError):
        await poll
    command = await hub.next_command("pi", "s1", wait_s=1)
    assert command is not None
    assert command["payload"]["text"] == "also count the lines"
