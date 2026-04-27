import pytest

from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import Event, EventKind


@pytest.mark.asyncio
async def test_publish_delivers_event_to_subscriber() -> None:
    bus = EventBus()
    event = Event(kind=EventKind.AGENT_TEXT, payload={"text": "hello"})

    async with bus.subscribe() as queue:
        await bus.publish(event)

        assert await queue.get() == event


@pytest.mark.asyncio
async def test_subscribers_receive_independent_copies() -> None:
    bus = EventBus()
    event = Event(kind=EventKind.TURN_STARTED)

    async with bus.subscribe() as first, bus.subscribe() as second:
        await bus.publish(event)

        assert await first.get() == event
        assert await second.get() == event


@pytest.mark.asyncio
async def test_slow_subscriber_drops_oldest_event_when_queue_is_full() -> None:
    bus = EventBus(queue_maxsize=1)
    first = Event(kind=EventKind.AGENT_TEXT, payload={"text": "first"})
    second = Event(kind=EventKind.AGENT_TEXT, payload={"text": "second"})

    async with bus.subscribe() as queue:
        await bus.publish(first)
        await bus.publish(second)

        assert await queue.get() == second


@pytest.mark.asyncio
async def test_unsubscribed_queue_stops_receiving_events() -> None:
    bus = EventBus()
    async with bus.subscribe() as queue:
        pass

    await bus.publish(Event(kind=EventKind.AGENT_TEXT))

    assert queue.empty()
