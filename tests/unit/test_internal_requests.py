"""A CLI's own side requests must not read as the agent's turn.

Codex asks the model for a session title over the same session right after the
user's question. Its reply (`{"title": ...}`) and its turn end used to be
narrated as "the agent answered and finished" — and, with Supervisor+ on,
triggered a STOP for "finishing without doing the work".
"""

from __future__ import annotations

import json

import pytest

from voice_copilot.commentator.importance import classify
from voice_copilot.core.bus import EventBus
from voice_copilot.core.config import CommentatorConfig
from voice_copilot.core.events import Event, EventKind
from voice_copilot.core.user_query import clean_user_query
from voice_copilot.proxy.openai import OpenAISSEParser
from voice_copilot.proxy.server import _make_parser_factory

TITLE_PROMPT = (
    "Generate a concise, single-line task title of at most 36 characters. "
    "Do not answer the request. User prompt: assess the project"
)


def _sse(*objs: dict) -> bytes:  # type: ignore[type-arg]
    return "".join(f"event: x\ndata: {json.dumps(o)}\n\n" for o in objs).encode()


def test_the_title_prompt_is_recognised_as_the_cli_talking_to_itself() -> None:
    assert clean_user_query(TITLE_PROMPT) is None
    assert clean_user_query("assess the project") == "assess the project"


@pytest.mark.asyncio
async def test_an_internal_parser_tags_everything_it_emits() -> None:
    bus = EventBus()
    factory = _make_parser_factory(bus, "openai", "s", internal=True)
    assert factory is not None
    parser = factory()
    assert isinstance(parser, OpenAISSEParser)
    async with bus.subscribe() as q:
        await parser.feed(
            _sse(
                {"type": "response.created"},
                {"type": "response.output_text.delta", "delta": '{"title":"Assess project"}'},
                {"type": "response.completed", "response": {"output": [{"type": "message"}]}},
            )
        )
        events = [q.get_nowait() for _ in range(q.qsize())]
    kinds = {e.kind for e in events}
    assert EventKind.AGENT_TEXT in kinds and EventKind.TURN_ENDED in kinds
    assert all(e.payload.get("internal") is True for e in events)


def test_internal_events_never_reach_the_narrator_or_supervisor() -> None:
    cfg = CommentatorConfig()
    reply = Event(
        kind=EventKind.AGENT_TEXT,
        source="openai.proxy",
        payload={"text": '{"title":"Assess project"}', "internal": True},
    )
    done = Event(
        kind=EventKind.TURN_ENDED,
        source="openai.proxy",
        payload={"final": True, "internal": True},
    )
    assert classify(reply, cfg) is None
    assert classify(done, cfg) is None
    # the same events without the tag are still the agent's
    assert classify(reply.model_copy(update={"payload": {"text": "done"}}), cfg) == "normal"


# --- sub-agents ------------------------------------------------------------

from voice_copilot.commentator.format import _format_one  # noqa: E402
from voice_copilot.proxy.server import _is_subagent_request  # noqa: E402


def test_codex_turn_metadata_identifies_sub_agents() -> None:
    main = {"x-codex-turn-metadata": json.dumps({"agent_name": "/root", "parent_thread_id": ""})}
    fork = {
        "x-codex-turn-metadata": json.dumps(
            {"agent_name": "/root/architecture", "parent_thread_id": "t1"}
        )
    }
    assert _is_subagent_request(main) is False
    assert _is_subagent_request(fork) is True
    assert _is_subagent_request({}) is False
    assert _is_subagent_request({"x-codex-turn-metadata": "not json"}) is False


@pytest.mark.asyncio
async def test_a_sub_agent_parser_tags_its_turn_end() -> None:
    bus = EventBus()
    parser = _make_parser_factory(bus, "openai", "s", subagent=True)()  # type: ignore[misc]
    async with bus.subscribe() as q:
        await parser.feed(
            _sse(
                {"type": "response.created"},
                {"type": "response.output_text.delta", "delta": "done"},
                {"type": "response.completed", "response": {"output": [{"type": "message"}]}},
            )
        )
        events = [q.get_nowait() for _ in range(q.qsize())]
    end = next(e for e in events if e.kind is EventKind.TURN_ENDED)
    assert end.payload["subagent"] is True and end.payload["final"] is True
    assert _format_one(end) == "sub-agent finished (main task continues)"


def test_codex_plugin_boilerplate_is_not_a_question() -> None:
    # A forked sub-agent request carries this as its last *user* item.
    block = "<recommended_plugins>\nHere is a list of plugins that are available but not installed.\n- A\n</recommended_plugins>"
    assert clean_user_query(block) is None
    assert clean_user_query(block + "\n\nfix the failing tests") == "fix the failing tests"
