"""Tool calls must reach the bus with their arguments, not just a name.

Anthropic streams `tool_use` input as `input_json_delta` after the block
starts, so emitting at block-start yields `input: {}` — the commentator then
has no file, command or pattern to talk about.
"""

import json

import pytest

from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import EventKind
from voice_copilot.proxy.anthropic import AnthropicSSEParser
from voice_copilot.proxy.openai import OpenAISSEParser
from voice_copilot.proxy.tool_events import decode_tool_input, file_path_from_tool


def _sse(*objs: dict) -> bytes:
    return "".join(f"event: x\ndata: {json.dumps(o)}\n\n" for o in objs).encode()


async def _drain(bus: EventBus, feed) -> list[tuple[EventKind, dict]]:
    async with bus.subscribe() as q:
        await feed()
        out = []
        while not q.empty():
            ev = q.get_nowait()
            out.append((ev.kind, ev.payload))
        return out


def _tool_block(index: int, tool_id: str, name: str, chunks: list[str]) -> list[dict]:
    events: list[dict] = [
        {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}},
        }
    ]
    events += [
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "input_json_delta", "partial_json": chunk},
        }
        for chunk in chunks
    ]
    events.append({"type": "content_block_stop", "index": index})
    return events


@pytest.mark.asyncio
async def test_anthropic_tool_call_carries_streamed_input() -> None:
    bus = EventBus()
    blob = json.dumps({"command": "pytest -q", "description": "run tests"})
    parser = AnthropicSSEParser(bus, session_id="s1")

    async def feed() -> None:
        await parser.feed(_sse(*_tool_block(0, "tu_1", "Bash", [blob[:9], blob[9:]])))
        await parser.feed(_sse({"type": "message_stop"}))

    events = await _drain(bus, feed)
    tools = [p for k, p in events if k is EventKind.TOOL_CALL_STARTED]
    assert len(tools) == 1
    assert tools[0]["tool"] == "Bash"
    assert tools[0]["input"] == {"command": "pytest -q", "description": "run tests"}
    assert tools[0]["session_id"] == "s1"


@pytest.mark.asyncio
async def test_anthropic_edit_tool_derives_file_edited() -> None:
    bus = EventBus()
    blob = json.dumps({"file_path": "src/app.py", "old_string": "a", "new_string": "b"})
    parser = AnthropicSSEParser(bus, session_id="s1")

    async def feed() -> None:
        await parser.feed(_sse(*_tool_block(0, "tu_1", "Edit", [blob])))

    events = await _drain(bus, feed)
    edited = [p for k, p in events if k is EventKind.FILE_EDITED]
    # The proxy never sees tool results, so an edit is only visible through
    # the call that requests it.
    assert edited == [{"path": "src/app.py", "via": "tool_call", "session_id": "s1"}]


@pytest.mark.asyncio
async def test_anthropic_flushes_tool_block_left_open_by_a_cut_stream() -> None:
    bus = EventBus()
    parser = AnthropicSSEParser(bus, session_id="s1")

    async def feed() -> None:
        await parser.feed(
            _sse(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "Read",
                        "input": {},
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '{"file_path":"a.py"}'},
                },
            )
        )
        await parser.close()

    events = await _drain(bus, feed)
    tools = [p for k, p in events if k is EventKind.TOOL_CALL_STARTED]
    assert tools and tools[0]["input"] == {"file_path": "a.py"}


@pytest.mark.asyncio
async def test_openai_chat_completions_tool_call_is_accumulated() -> None:
    bus = EventBus()
    parser = OpenAISSEParser(bus, session_id="s2")

    async def feed() -> None:
        await parser.feed(
            _sse(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "function": {"name": "shell", "arguments": '{"cmd":'},
                                    }
                                ]
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {"index": 0, "function": {"arguments": '"ruff check"}'}}
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
            )
        )

    events = await _drain(bus, feed)
    tools = [p for k, p in events if k is EventKind.TOOL_CALL_STARTED]
    assert tools == [
        {
            "tool_use_id": "call_1",
            "tool": "shell",
            "input": {"cmd": "ruff check"},
            "session_id": "s2",
        }
    ]


@pytest.mark.asyncio
async def test_openai_responses_api_function_call() -> None:
    bus = EventBus()
    parser = OpenAISSEParser(bus, session_id="s2")

    async def feed() -> None:
        await parser.feed(
            _sse(
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {"type": "function_call", "id": "fc_1", "name": "apply_patch"},
                },
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "fc_1",
                    "delta": '{"path":"src/app.py"}',
                },
                {"type": "response.function_call_arguments.done", "item_id": "fc_1"},
            )
        )

    events = await _drain(bus, feed)
    kinds = [k for k, _ in events]
    assert EventKind.TOOL_CALL_STARTED in kinds
    # apply_patch writes a file, so the edit surfaces too.
    assert EventKind.FILE_EDITED in kinds


def test_file_path_only_for_editing_tools() -> None:
    assert file_path_from_tool("Write", {"file_path": "a.py"}) == "a.py"
    assert file_path_from_tool("NotebookEdit", {"notebook_path": "nb.ipynb"}) == "nb.ipynb"
    # Reading a file is not editing it.
    assert file_path_from_tool("Read", {"file_path": "a.py"}) is None
    assert file_path_from_tool("Edit", {"old_string": "x"}) is None
    assert file_path_from_tool(None, {"file_path": "a.py"}) is None


def test_decode_tool_input_keeps_a_truncated_blob() -> None:
    assert decode_tool_input('{"a": 1}') == {"a": 1}
    assert decode_tool_input('{"file_path":"a.py"') == '{"file_path":"a.py"'
    assert decode_tool_input("") is None
    assert decode_tool_input(None) is None
