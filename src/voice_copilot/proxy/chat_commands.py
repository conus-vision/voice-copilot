"""`vc ...` words for a CLI narrated through the proxy, which has no plugin to catch them.

The proxy sees the prompt on its way to the model, so it can answer in the model's place:
nothing goes upstream, nothing is spent, and the CLI shows the reply as any other. Each
provider's wire format gets a reply of its own, streamed if the request asked for that.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Iterable
from typing import Any

from fastapi.responses import Response, StreamingResponse

from voice_copilot.companion.commands import Command, say
from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import Event, EventKind

_SSE = "text/event-stream"
#: A CLI may send the same prompt twice, with a side request for a title or on a retry.
#: The command runs once, and both get its answer.
_SAME_COMMAND_S = 5.0
_NOT_SAID = ("system", "developer")


class ChatCommands:
    """The `vc` words as a wrapped CLI's proxy hears them. There is one session to mean here."""

    def __init__(self, *, bus: EventBus, panel_url: str, language: Callable[[], str]) -> None:
        self._bus = bus
        self._panel_url = panel_url
        self._language = language
        self.center: Any = None  # Set once the adapter it works with exists.
        self.narration_off = False
        self._last: tuple[Command, float, str] | None = None

    async def answer(self, command: Command) -> str:
        now = time.monotonic()
        if self._last and self._last[0] == command and now - self._last[1] < _SAME_COMMAND_S:
            return self._last[2]
        text = await self._run(command)
        self._last = (command, now, text)
        return text

    async def _run(self, command: Command) -> str:
        lang = self._language()
        if command.verb == "help":
            return say("menu", lang)
        if command.verb == "off":
            self.narration_off = True
            await self._bus.publish(Event(kind=EventKind.NARRATION_STOP, source="proxy"))
            return say("off_all", lang)
        if command.verb == "on":
            self.narration_off = False
            return say("on_all", lang, url=self._panel_url)
        if command.verb == "status":
            if self.narration_off:
                return say("status_off", lang)
            return say("status_on", lang, url=self._panel_url)
        if self.narration_off:
            return say("status_off", lang)
        answer: dict[str, Any] = await self.center.run(command.verb, list(command.args))
        return str(answer.get("text"))


def ends_with_the_users_words(doc: dict[str, Any]) -> bool:
    """Whether the newest thing in this request is what the user just typed.

    The newest *user text* isn't enough. After a tool ran, the request ends in the tool's
    result, and the prompt further back would be taken for a new one on every step.
    """
    if isinstance(doc.get("contents"), list) and doc["contents"]:  # Gemini
        last = doc["contents"][-1]
        role = last.get("role", "user") if isinstance(last, dict) else None
        parts = last.get("parts") if isinstance(last, dict) else None
        return role == "user" and isinstance(parts, list) and all("text" in p for p in parts)
    items = doc.get("messages") if isinstance(doc.get("messages"), list) else doc.get("input")
    if isinstance(items, str):
        return True
    if not isinstance(items, list) or not items:
        return bool(doc.get("prompt"))  # Ollama's /api/generate
    # Claude Code puts a system-role block about its environment after the prompt.
    said = [i for i in items if not (isinstance(i, dict) and i.get("role") in _NOT_SAID)]
    last = said[-1] if said else None
    if not isinstance(last, dict) or last.get("role") != "user":
        return False
    content = last.get("content")
    if isinstance(content, str):
        return True
    return isinstance(content, list) and not any(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in content
    )


def _sse(rows: Iterable[tuple[str | None, dict[str, Any] | str]]) -> Response:
    def lines() -> Iterable[bytes]:
        for event, data in rows:
            body = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
            head = f"event: {event}\n" if event else ""
            yield f"{head}data: {body}\n\n".encode()

    return StreamingResponse(iter(list(lines())), media_type=_SSE)


def _json(doc: dict[str, Any]) -> Response:
    return Response(json.dumps(doc, ensure_ascii=False), media_type="application/json")


