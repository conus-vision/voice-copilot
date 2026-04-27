"""Minimal asyncio fan-out event bus.

Publishers call `publish(event)`. Subscribers get their own `asyncio.Queue`
via `subscribe()` — decoupled from publishers and from each other.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from voice_copilot.core.events import Event


class EventBus:
    def __init__(self, queue_maxsize: int = 1024) -> None:
        self._subscribers: list[asyncio.Queue[Event]] = []
        self._queue_maxsize = queue_maxsize
        self._lock = asyncio.Lock()

    async def publish(self, event: Event) -> None:
        async with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            # Drop-oldest back-pressure: a slow subscriber must not stall the bus.
            if q.full():
                with suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(event)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[Event]]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_maxsize)
        async with self._lock:
            self._subscribers.append(q)
        try:
            yield q
        finally:
            async with self._lock:
                self._subscribers.remove(q)
