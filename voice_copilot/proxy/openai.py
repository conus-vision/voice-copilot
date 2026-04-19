"""SSE parser for OpenAI Chat Completions + Responses streaming.

Covers the common `data: {...}` lines. The Responses API (used by Codex) is
richer — for now we only extract text content and reasoning summaries where
they appear; full coverage can land with Э10 when we exercise Codex against
this proxy.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import Event, EventKind

log = logging.getLogger(__name__)


class OpenAISSEParser:
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._buf = b""

    async def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._buf += chunk
        while True:
            sep, rest = self._split_event(self._buf)
            if sep is None:
                break
            event_bytes, self._buf = sep, rest
            try:
                await self._handle_event_bytes(event_bytes)
            except Exception:  # noqa: BLE001
                log.exception("failed to handle openai SSE event")

    async def close(self) -> None:
        tail = self._buf.strip()
        if tail:
            try:
                await self._handle_event_bytes(tail)
            except Exception:  # noqa: BLE001
                log.exception("failed to handle final openai SSE event")
        self._buf = b""

    @staticmethod
    def _split_event(buf: bytes) -> tuple[bytes | None, bytes]:
        for sep in (b"\n\n", b"\r\n\r\n"):
            idx = buf.find(sep)
            if idx != -1:
                return buf[:idx], buf[idx + len(sep):]
        return None, buf

    async def _handle_event_bytes(self, raw: bytes) -> None:
        data_parts: list[str] = []
        for line in raw.decode("utf-8", errors="replace").splitlines():
            if line.startswith("data:"):
                body = line[5:].lstrip()
                if body == "[DONE]":
                    await self._emit(EventKind.TURN_ENDED, {"via": "openai.proxy"})
                    return
                data_parts.append(body)
        if not data_parts:
            return
        try:
            payload = json.loads("\n".join(data_parts))
        except json.JSONDecodeError:
            return
        if isinstance(payload, dict):
            await self._dispatch(payload)

    async def _dispatch(self, p: dict[str, Any]) -> None:
        # Chat Completions: {choices: [{delta: {content: "..."}}]}
        choices = p.get("choices")
        if isinstance(choices, list) and choices:
            delta = (choices[0] or {}).get("delta") or {}
            text = delta.get("content")
            if text:
                await self._emit(EventKind.AGENT_TEXT, {"text": text, "streaming": True})
            return

        # Responses API: event objects with .type and .delta/.item
        t = p.get("type")
        if t == "response.output_text.delta":
            text = p.get("delta") or ""
            if text:
                await self._emit(EventKind.AGENT_TEXT, {"text": text, "streaming": True})
            return
        if t == "response.reasoning_summary_text.delta":
            text = p.get("delta") or ""
            if text:
                await self._emit(EventKind.AGENT_THINKING, {"text": text, "streaming": True})
            return
        if t == "response.completed":
            await self._emit(EventKind.TURN_ENDED, {"via": "openai.proxy"})
            return

    async def _emit(self, kind: EventKind, payload: dict[str, Any]) -> None:
        await self._bus.publish(Event(kind=kind, source="openai.proxy", payload=payload))
