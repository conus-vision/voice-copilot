"""A model response that ends in tool calls is a step, not the end of the turn.

Chat Completions (OpenCode, Aider, Crush, Qwen, ...) ends every tool round
with `[DONE]`, and Ollama's native API with `done: true`. Tagged as a plain
turn end, each round read as "the agent finished": the narrator wrapped up
mid-task and the supervisor ran its end-of-turn review after every tool call.
"""

from __future__ import annotations

import json

from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import EventKind
from voice_copilot.proxy.ollama_native import OllamaNativeParser
from voice_copilot.proxy.openai import OpenAISSEParser


def _sse(*objs: dict) -> bytes:
    return "".join(f"data: {json.dumps(o)}\n\n" for o in objs).encode() + b"data: [DONE]\n\n"


async def _events(bus: EventBus, parser, chunk: bytes) -> list[tuple[EventKind, dict]]:
    async with bus.subscribe() as q:
        await parser.feed(chunk)
        await parser.close()
        out = []
        while not q.empty():
            ev = q.get_nowait()
            out.append((ev.kind, ev.payload))
        return out


def _turn_ends(events: list[tuple[EventKind, dict]]) -> list[dict]:
    return [p for k, p in events if k is EventKind.TURN_ENDED]


async def test_chat_completions_tool_round_is_not_final() -> None:
    bus = EventBus()
    call = {
        "index": 0,
        "id": "c1",
        "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
    }
    chunk = _sse(
        {"choices": [{"delta": {"content": "Let me look."}}]},
        {"choices": [{"delta": {"tool_calls": [call]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    )
    events = await _events(bus, OpenAISSEParser(bus, session_id="s1"), chunk)
    assert [p["final"] for p in _turn_ends(events)] == [False]
    tools = [p for k, p in events if k is EventKind.TOOL_CALL_STARTED]
    assert tools and tools[0]["input"] == {"path": "a.py"}


async def test_chat_completions_plain_answer_is_final() -> None:
    bus = EventBus()
    chunk = _sse(
        {"choices": [{"delta": {"content": "All done, tests pass."}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    )
    events = await _events(bus, OpenAISSEParser(bus, session_id="s1"), chunk)
    assert [p["final"] for p in _turn_ends(events)] == [True]


def _ndjson(*objs: dict) -> bytes:
    return "".join(json.dumps(o) + "\n" for o in objs).encode()


async def test_ollama_native_tool_calls_are_published_and_not_final() -> None:
    bus = EventBus()
    call = {"function": {"name": "write_file", "arguments": {"path": "b.py", "content": "x"}}}
    chunk = _ndjson(
        {"message": {"role": "assistant", "content": "", "tool_calls": [call]}, "done": False},
        {"message": {"role": "assistant", "content": ""}, "done": True},
    )
    events = await _events(bus, OllamaNativeParser(bus, session_id="s2"), chunk)
    assert [p["final"] for p in _turn_ends(events)] == [False]
    kinds = [k for k, _ in events]
    assert EventKind.TOOL_CALL_STARTED in kinds
    assert (
        EventKind.FILE_EDITED,
        {"path": "b.py", "via": "tool_call", "session_id": "s2"},
    ) in events


async def test_ollama_native_plain_answer_is_final() -> None:
    bus = EventBus()
    chunk = _ndjson({"message": {"role": "assistant", "content": "Hello."}, "done": True})
    events = await _events(bus, OllamaNativeParser(bus, session_id="s2"), chunk)
    assert [p["final"] for p in _turn_ends(events)] == [True]
