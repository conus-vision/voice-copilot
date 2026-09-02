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
from voice_copilot.proxy.tool_events import decode_tool_input, publish_tool_call

log = logging.getLogger(__name__)

# Minimum accumulated reasoning/text length before we emit an incremental
# flush. Below this, we keep buffering — short fragments get dropped by the
# importance classifier anyway.
_INCREMENTAL_MIN_CHARS = 120
# Sentence terminators (including CJK / RU) to cut incremental flushes on.
_SENTENCE_ENDS = (".", "!", "?", "\n", "。", "！", "？", "…")  # noqa: RUF001


class OpenAISSEParser:
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
        # Accumulate per-turn text and reasoning so the commentator receives
        # one coherent chunk per turn instead of sub-token fragments that get
        # filtered out as "too short" by importance.classify.
        self._text_buf: list[str] = []
        self._reasoning_buf: list[str] = []
        # Pending tool calls, keyed by the stream's own index/id. Chat
        # Completions streams `delta.tool_calls[].function.arguments` and the
        # Responses API streams `response.function_call_arguments.delta`; both
        # are only useful once complete, so we accumulate and flush at the end
        # of the turn.
        self._tool_calls: dict[str, dict[str, Any]] = {}
        # A response that ends by calling tools is not the end of the agent's
        # turn — the next request carries the results and continues it. Track
        # it so TURN_ENDED can say whether the agent is actually done.
        self._turn_called_tools = False

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
            except Exception:
                log.exception("failed to handle openai SSE event")

    async def close(self) -> None:
        tail = self._buf.strip()
        if tail:
            try:
                await self._handle_event_bytes(tail)
            except Exception:
                log.exception("failed to handle final openai SSE event")
        self._buf = b""
        await self._flush_turn()

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
                body = line[5:].lstrip()
                if body == "[DONE]":
                    await self._flush_turn()
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
        # Chat Completions: {choices: [{delta: {content: "...", reasoning: "..."}}]}
        # `reasoning` (and `reasoning_content`) appear on thinking-capable models
        # via Ollama / OpenRouter / DeepSeek — same shape, different field.
        choices = p.get("choices")
        if isinstance(choices, list) and choices:
            delta = (choices[0] or {}).get("delta") or {}
            text = delta.get("content")
            if text:
                self._text_buf.append(text)
            thinking = delta.get("reasoning") or delta.get("reasoning_content")
            if thinking:
                self._reasoning_buf.append(thinking)
            self._collect_chat_tool_calls(delta.get("tool_calls"))
            if (choices[0] or {}).get("finish_reason"):
                await self._flush_tool_calls()
            await self._try_incremental_flush()
            return

        # Responses API: event objects with .type and .delta/.item
        t = p.get("type")
        if t == "response.output_text.delta":
            text = p.get("delta") or ""
            if text:
                self._text_buf.append(text)
                await self._try_incremental_flush()
            return
        if t == "response.reasoning_summary_text.delta":
            text = p.get("delta") or ""
            if text:
                self._reasoning_buf.append(text)
                await self._try_incremental_flush()
            return
        if t == "response.created":
            self._turn_called_tools = False
            return
        if t == "response.output_item.added":
            item = p.get("item") or {}
            if item.get("type") in ("function_call", "custom_tool_call"):
                self._turn_called_tools = True
                key = str(item.get("id") or item.get("call_id") or p.get("output_index") or "0")
                self._tool_calls[key] = {
                    "id": item.get("call_id") or item.get("id"),
                    "name": item.get("name"),
                    "args": item.get("arguments") or "",
                }
            return
        if t in ("response.function_call_arguments.delta", "response.custom_tool_call_input.delta"):
            key = str(p.get("item_id") or p.get("output_index") or "0")
            entry = self._tool_calls.setdefault(key, {"id": None, "name": None, "args": ""})
            entry["args"] += str(p.get("delta") or p.get("input") or "")
            return
        if t in ("response.function_call_arguments.done", "response.output_item.done"):
            item = p.get("item") or {}
            key = str(
                p.get("item_id")
                or item.get("id")
                or item.get("call_id")
                or p.get("output_index")
                or "0"
            )
            pending = self._tool_calls.get(key)
            if pending is not None:
                if isinstance(p.get("arguments"), str) and p["arguments"]:
                    pending["args"] = p["arguments"]
                if not pending.get("name"):
                    pending["name"] = item.get("name")
                await self._flush_tool_calls(only=key)
            return
        if t == "response.completed":
            await self._flush_turn()
            output = (p.get("response") or {}).get("output")
            called_tools = self._turn_called_tools or (
                isinstance(output, list)
                and any(
                    isinstance(item, dict)
                    and item.get("type") in ("function_call", "custom_tool_call")
                    for item in output
                )
            )
            self._turn_called_tools = False
            await self._emit(
                EventKind.TURN_ENDED, {"via": "openai.proxy", "final": not called_tools}
            )
            return

    async def _try_incremental_flush(self) -> None:
        """Emit AGENT_THINKING / AGENT_TEXT mid-turn.

        Thinking: flush as soon as the buffer reaches the minimum size — no
        sentence-boundary requirement. The narration LLM will paraphrase the
        raw thinking into natural speech anyway, so mid-sentence cuts are fine.

        Text (final answer): prefer a sentence boundary so spoken sentences
        are complete; force-flush at 2x minimum if no boundary appears sooner.
        """
        if self._reasoning_buf:
            joined = "".join(self._reasoning_buf)
            if len(joined) >= _INCREMENTAL_MIN_CHARS:
                self._reasoning_buf = []
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

    def _collect_chat_tool_calls(self, tool_calls: Any) -> None:
        """Accumulate Chat Completions tool-call deltas, keyed by index."""
        if not isinstance(tool_calls, list):
            return
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            key = str(call.get("index", 0))
            entry = self._tool_calls.setdefault(key, {"id": None, "name": None, "args": ""})
            if call.get("id"):
                entry["id"] = call["id"]
            fn = call.get("function") or {}
            if fn.get("name"):
                entry["name"] = fn["name"]
            if isinstance(fn.get("arguments"), str):
                entry["args"] += fn["arguments"]

    async def _flush_tool_calls(self, only: str | None = None) -> None:
        keys = [only] if only is not None else list(self._tool_calls)
        for key in keys:
            entry = self._tool_calls.pop(key, None)
            if not entry or not entry.get("name"):
                continue
            await publish_tool_call(
                self._bus,
                source="openai.proxy",
                session_id=self._session_id,
                tool=entry.get("name"),
                tool_input=decode_tool_input(entry.get("args")),
                tool_use_id=entry.get("id"),
            )

    async def _flush_turn(self) -> None:
        """Emit accumulated text/reasoning as single events, then reset."""
        await self._flush_tool_calls()
        if self._reasoning_buf:
            full = "".join(self._reasoning_buf).strip()
            self._reasoning_buf = []
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
        if self._internal:
            payload = {**payload, "internal": True}
        if self._subagent:
            payload = {**payload, "subagent": True}
        await self._bus.publish(Event(kind=kind, source="openai.proxy", payload=payload))
