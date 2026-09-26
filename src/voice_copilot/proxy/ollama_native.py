"""Parser for Ollama's native `/api/chat` streaming response (NDJSON).

Unlike `/v1/chat/completions` (OpenAI-compatible, SSE), the native endpoint
streams one JSON object per line:

    {"model":"...","message":{"role":"assistant","content":"hi","thinking":""},"done":false}
    ...
    {"model":"...","message":{...},"done":true,"total_duration":...}

litellm's `ollama_chat/...` model prefix and some tools (Open WebUI, native
Ollama clients) hit this endpoint directly.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import Event, EventKind
from voice_copilot.proxy.tool_events import decode_tool_input, publish_tool_call

log = logging.getLogger(__name__)

_INCREMENTAL_MIN_CHARS = 120
# Includes CJK fullwidth punctuation on purpose (zh/ja models use them).
_SENTENCE_ENDS = (".", "!", "?", "\n", "。", "！", "？", "…")  # noqa: RUF001


class OllamaNativeParser:
    def __init__(self, bus: EventBus, session_id: str | None = None) -> None:
        self._bus = bus
        self._session_id = session_id
        self._buf = b""
        self._text_buf: list[str] = []
        self._thinking_buf: list[str] = []
        # A reply that asks for tools is a step: the client runs them and
        # sends the results back in the next request.
        self._called_tools = False

    async def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._buf += chunk
        # NDJSON: one JSON object per line.
        while True:
            idx = self._buf.find(b"\n")
            if idx == -1:
                break
            line, self._buf = self._buf[:idx], self._buf[idx + 1 :]
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                try:
                    await self._dispatch(payload)
                except Exception:
                    log.exception("failed to handle ollama-native chunk")

    async def close(self) -> None:
        tail = self._buf.strip()
        if tail:
            try:
                payload = json.loads(tail.decode("utf-8", errors="replace"))
                if isinstance(payload, dict):
                    await self._dispatch(payload)
            except Exception:
                pass
        self._buf = b""
        await self._flush_turn()

    async def _dispatch(self, p: dict[str, Any]) -> None:
        msg = p.get("message") or {}
        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, str) and content:
                self._text_buf.append(content)
            thinking = msg.get("thinking")
            if isinstance(thinking, str) and thinking:
                self._thinking_buf.append(thinking)
            await self._publish_tool_calls(msg.get("tool_calls"))
            await self._try_incremental_flush()
        if p.get("done"):
            await self._flush_turn()
            final = not self._called_tools
            self._called_tools = False
            await self._emit(EventKind.TURN_ENDED, {"via": "ollama.proxy", "final": final})

    async def _publish_tool_calls(self, calls: Any) -> None:
        """Native tool calls arrive whole: `{"function": {"name", "arguments"}}`."""
        if not isinstance(calls, list):
            return
        for call in calls:
            fn = call.get("function") if isinstance(call, dict) else None
            if not isinstance(fn, dict) or not fn.get("name"):
                continue
            self._called_tools = True
            args = fn.get("arguments")
            await publish_tool_call(
                self._bus,
                source="ollama.proxy",
                session_id=self._session_id,
                tool=str(fn["name"]),
                tool_input=decode_tool_input(args) if isinstance(args, str) else args,
                tool_use_id=call.get("id") if isinstance(call.get("id"), str) else None,
            )

    async def _try_incremental_flush(self) -> None:
        """Emit AGENT_THINKING / AGENT_TEXT mid-turn.

        Thinking: flush at minimum size; no sentence-boundary requirement.
        Text: prefer sentence boundary; force-flush at 2x minimum.
        """
        if self._thinking_buf:
            joined = "".join(self._thinking_buf)
            if len(joined) >= _INCREMENTAL_MIN_CHARS:
                self._thinking_buf = []
                full = joined.strip()
                if full:
                    await self._emit(EventKind.AGENT_THINKING, {"text": full})
        if self._text_buf:
            joined = "".join(self._text_buf)
            at_boundary = joined.rstrip().endswith(_SENTENCE_ENDS)
            if len(joined) >= _INCREMENTAL_MIN_CHARS and (
                at_boundary or len(joined) >= _INCREMENTAL_MIN_CHARS * 2
            ):
                self._text_buf = []
                full = joined.strip()
                if full:
                    await self._emit(EventKind.AGENT_TEXT, {"text": full})

    async def _flush_turn(self) -> None:
        if self._thinking_buf:
            full = "".join(self._thinking_buf).strip()
            self._thinking_buf = []
            if full:
                await self._emit(EventKind.AGENT_THINKING, {"text": full})
        if self._text_buf:
            full = "".join(self._text_buf).strip()
            self._text_buf = []
            if full:
                await self._emit(EventKind.AGENT_TEXT, {"text": full})

    async def _emit(self, kind: EventKind, payload: dict[str, Any]) -> None:
        if self._session_id is not None:
            payload = {**payload, "session_id": self._session_id}
        await self._bus.publish(Event(kind=kind, source="ollama.proxy", payload=payload))
