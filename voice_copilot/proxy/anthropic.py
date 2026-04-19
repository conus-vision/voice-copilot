"""SSE parser for Anthropic `/v1/messages` streaming responses.

Event timeline (simplified from the official schema):
    message_start
    content_block_start  {content_block: {type: text|thinking|tool_use, ...}}
    content_block_delta  {delta: {type: text_delta|thinking_delta|input_json_delta, ...}}
    content_block_stop
    message_delta        {delta: {stop_reason, ...}}
    message_stop

We accumulate block deltas and emit our canonical events on `content_block_stop`
(so the commentator receives coherent chunks, not token-by-token garbage).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import Event, EventKind

log = logging.getLogger(__name__)


class AnthropicSSEParser:
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._buf = b""
        self._blocks: dict[int, dict[str, Any]] = {}

    async def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._buf += chunk
        # SSE events end with a blank line — CRLF or LF variants in the wild.
        while True:
            sep, rest = self._split_event(self._buf)
            if sep is None:
                break
            event_bytes, self._buf = sep, rest
            try:
                await self._handle_event_bytes(event_bytes)
            except Exception:  # noqa: BLE001
                log.exception("failed to handle anthropic SSE event")

    async def close(self) -> None:
        tail = self._buf.strip()
        if tail:
            try:
                await self._handle_event_bytes(tail)
            except Exception:  # noqa: BLE001
                log.exception("failed to handle final anthropic SSE event")
        self._buf = b""
        self._blocks.clear()

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
                data_parts.append(line[5:].lstrip())
        if not data_parts:
            return
        try:
            payload = json.loads("\n".join(data_parts))
        except json.JSONDecodeError:
            return
        if isinstance(payload, dict):
            await self._dispatch(payload)

    async def _dispatch(self, p: dict[str, Any]) -> None:
        t = p.get("type")

        if t == "message_start":
            msg = p.get("message") or {}
            await self._emit(
                EventKind.TURN_STARTED,
                {"model": msg.get("model"), "role": msg.get("role"), "via": "anthropic.proxy"},
            )
            return

        if t == "content_block_start":
            idx = int(p.get("index", 0))
            block = p.get("content_block") or {}
            self._blocks[idx] = {
                "type": block.get("type"),
                "text": "",
                "name": block.get("name"),
                "id": block.get("id"),
                "input": block.get("input"),
            }
            if block.get("type") == "tool_use":
                await self._emit(
                    EventKind.TOOL_CALL_STARTED,
                    {
                        "tool_use_id": block.get("id"),
                        "tool": block.get("name"),
                        "input": block.get("input"),
                    },
                )
            return

        if t == "content_block_delta":
            idx = int(p.get("index", 0))
            block = self._blocks.get(idx)
            if block is None:
                return
            delta = p.get("delta") or {}
            dt = delta.get("type")
            if dt == "text_delta":
                block["text"] += delta.get("text", "")
            elif dt == "thinking_delta":
                block["text"] += delta.get("thinking", "")
            elif dt == "input_json_delta":
                block["text"] += delta.get("partial_json", "")
            return

        if t == "content_block_stop":
            idx = int(p.get("index", 0))
            block = self._blocks.pop(idx, None)
            if not block:
                return
            btype = block.get("type")
            text = block.get("text") or ""
            if btype == "text" and text:
                await self._emit(EventKind.AGENT_TEXT, {"text": text})
            elif btype == "thinking" and text:
                await self._emit(EventKind.AGENT_THINKING, {"text": text})
            # tool_use TOOL_CALL_STARTED was already emitted at block_start; we
            # don't emit FINISHED here — the result comes back via the next
            # request's tool_result, not in this stream.
            return

        if t == "message_stop":
            await self._emit(EventKind.TURN_ENDED, {"via": "anthropic.proxy"})
            self._blocks.clear()
            return

    async def _emit(self, kind: EventKind, payload: dict[str, Any]) -> None:
        await self._bus.publish(Event(kind=kind, source="anthropic.proxy", payload=payload))