def _anthropic(doc: dict[str, Any], text: str) -> Response:
    message = {
        "id": f"msg_vc_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": doc.get("model", "voice-copilot"),
        "stop_sequence": None,
    }
    usage = {"input_tokens": 0, "output_tokens": 0}
    if not doc.get("stream"):
        return _json(
            {
                **message,
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
                "usage": usage,
            }
        )
    block_end = {"type": "content_block_stop", "index": 0}
    return _sse(
        [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {**message, "content": [], "stop_reason": None, "usage": usage},
                },
            ),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                },
            ),
            ("content_block_stop", block_end),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 0},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
    )


def _chat_completions(doc: dict[str, Any], text: str) -> Response:
    base = {
        "id": f"chatcmpl-vc-{uuid.uuid4().hex}",
        "created": int(time.time()),
        "model": doc.get("model", "voice-copilot"),
    }
    if not doc.get("stream"):
        return _json(
            {
                **base,
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
        )
    chunk = {**base, "object": "chat.completion.chunk"}
    say = {"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}
    end = {"index": 0, "delta": {}, "finish_reason": "stop"}
    return _sse(
        [
            (None, {**chunk, "choices": [say]}),
            (None, {**chunk, "choices": [end]}),
            (None, "[DONE]"),
        ]
    )


def _responses(doc: dict[str, Any], text: str) -> Response:
    response_id = f"resp_vc_{uuid.uuid4().hex}"
    item_id = f"msg_vc_{uuid.uuid4().hex}"
    part = {"type": "output_text", "text": text, "annotations": []}
    item = {
        "id": item_id,
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [part],
    }
    base = {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "model": doc.get("model", "voice-copilot"),
    }
    done = {
        **base,
        "status": "completed",
        "output": [item],
        "usage": {
            "input_tokens": 0,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 0,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 0,
        },
    }
    if not doc.get("stream"):
        return _json(done)
    where = {"item_id": item_id, "output_index": 0, "content_index": 0}
    rows: list[tuple[str, dict[str, Any]]] = [
        (
            "response.created",
            {"response": {**base, "status": "in_progress", "output": []}},
        ),
        (
            "response.output_item.added",
            {"output_index": 0, "item": {**item, "status": "in_progress", "content": []}},
        ),
        ("response.content_part.added", {**where, "part": {**part, "text": ""}}),
        ("response.output_text.delta", {**where, "delta": text}),
        ("response.output_text.done", {**where, "text": text}),
        ("response.content_part.done", {**where, "part": part}),
        ("response.output_item.done", {"output_index": 0, "item": item}),
        ("response.completed", {"response": done}),
    ]
    return _sse(
        (event, {"type": event, "sequence_number": n, **data})
        for n, (event, data) in enumerate(rows)
    )


def _ollama_native(doc: dict[str, Any], path: str, text: str) -> Response:
    base = {"model": doc.get("model", "voice-copilot"), "done": True, "done_reason": "stop"}
    shaped = (
        {"response": text}
        if path.startswith("api/generate")
        else {"message": {"role": "assistant", "content": text}}
    )
    return Response(
        json.dumps({**base, **shaped}, ensure_ascii=False) + "\n",
        media_type="application/x-ndjson" if doc.get("stream", True) else "application/json",
    )


def _gemini(path: str, text: str) -> Response:
    reply = {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": text}]},
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "usageMetadata": {"promptTokenCount": 0, "candidatesTokenCount": 0, "totalTokenCount": 0},
    }
    return _sse([(None, reply)]) if "streamGenerate" in path else _json(reply)


def is_a_model_call(provider: str, path: str) -> bool:
    """Only these carry a prompt we can answer. Token counts, model lists and so on pass."""
    clean = path.lstrip("/")
    if provider == "gemini":
        return "generateContent" in clean or "GenerateContent" in clean
    if clean.startswith("api/"):
        return clean.startswith(("api/chat", "api/generate"))
    return clean.endswith(("messages", "chat/completions", "responses"))


def synthetic_reply(provider: str, path: str, doc: dict[str, Any], text: str) -> Response:
    """The model's reply, in this provider's format, saying `text`."""
    clean = path.lstrip("/")
    if provider == "gemini":
        return _gemini(clean, text)
    if clean.startswith("api/"):
        return _ollama_native(doc, clean, text)
    if clean.endswith("messages"):
        return _anthropic(doc, text)
    if clean.endswith("responses"):
        return _responses(doc, text)
    return _chat_completions(doc, text)
