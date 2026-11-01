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
from voice_copilot.commentator.format import _format_one, build_supervisor_user, speech_text
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


def _failed(tool: str, preview: str = "boom") -> Event:
    return Event(
        kind=EventKind.TOOL_CALL_FINISHED,
        source="openai.proxy",
        payload={"tool": tool, "is_error": True, "preview": preview, "session_id": "s"},
    )


def _succeeded(tool: str) -> Event:
    return Event(
        kind=EventKind.TOOL_CALL_FINISHED,
        source="openai.proxy",
        payload={"tool": tool, "is_error": False, "preview": "fine", "session_id": "s"},
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
    assert c._checkpoint_reason(fails, ctx) == "the same failure came up 2 times: pytest: boom"


def test_different_failures_of_one_tool_are_not_a_repeat() -> None:
    # Every shell command reports the tool "Bash", so the tool alone says nothing.
    c = _commentator("watch", _ScriptedLLM("OK"))
    ctx = c._contexts.setdefault("s", _SessionContext())
    batch = _Batch(
        events=[
            _failed("Bash", "fatal: detected dubious ownership"),
            _failed("Bash", "No module named freezegun"),
            _failed("Bash", "NameError: name 'dt' is not defined"),
        ],
        session_key="s",
    )
    c._record_for_supervisor(batch)
    assert c._checkpoint_reason(batch, ctx) is None


def test_a_failure_returning_after_a_fix_attempt_is_a_repeat() -> None:
    # The usual loop: the edit between the two runs succeeds, the error stays.
    c = _commentator("watch", _ScriptedLLM("OK"))
    ctx = c._contexts.setdefault("s", _SessionContext())
    batch = _Batch(
        events=[
            _failed("Bash", "E0432 unresolved import at line 12"),
            _succeeded("apply_patch"),
            _failed("Bash", "E0432 unresolved import at line 14"),
        ],
        session_key="s",
    )
    c._record_for_supervisor(batch)
    assert "2 times" in (c._checkpoint_reason(batch, ctx) or "")


def test_a_repeat_is_reviewed_once_per_recurrence() -> None:
    # It used to fire at every batch while the streak stood: one review after another.
    c = _commentator("watch", _ScriptedLLM("OK"))
    ctx = c._contexts.setdefault("s", _SessionContext())
    fails = _Batch(events=[_failed("Bash"), _failed("Bash")], session_key="s")
    c._record_for_supervisor(fails)
    assert c._checkpoint_reason(fails, ctx) is not None
    quiet = _Batch(events=[_tool()], session_key="s")
    c._record_for_supervisor(quiet)
    assert c._checkpoint_reason(quiet, ctx) is None
    again = _Batch(events=[_failed("Bash")], session_key="s")
    c._record_for_supervisor(again)
    assert "3 times" in (c._checkpoint_reason(again, ctx) or "")


class _FailingLLM(_ScriptedLLM):
    def stream_chat(self, messages, *, system=None, max_tokens=None, temperature=None):  # type: ignore[no-untyped-def]
        async def gen() -> AsyncIterator[str]:
            await asyncio.sleep(10)
            yield ""

        return gen()


class _EmptyLLM(_ScriptedLLM):
    def stream_chat(self, messages, *, system=None, max_tokens=None, temperature=None):  # type: ignore[no-untyped-def]
        async def gen() -> AsyncIterator[str]:
            yield ""

        return gen()


async def _narration_errors(c: Commentator) -> list[str]:
    async with c._bus.subscribe() as q:
        await c._narrate(_Batch(events=[_tool()], session_key="s"))
        errors = []
        while not q.empty():
            ev = q.get_nowait()
            if ev.kind is EventKind.ERROR:
                errors.append(ev.payload["message"])
        return errors


@pytest.mark.asyncio
async def test_a_model_that_never_answers_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("voice_copilot.commentator.pipeline._LLM_TIMEOUT_S", 0.01)
    errors = await _narration_errors(_commentator("off", _FailingLLM("")))
    assert ["didn't answer" in e for e in errors] == [True]


@pytest.mark.asyncio
async def test_a_model_that_returns_nothing_is_reported() -> None:
    errors = await _narration_errors(_commentator("off", _EmptyLLM("")))
    assert ["no text" in e for e in errors] == [True]


@pytest.mark.asyncio
async def test_the_supervisor_survives_a_silent_narrator() -> None:
    c = _commentator("watch", _EmptyLLM(""))
    await c._drain_and_narrate(_Batch(events=[_turn_end(True)], session_key="s"))
    assert c._supervisor_tasks
    await asyncio.gather(*c._supervisor_tasks)


@pytest.mark.asyncio
async def test_a_later_message_adds_to_the_goal_instead_of_replacing_it() -> None:
    c = _commentator("watch", _ScriptedLLM("OK"))
    start = Event(
        kind=EventKind.USER_MESSAGE,
        source="hooks.codex",
        payload={"session_id": "s", "text": "Fix the parser", "delivery": "observed"},
    )
    c._consume_control_event(start)
    ctx = c._contexts["s"]
    version = ctx.query_version
    for source, delivery in (
        ("web", None),
        ("dialog.manager", "sent"),
        ("hooks.codex", "delivered"),
    ):
        payload = {"session_id": "s", "text": "Skip the README"}
        if delivery:
            payload["delivery"] = delivery
        c._consume_control_event(Event(kind=EventKind.USER_MESSAGE, source=source, payload=payload))

    assert ctx.goal is not None
    assert "Fix the parser" in ctx.goal
    assert "Skip the README" in ctx.goal
    assert ctx.query_version == version  # narration in progress isn't thrown away
    assert ctx.history == ["- the user said: Skip the README"]  # once, for three events

    # What then reaches the CLI as a prompt is a new request.
    c._consume_control_event(
        Event(
            kind=EventKind.USER_MESSAGE,
            source="hooks.codex",
            payload={"session_id": "s", "text": "Now add tests", "delivery": "observed"},
        )
    )
    assert ctx.goal == "Now add tests"
    assert ctx.query_version == version + 1


@pytest.mark.asyncio
async def test_the_supervisor_is_told_what_it_already_said() -> None:
    seen: list[str] = []

    class _Recording(_ScriptedLLM):
        def stream_chat(self, messages, *, system=None, max_tokens=None, temperature=None):  # type: ignore[no-untyped-def]
            seen.append(messages[0].content)
            return super().stream_chat(messages, system=system)

    c = _commentator("watch", _Recording("WARN\nTests were not run."))
    batch = _Batch(events=[_turn_end(True)], session_key="s")
    await c._supervise(batch, "turn ended")
    await c._supervise(batch, "turn ended")
    assert "ALREADY_TOLD_THE_USER" not in seen[0]
    assert "ALREADY TOLD THE USER:\nTests were not run." in seen[1]


class _Gated(_ScriptedLLM):
    """Holds each review until released, and records the reasons it was asked about."""

    def __init__(self, reply: str) -> None:
        super().__init__(reply)
        self.release = asyncio.Event()
        self.reasons: list[str] = []

    def stream_chat(self, messages, *, system=None, max_tokens=None, temperature=None):  # type: ignore[no-untyped-def]
        self.reasons.append(messages[0].content)

        async def gen() -> AsyncIterator[str]:
            await self.release.wait()
            yield self.reply

        return gen()


@pytest.mark.asyncio
async def test_the_end_of_the_turn_is_reviewed_even_if_a_review_is_running() -> None:
    # It used to be skipped: "the next checkpoint will look", but this one is the last.
    llm = _Gated("OK")
    c = _commentator("watch", llm)
    c._review_if_due(_Batch(events=[_tool(), _tool(), _tool()], session_key="s"))
    async with asyncio.timeout(2):  # the provider is fetched off the event loop
        while not llm.reasons:
            await asyncio.sleep(0.01)

    c._review_if_due(_Batch(events=[_turn_end(True)], session_key="s"))
    await asyncio.sleep(0.05)
    assert len(llm.reasons) == 1  # never two at once

    llm.release.set()
    await asyncio.gather(*c._supervisor_tasks)
    assert len(llm.reasons) == 2
    assert "turn ended" in llm.reasons[1]


@pytest.mark.asyncio
async def test_a_new_request_cancels_the_postponed_review() -> None:
    llm = _Gated("OK")
    c = _commentator("watch", llm)
    c._review_if_due(_Batch(events=[_tool(), _tool(), _tool()], session_key="s"))
    await asyncio.sleep(0)
    c._review_if_due(_Batch(events=[_turn_end(True)], session_key="s"))
    c._contexts["s"].query_version += 1
    llm.release.set()
    await asyncio.gather(*c._supervisor_tasks)
    assert len(llm.reasons) == 1


def test_a_test_run_is_judged_by_its_end_without_terminal_colours() -> None:
    noise = "\x1b[31mF\x1b[0m" * 1000
    done = Event(
        kind=EventKind.TOOL_CALL_FINISHED,
        source="hooks.codex",
        payload={
            "tool": "Bash",
            "is_error": True,
            "preview": f"Warning: truncated {noise} FAILED tests/a.py::t - boom; 1 failed",
        },
    )
    line = _format_one(done)
    assert "\x1b" not in line
    assert line.endswith("FAILED tests/a.py::t - boom; 1 failed")
    assert len(line) < 1600


def test_the_same_verdict_line_from_different_failures_is_not_a_repeat() -> None:
    c = _commentator("watch", _ScriptedLLM("OK"))
    ctx = c._contexts.setdefault("s", _SessionContext())
    head = "Warning: truncated output (original token count: 19298) "
    batch = _Batch(
        events=[
            _failed("Bash", head + "FAILED tests/a.py::one - boom; 1 failed in 2.1s"),
            _failed("Bash", head + "FAILED tests/b.py::two - other; 1 failed in 3.4s"),
        ],
        session_key="s",
    )
    c._record_for_supervisor(batch)
    assert c._checkpoint_reason(batch, ctx) is None


def test_the_supervisor_sees_time_and_counts() -> None:
    c = _commentator("watch", _ScriptedLLM("OK"))
    ctx = c._contexts.setdefault("s", _SessionContext())
    ctx.started_at -= 300
    edit = Event(
        kind=EventKind.FILE_EDITED, source="x", payload={"path": "a.py", "session_id": "s"}
    )
    c._record_for_supervisor(_Batch(events=[_tool(), _tool(), edit, edit], session_key="s"))
    assert ctx.progress == "5 min since the request, 2 tool calls, 1 files edited"


@pytest.mark.asyncio
async def test_an_empty_review_is_reported_not_taken_for_ok() -> None:
    got = await _drain(
        _commentator("guard", _ScriptedLLM("")), _Batch(session_key="s", events=[_tool()])
    )
    assert EventKind.SUPERVISOR_VERDICT not in got
    assert "unreviewed" in got[EventKind.ERROR]["message"]


def test_switching_off_drops_what_was_about_to_be_said() -> None:
    c = _commentator("watch", _ScriptedLLM("OK"))
    ctx = c._contexts.setdefault("s", _SessionContext())
    c._buffer = [_tool()]
    in_flight = _Batch(events=[_tool()], session_key="s", query_version=ctx.query_version)
    stop = Event(kind=EventKind.NARRATION_STOP, source="hooks", payload={})
    assert c._consume_control_event(stop) == "consume"
    assert c._buffer == []
    assert c._is_stale_batch(in_flight)


def _edit(tool: str, **tool_input: object) -> Event:
    return Event(
        kind=EventKind.TOOL_CALL_STARTED,
        source="x",
        payload={"tool": tool, "input": tool_input, "session_id": "s"},
    )


def test_the_supervisor_reads_the_change_itself() -> None:
    c = _commentator("watch", _ScriptedLLM("OK"))
    ctx = c._contexts.setdefault("s", _SessionContext())
    batch = _Batch(
        events=[
            _edit("Edit", file_path="src/a/b.py", old_string="x = 1", new_string="x = 2"),
            _edit("apply_patch", command="*** Begin Patch\n*** Update File: c.py\n+y"),
            _edit("Write", file_path="d.py", content="print(1)"),
            _edit("MultiEdit", file_path="e.py", edits=[{"old_string": "m", "new_string": "n"}]),
            _edit("Read", file_path="not_an_edit.py"),
            _edit("Bash", command="not an edit"),
        ],
        session_key="s",
    )
    c._record_for_supervisor(batch)
    assert len(ctx.edits) == 4
    assert ctx.edits[0] == "a/b.py: - x = 1 ⏎ + x = 2"
    assert "*** Update File: c.py" in ctx.edits[1]
    assert ctx.edits[2] == "d.py: print(1)"
    assert ctx.edits[3] == "e.py: - m ⏎ + n"


def test_a_long_edit_is_cut_and_only_the_latest_are_kept() -> None:
    c = _commentator("watch", _ScriptedLLM("OK"))
    ctx = c._contexts.setdefault("s", _SessionContext())
    events = [_edit("Write", file_path=f"{n}.py", content="x" * 5000) for n in range(9)]
    c._record_for_supervisor(_Batch(events=events, session_key="s"))
    assert len(ctx.edits) == 6
    assert ctx.edits[-1].startswith("8.py")
    assert len(ctx.edits[-1]) < 1300


def test_edits_reach_the_review_in_both_styles() -> None:
    for style, label in (("api", "RECENT EDITS:"), ("cli", "RECENT EDITS:")):
        text = build_supervisor_user(
            user_query="q", summary=None, history=[], reason="r", style=style, edits=["a.py: + x"]
        )
        assert label in text
        assert "a.py: + x" in text
        bare = build_supervisor_user(
            user_query="q", summary=None, history=[], reason="r", style=style
        )
        assert label not in bare


def test_speech_text_drops_what_tts_would_read_out() -> None:
    assert speech_text("Правим `precisedelta` в **src/humanize/time.py**") == (
        "Правим precisedelta в time.py"
    )
    assert speech_text(r"edited C:\Users\me\repo\a.py") == "edited a.py"
    # Not paths.
    assert speech_text("and/or, TCP/IP, 1/2") == "and/or, TCP/IP, 1/2"
    assert speech_text("see https://example.com/a/b.html") == "see https://example.com/a/b.html"


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


# --- the user asking for a check -----------------------------------------


class _VerdictLLM:
    prompt_style = "api"

    def __init__(self, verdict: str) -> None:
        self.verdict = verdict
        self.prompts: list[str] = []

    def stream_chat(self, messages, **_kwargs) -> AsyncIterator[str]:  # type: ignore[no-untyped-def]
        self.prompts.append(messages[0].content)

        async def gen() -> AsyncIterator[str]:
            yield self.verdict

        return gen()


@pytest.mark.asyncio
async def test_a_check_is_a_checkpoint_and_repeats_what_the_user_asked_to_hear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = EventBus()
    sup = _VerdictLLM("WARN\nThe agent has been stuck on the same test.")
    cfg = CommentatorConfig(supervisor=SupervisorConfig(mode="watch"))
    commentator = Commentator(bus, cfg, "en", llm=_VerdictLLM("Still working."))  # type: ignore[arg-type]
    monkeypatch.setattr(commentator, "_supervisor", lambda: sup)
    ctx = commentator._contexts.setdefault("_none", _SessionContext())
    ctx.last_verdict = ("warn", "The agent has been stuck on the same test.")

    async with bus.subscribe() as q:
        runner = asyncio.create_task(commentator.run())
        await asyncio.sleep(0.05)
        await bus.publish(Event(kind=EventKind.USER_CHECK, source="web", payload={}))
        verdict = None
        spoken = []
        try:
            async with asyncio.timeout(3):
                while verdict is None or len(spoken) < 2:
                    event = await q.get()
                    if event.kind is EventKind.SUPERVISOR_VERDICT:
                        verdict = event.payload
                    if event.kind is EventKind.COMMENTATOR_UTTERANCE and not event.payload.get(
                        "streaming"
                    ):
                        spoken.append(event.payload["text"])
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)

    assert verdict["reason"].startswith("the user asked for a check right now")
    assert verdict["repeat"] is False
    assert "stuck on the same test" in spoken[-1]
    assert "the user asks what is going on" not in sup.prompts[0]


