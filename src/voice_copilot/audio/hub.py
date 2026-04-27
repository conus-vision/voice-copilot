"""Registry of connected WebSocket clients for audio fan-out.

The WS endpoint (`web/ws.py`) registers each connection with the hub on
`accept` and deregisters on disconnect. The TTS driver uses the hub to
broadcast synthesized audio to everyone who's listening.

Broadcast is serialized with a lock so a single utterance's frames
(`audio_header` → binary bytes → `audio_end`) never interleave with another
utterance mid-stream.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket

log = logging.getLogger(__name__)


class AudioHub:
    def __init__(self) -> None:
        self._clients: list[WebSocket] = []
        self._client_lock = asyncio.Lock()
        self._broadcast_lock = asyncio.Lock()

    async def register(self, ws: WebSocket) -> None:
        async with self._client_lock:
            self._clients.append(ws)

    async def unregister(self, ws: WebSocket) -> None:
        async with self._client_lock:
            if ws in self._clients:
                self._clients.remove(ws)

    async def _snapshot(self) -> list[WebSocket]:
        async with self._client_lock:
            return list(self._clients)

    async def broadcast_text(self, msg: dict[str, Any]) -> None:
        payload = json.dumps(msg)
        for ws in await self._snapshot():
            try:
                await ws.send_text(payload)
            except Exception:
                log.debug("drop ws client (send_text failed)")

    async def broadcast_bytes(self, data: bytes) -> None:
        for ws in await self._snapshot():
            try:
                await ws.send_bytes(data)
            except Exception:
                log.debug("drop ws client (send_bytes failed)")

    def utterance_lock(self) -> asyncio.Lock:
        """TTS driver wraps one utterance's broadcast in `async with` this lock."""
        return self._broadcast_lock

    def has_clients(self) -> bool:
        return bool(self._clients)
