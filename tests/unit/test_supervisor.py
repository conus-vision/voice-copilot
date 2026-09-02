"""The supervisor: a stronger model that reviews the agent at checkpoints.

Also covers the prerequisite that made it possible — TURN_ENDED now says
whether the agent is really done, so neither narrator nor supervisor announces
completion after every tool round.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest

from voice_copilot.adapters.base import CLIAdapter, QuickAsideCapability
from voice_copilot.commentator.format import _format_one
from voice_copilot.commentator.pipeline import (
    Commentator,
    _Batch,
    _SessionContext,
    parse_supervisor_verdict,
)
from voice_copilot.core.bus import EventBus
from voice_copilot.core.config import CommentatorConfig, DialogConfig, SupervisorConfig
from voice_copilot.core.events import Event, EventKind
from voice_copilot.dialog.manager import DialogManager
from voice_copilot.proxy.anthropic import AnthropicSSEParser
from voice_copilot.proxy.openai import OpenAISSEParser

# --- verdict parsing -----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "status", "message"),
    [
        ("OK", "ok", ""),
        ("ok\n", "ok", ""),
        (
            "WARN\nTests were not run after the edits.",
            "warn",
            "Tests were not run after the edits.",
        ),
        ("**STOP**\nEditing files outside the task.", "stop", "Editing files outside the task."),
        ("STOP: the agent is deleting a branch.", "stop", "the agent is deleting a branch."),
        ("All good, carrying on.", "ok", ""),  # no verdict word → never stops the agent
        ("", "ok", ""),
    ],
)
def test_parse_verdict(raw: str, status: str, message: str) -> None:
    assert parse_supervisor_verdict(raw) == (status, message)


# --- turn-ended final flag -----------------------------------------------


async def _collect(bus: EventBus, feed) -> list[Event]:  # type: ignore[no-untyped-def]
    async with bus.subscribe() as q:
        await feed()
        out = []
        while not q.empty():
            out.append(q.get_nowait())
        return out


def _sse(*objs: dict) -> bytes:  # type: ignore[type-arg]
    return "".join(f"event: x\ndata: {json.dumps(o)}\n\n" for o in objs).encode()


@pytest.mark.asyncio
async def test_openai_turn_end_is_not_final_when_tools_were_called() -> None:
    bus = EventBus()
    parser = OpenAISSEParser(bus, session_id="s")
    call = {"type": "function_call", "id": "c1", "name": "exec"}
    events = await _collect(
        bus,
        lambda: parser.feed(
            _sse(
                {"type": "response.created"},
                {"type": "response.output_item.added", "item": call},
                {"type": "response.output_item.done", "item": {**call, "arguments": "{}"}},
                {"type": "response.completed", "response": {"output": []}},
                {"type": "response.created"},
                {"type": "response.output_text.delta", "delta": "Done."},
                {"type": "response.completed", "response": {"output": [{"type": "message"}]}},
            )
        ),
    )
    ends = [e.payload["final"] for e in events if e.kind is EventKind.TURN_ENDED]
    assert ends == [False, True]


@pytest.mark.asyncio
async def test_anthropic_turn_end_reads_stop_reason() -> None:
    bus = EventBus()
    parser = AnthropicSSEParser(bus, session_id="s")
    events = await _collect(
        bus,
        lambda: parser.feed(
            _sse(
                {"type": "message_start", "message": {}},
                {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
                {"type": "message_stop"},
                {"type": "message_start", "message": {}},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                {"type": "message_stop"},
            )
        ),
    )
    ends = [e.payload["final"] for e in events if e.kind is EventKind.TURN_ENDED]
    assert ends == [False, True]


def test_intermediate_turn_end_is_worded_as_a_step() -> None:
    step = Event(kind=EventKind.TURN_ENDED, source="x", payload={"final": False})
    done = Event(kind=EventKind.TURN_ENDED, source="x", payload={"final": True})
    legacy = Event(kind=EventKind.TURN_ENDED, source="x", payload={})
    assert "turn ended" not in _format_one(step)
    assert _format_one(done) == "turn ended"
    assert _format_one(legacy) == "turn ended"  # adapters without the flag


# --- checkpoints -----------------------------------------------------------


class _ScriptedLLM:
    prompt_style = "api"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.systems: list[str] = []

    def stream_chat(self, messages, *, system=None, max_tokens=None, temperature=None):  # type: ignore[no-untyped-def]
        self.systems.append(system or "")

        async def gen() -> AsyncIterator[str]:
            yield self.reply

        return gen()


def _turn_end(final: bool) -> Event:
    return Event(
        kind=EventKind.TURN_ENDED,
        source="openai.proxy",
        payload={"final": final, "session_id": "s"},
    )


def _tool(tool: str = "exec") -> Event:
    return Event(
        kind=EventKind.TOOL_CALL_STARTED,
        source="openai.proxy",
        payload={"tool": tool, "input": {}, "session_id": "s"},
    )


def _failed(tool: str) -> Event:
    return Event(
        kind=EventKind.TOOL_CALL_FINISHED,
        source="openai.proxy",
        payload={"tool": tool, "is_error": True, "preview": "boom", "session_id": "s"},
    )


def _commentator(mode: str, llm: _ScriptedLLM) -> Commentator:
    cfg = CommentatorConfig(supervisor=SupervisorConfig(mode=mode, every_n_tools=3))  # type: ignore[arg-type]
    c = Commentator(EventBus(), cfg, "en", llm=llm)  # type: ignore[arg-type]
    # `_supervisor()` would otherwise build the real provider for the strong model.
    c._supervisor = lambda: llm  # type: ignore[method-assign, assignment, return-value]
    return c


def test_off_never_checkpoints() -> None:
    c = _commentator("off", _ScriptedLLM("STOP\nnope"))
    ctx = _SessionContext()
    batch = _Batch(events=[_turn_end(True)], session_key="s")
    c._record_for_supervisor(batch)
    assert c._checkpoint_reason(batch, ctx) is None


def test_only_a_final_turn_end_is_a_checkpoint() -> None:
    c = _commentator("watch", _ScriptedLLM("OK"))
    ctx = c._contexts.setdefault("s", _SessionContext())
    step = _Batch(events=[_turn_end(False)], session_key="s")
    c._record_for_supervisor(step)
    assert c._checkpoint_reason(step, ctx) is None
    done = _Batch(events=[_turn_end(True)], session_key="s")
    c._record_for_supervisor(done)
    assert c._checkpoint_reason(done, ctx) == "turn ended"


def test_a_run_of_tool_calls_and_a_repeated_failure_are_checkpoints() -> None:
    c = _commentator("watch", _ScriptedLLM("OK"))
    ctx = c._contexts.setdefault("s", _SessionContext())
    calls = _Batch(events=[_tool(), _tool(), _tool()], session_key="s")
    c._record_for_supervisor(calls)
    assert "3 tool calls" in (c._checkpoint_reason(calls, ctx) or "")
    ctx.tools_since_review = 0
    fails = _Batch(events=[_failed("pytest"), _failed("pytest")], session_key="s")
    c._record_for_supervisor(fails)
    assert "pytest failed 2 times" in (c._checkpoint_reason(fails, ctx) or "")


async def _drain(c: Commentator, batch: _Batch) -> dict[EventKind, dict]:  # type: ignore[type-arg]
    async with c._bus.subscribe() as q:
        await c._supervise(batch, "turn ended")
        out: dict[EventKind, dict] = {}  # type: ignore[type-arg]
        while not q.empty():
            ev = q.get_nowait()
            out[ev.kind] = ev.payload
        return out


@pytest.mark.asyncio
async def test_stop_in_guard_mode_pauses_and_speaks() -> None:
    llm = _ScriptedLLM("STOP\nThe agent is editing files outside the task.")
    c = _commentator("guard", llm)
    ctx = c._contexts.setdefault("s", _SessionContext())
    ctx.history = ["file edited: unrelated.py"]
    batch = _Batch(events=[_turn_end(True)], session_key="s", user_query="fix the login bug")
    got = await _drain(c, batch)
    assert got[EventKind.SUPERVISOR_VERDICT]["status"] == "stop"
    assert got[EventKind.SUPERVISOR_VERDICT]["paused_agent"] is True
    assert EventKind.SUPERVISOR_STOP in got
    spoken = got[EventKind.COMMENTATOR_UTTERANCE]
    assert spoken["role"] == "supervisor"
    assert "outside the task" in spoken["text"]
    assert llm.systems and "OK, WARN or STOP" in llm.systems[0]


@pytest.mark.asyncio
async def test_stop_in_watch_mode_only_warns() -> None:
    c = _commentator("watch", _ScriptedLLM("STOP\nLooping without progress."))
    got = await _drain(c, _Batch(events=[_turn_end(True)], session_key="s"))
    assert EventKind.SUPERVISOR_STOP not in got
    assert EventKind.COMMENTATOR_UTTERANCE in got
    assert got[EventKind.SUPERVISOR_VERDICT]["paused_agent"] is False


@pytest.mark.asyncio
async def test_ok_is_trace_only() -> None:
    c = _commentator("guard", _ScriptedLLM("OK"))
    got = await _drain(c, _Batch(events=[_turn_end(True)], session_key="s"))
    assert list(got) == [EventKind.SUPERVISOR_VERDICT]


# --- the dialog manager honours the stop ----------------------------------


class _FakeAdapter(CLIAdapter):
    name = "fake"
    quick_aside = QuickAsideCapability.QUEUE

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._paused = False

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def send_user_message(self, text: str, *, urgent: bool = False) -> bool:
        return True

    async def pause(self) -> bool:
        self.calls.append("pause")
        self._paused = True
        return True

    async def resume(self) -> bool:
        self.calls.append("resume")
        self._paused = False
        return True

    @property
    def is_paused(self) -> bool:
        return self._paused


@pytest.mark.asyncio
async def test_supervisor_stop_pauses_until_the_user_resumes() -> None:
    bus, adapter = EventBus(), _FakeAdapter()
    dialog = DialogManager(bus, adapter, DialogConfig())
    task = asyncio.create_task(dialog.run())
    await asyncio.sleep(0.02)
    await bus.publish(
        Event(kind=EventKind.SUPERVISOR_STOP, source="commentator.supervisor", payload={})
    )
    await asyncio.sleep(0.02)
    assert adapter.is_paused
    # a playback report must NOT release it — this is not the narration hold
    await bus.publish(Event(kind=EventKind.PLAYBACK_READY, source="web", payload={}))
    await asyncio.sleep(0.02)
    assert adapter.is_paused
    await bus.publish(Event(kind=EventKind.USER_PAUSE_TOGGLE, source="web", payload={}))
    await asyncio.sleep(0.02)
    task.cancel()
    assert adapter.calls == ["pause", "resume"]


# --- checkpoint hygiene ------------------------------------------------------


def _subagent_turn_end() -> Event:
    return Event(
        kind=EventKind.TURN_ENDED,
        source="openai.proxy",
        payload={"final": True, "subagent": True, "session_id": "s"},
    )


def test_a_sub_agent_finishing_is_not_a_checkpoint() -> None:
    c = _commentator("watch", _ScriptedLLM("OK"))
    ctx = c._contexts.setdefault("s", _SessionContext())
    batch = _Batch(events=[_subagent_turn_end()], session_key="s")
    c._record_for_supervisor(batch)
    assert c._checkpoint_reason(batch, ctx) is None


@pytest.mark.asyncio
async def test_the_same_complaint_is_not_spoken_twice() -> None:
    c = _commentator("watch", _ScriptedLLM("WARN\nTests were not run after the edits."))
    batch = _Batch(events=[_turn_end(True)], session_key="s")
    first = await _drain(c, batch)
    assert EventKind.COMMENTATOR_UTTERANCE in first
    second = await _drain(c, batch)
    assert second[EventKind.SUPERVISOR_VERDICT]["repeat"] is True
    assert EventKind.COMMENTATOR_UTTERANCE not in second


@pytest.mark.asyncio
async def test_a_repeated_stop_in_guard_mode_still_pauses() -> None:
    c = _commentator("guard", _ScriptedLLM("STOP\nEditing files outside the task."))
    batch = _Batch(events=[_turn_end(True)], session_key="s")
    await _drain(c, batch)
    again = await _drain(c, batch)
    assert EventKind.SUPERVISOR_STOP in again  # safety beats de-duplication