# --- the supervisor does not depend on the narrator ------------------------


class _BrokenNarrator:
    prompt_style = "api"

    def __init__(self, *, raise_error: bool) -> None:
        self.raise_error = raise_error

    def stream_chat(self, messages, *, system=None, max_tokens=None, temperature=None):  # type: ignore[no-untyped-def]
        async def gen() -> AsyncIterator[str]:
            if self.raise_error:
                raise RuntimeError("auto commentator: codex narrated nothing")
            if False:  # pragma: no cover - makes this an async generator
                yield ""

        return gen()


@pytest.mark.asyncio
@pytest.mark.parametrize("raise_error", [True, False], ids=["narrator-error", "narrator-empty"])
async def test_a_failed_narration_still_reaches_the_supervisor(raise_error: bool) -> None:
    # The review used to be started only after a successful narration, so a
    # narrator on a model the plan rejects silently disabled Supervisor+ too.
    supervisor = _ScriptedLLM("STOP\nThe agent deleted the migrations folder.")
    cfg = CommentatorConfig(supervisor=SupervisorConfig(mode="guard"))
    c = Commentator(EventBus(), cfg, "en", llm=_BrokenNarrator(raise_error=raise_error))  # type: ignore[arg-type]
    c._supervisor = lambda: supervisor  # type: ignore[method-assign, assignment, return-value]

    async with c._bus.subscribe() as q:
        await c._drain_and_narrate(_Batch(events=[_turn_end(True)], session_key="s"))
        await asyncio.gather(*c._supervisor_tasks)
        kinds = []
        while not q.empty():
            kinds.append(q.get_nowait().kind)

    assert EventKind.SUPERVISOR_VERDICT in kinds
    assert EventKind.SUPERVISOR_STOP in kinds
