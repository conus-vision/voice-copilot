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
from voice_copilot.proxy.tool_events import decode_tool_input, publish_tool_call

log = logging.getLogger(__name__)


class AnthropicSSEParser:
    def __init__(
        self,
        bus: EventBus,
        session_id: str | None = None,
        *,
        internal: bool = False,
        subagent: bool = False,
    ) -> None:
        self._bus = bus
        self._session_id = session_id
        # A forked sub-agent's stream: its turn end is not the user's turn end.
        self._subagent = subagent
        # The CLI talking to itself (a title-generation call, a quota probe):
        # its reply and its turn end are not the agent's, and must not be
        # narrated, reviewed, or read as the task finishing.
        self._internal = internal
        self._buf = b""
        self._blocks: dict[int, dict[str, Any]] = {}
        # `message_delta` carries stop_reason; "tool_use" means the turn goes on
        # after the tool results come back, so TURN_ENDED must not read as done.
        self._stop_reason: str | None = None

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
            except Exception:
                log.exception("failed to handle anthropic SSE event")

    async def close(self) -> None:
        tail = self._buf.strip()
        if tail:
            try:
                await self._handle_event_bytes(tail)
            except Exception:
                log.exception("failed to handle final anthropic SSE event")
        self._buf = b""
        await self._flush_pending_tools()
        self._blocks.clear()

    @staticmethod
    def _split_event(buf: bytes) -> tuple[bytes | None, bytes]:
        for sep in (b"\n\n", b"\r\n\r\n"):
            idx = buf.find(sep)
            if idx != -1:
                return buf[:idx], buf[idx + len(sep) :]
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

        if t == "message_delta":
            reason = (p.get("delta") or {}).get("stop_reason")
            if isinstance(reason, str):
                self._stop_reason = reason
            return

        if t == "message_start":
            self._stop_reason = None
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
                "emitted": False,
            }
            # A tool_use block starts with an empty `input` — the arguments
            # arrive afterwards as input_json_delta. Emitting here would name
            # the tool but never the file or command, so we wait for the block
            # to close. Providers that do inline the input are emitted at once.
            if block.get("type") == "tool_use" and block.get("input"):
                self._blocks[idx]["emitted"] = True
                await self._emit_tool_call(self._blocks[idx], block.get("input"))
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
            elif btype == "tool_use":
                # Arguments are complete now. We don't emit FINISHED — the
                # result comes back via the next request's tool_result, not
                # in this stream.
                await self._flush_tool_block(block)
            return

        if t == "message_stop":
            await self._flush_pending_tools()
            final = self._stop_reason != "tool_use"
            self._stop_reason = None
            await self._emit(EventKind.TURN_ENDED, {"via": "anthropic.proxy", "final": final})
            self._blocks.clear()
            return

    async def _flush_tool_block(self, block: dict[str, Any]) -> None:
        """Emit a tool call once its streamed arguments are complete."""
        if block.get("emitted"):
            return
        block["emitted"] = True
        tool_input = block.get("input") or decode_tool_input(block.get("text"))
        await self._emit_tool_call(block, tool_input)

    async def _emit_tool_call(self, block: dict[str, Any], tool_input: Any) -> None:
        await publish_tool_call(
            self._bus,
            source="anthropic.proxy",
            session_id=self._session_id,
            tool=block.get("name"),
            tool_input=tool_input,
            tool_use_id=block.get("id"),
        )

    async def _flush_pending_tools(self) -> None:
        """Emit tool blocks left open by a truncated stream."""
        for block in list(self._blocks.values()):
            if block.get("type") == "tool_use":
                await self._flush_tool_block(block)

    async def _emit(self, kind: EventKind, payload: dict[str, Any]) -> None:
        if self._session_id is not None:
            payload = {**payload, "session_id": self._session_id}
        if self._internal:
            payload = {**payload, "internal": True}
        if self._subagent:
            payload = {**payload, "subagent": True}
        await self._bus.publish(Event(kind=kind, source="anthropic.proxy", payload=payload))
