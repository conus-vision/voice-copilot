"""Attaching through plugin hooks.

A hook's reply goes straight into a live agent, so the rules here are about what
gets said and when: a message arrives once, a held agent really waits, a blocked
stop isn't a finished turn, and nothing keeps an agent waiting forever.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from voice_copilot.adapters.hooks import HookAdapter
from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import Event, EventKind
from voice_copilot.proxy.session import SessionRegistry


class _Tap:
    """Records what reaches the bus while the adapter runs."""

    def __init__(self, bus: EventBus) -> None:
        self.events: list[Event] = []
        self._publish = bus.publish

        async def publish(ev: Event) -> None:
            self.events.append(ev)
            await self._publish(ev)

        bus.publish = publish  # type: ignore[method-assign]

    def of(self, kind: EventKind) -> list[dict[str, Any]]:
        return [e.payload for e in self.events if e.kind is kind]


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def tap(bus: EventBus) -> _Tap:
    return _Tap(bus)


@pytest.fixture
def adapter(bus: EventBus, tap: _Tap) -> HookAdapter:
    return HookAdapter(bus, SessionRegistry(), hold_timeout_s=0.2)


def _hook(event: str, sid: str = "s1", **fields: Any) -> dict[str, Any]:
    return {"hook_event_name": event, "session_id": sid, "cwd": "/repo", **fields}


async def test_tool_call_becomes_events_with_session_and_edit(
    adapter: HookAdapter, tap: _Tap
) -> None:
    await adapter.handle_hook(
        "claude",
        _hook("PreToolUse", tool_name="Edit", tool_use_id="t1", tool_input={"file_path": "a.py"}),
    )
    started = tap.of(EventKind.TOOL_CALL_STARTED)[0]
    assert started["tool"] == "Edit"
    assert started["session_id"] == "s1"
    assert tap.of(EventKind.FILE_EDITED)[0]["path"] == "a.py"


async def test_codex_apply_patch_names_every_file(adapter: HookAdapter, tap: _Tap) -> None:
    patch = "*** Begin Patch\n*** Update File: src/a.py\n@@\n*** Add File: src/b.py\n*** End Patch"
    await adapter.handle_hook(
        "codex", _hook("PreToolUse", tool_name="apply_patch", tool_input={"command": patch})
    )
    assert [e["path"] for e in tap.of(EventKind.FILE_EDITED)] == ["src/a.py", "src/b.py"]


async def test_failed_tool_is_reported_as_failed(adapter: HookAdapter, tap: _Tap) -> None:
    await adapter.handle_hook(
        "codex", _hook("PostToolUse", tool_name="Bash", tool_response={"exit_code": 1})
    )
    await adapter.handle_hook("claude", _hook("PostToolUseFailure", tool_name="Bash", error="boom"))
    assert [e["is_error"] for e in tap.of(EventKind.TOOL_CALL_FINISHED)] == [True, True]


def _codex_command_done(tool_id: str, exit_code: int | None, status: str) -> dict[str, Any]:
    return {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "item": {
                "type": "CommandExecution",
                "id": tool_id,
                "exit_code": exit_code,
                "status": status,
            },
        },
    }


async def _codex_with_transcript(adapter: HookAdapter, path: Path) -> None:
    path.write_text("", encoding="utf-8")
    start = _hook("SessionStart", source="startup", transcript_path=str(path))
    await adapter.handle_hook("codex", start)


async def test_codex_failure_is_taken_from_the_transcript_on_the_next_hook(
    adapter: HookAdapter, tap: _Tap, tmp_path: Path
) -> None:
    # Codex's PostToolUse carries only the output, and the exit code is written after it.
    path = tmp_path / "rollout.jsonl"
    await _codex_with_transcript(adapter, path)
    post = _hook("PostToolUse", tool_name="Bash", tool_use_id="a", tool_response="")
    await adapter.handle_hook("codex", post)
    assert tap.of(EventKind.TOOL_CALL_FINISHED) == []

    _write(path, _codex_command_done("a", 3, "failed"))
    await adapter.handle_hook("codex", _hook("PreToolUse", tool_name="Bash", tool_use_id="b"))
    finished = tap.of(EventKind.TOOL_CALL_FINISHED)
    assert [e["is_error"] for e in finished] == [True]
    assert finished[0]["preview"] == "exit code 3"
    kinds = [e.kind for e in tap.events if e.kind.value.startswith("tool.call")]
    assert kinds == [EventKind.TOOL_CALL_FINISHED, EventKind.TOOL_CALL_STARTED]


async def test_a_search_that_finds_nothing_is_not_a_failure(
    adapter: HookAdapter, tap: _Tap, tmp_path: Path
) -> None:
    path = tmp_path / "rollout.jsonl"
    await _codex_with_transcript(adapter, path)

    async def run(tool_id: str, command: str, response: str, code: int) -> None:
        await adapter.handle_hook(
            "codex",
            _hook(
                "PreToolUse",
                tool_name="Bash",
                tool_use_id=tool_id,
                tool_input={"command": command},
            ),
        )
        post = _hook("PostToolUse", tool_name="Bash", tool_use_id=tool_id, tool_response=response)
        await adapter.handle_hook("codex", post)
        _write(path, _codex_command_done(tool_id, code, "failed"))

    await run("a", 'rg -n "nothing here" src', "", 1)
    await run("b", 'cd src; rg "x"', "", 1)
    await run("c", 'rg "x"', "bad regex", 2)  # rg's real errors
    await run("d", 'rg "x"', "src/a.py: ok", 1)  # output: not an empty search
    await run("e", "python -m pytest", "", 1)
    await adapter.handle_hook("codex", _hook("Stop"))
    failed = {e["tool_use_id"]: e["is_error"] for e in tap.of(EventKind.TOOL_CALL_FINISHED)}
    assert failed == {"a": False, "b": False, "c": True, "d": True, "e": True}


async def test_codex_command_end_already_written_settles_at_once(
    adapter: HookAdapter, tap: _Tap, tmp_path: Path
) -> None:
    path = tmp_path / "rollout.jsonl"
    await _codex_with_transcript(adapter, path)
    _write(path, _codex_command_done("a", 0, "completed"))
    post = _hook("PostToolUse", tool_name="Bash", tool_use_id="a", tool_response="fine")
    await adapter.handle_hook("codex", post)
    assert [e["is_error"] for e in tap.of(EventKind.TOOL_CALL_FINISHED)] == [False]


async def test_codex_sibling_result_does_not_flush_a_parallel_call(
    adapter: HookAdapter, tap: _Tap, tmp_path: Path
) -> None:
    path = tmp_path / "rollout.jsonl"
    await _codex_with_transcript(adapter, path)
    for tool_id in ("a", "b"):
        post = _hook("PostToolUse", tool_name="Bash", tool_use_id=tool_id, tool_response="x")
        await adapter.handle_hook("codex", post)
    assert tap.of(EventKind.TOOL_CALL_FINISHED) == []

    _write(path, _codex_command_done("a", 1, "failed"), _codex_command_done("b", 0, "completed"))
    await adapter.handle_hook("codex", _hook("Stop"))
    finished = {e["tool_use_id"]: e["is_error"] for e in tap.of(EventKind.TOOL_CALL_FINISHED)}
    assert finished == {"a": True, "b": False}


async def test_codex_unknown_exit_code_only_delays_the_event(
    adapter: HookAdapter, tap: _Tap, tmp_path: Path
) -> None:
    # apply_patch has no exit code, and a new transcript format would lose them all.
    path = tmp_path / "rollout.jsonl"
    await _codex_with_transcript(adapter, path)
    post = _hook("PostToolUse", tool_name="apply_patch", tool_use_id="a", tool_response="ok")
    await adapter.handle_hook("codex", post)
    await adapter.handle_hook("codex", _hook("Stop"))
    assert [e["is_error"] for e in tap.of(EventKind.TOOL_CALL_FINISHED)] == [False]


async def test_codex_without_a_transcript_reports_results_at_once(
    adapter: HookAdapter, tap: _Tap
) -> None:
    post = _hook("PostToolUse", tool_name="Bash", tool_use_id="a", tool_response="x")
    await adapter.handle_hook("codex", post)
    assert len(tap.of(EventKind.TOOL_CALL_FINISHED)) == 1


async def test_switching_off_lets_everything_go(
    adapter: HookAdapter, tap: _Tap, tmp_path: Path
) -> None:
    path = tmp_path / "t.jsonl"
    _write(path, _claude_text("before"))
    start = _hook("SessionStart", source="startup", transcript_path=str(path))
    await adapter.handle_hook("claude", start)
    await adapter.send_user_message("never delivered")
    await adapter.pause()

    await adapter.switch_off(by_agent=True)

    assert adapter.session_count == 0
    assert not adapter.is_paused
    assert tap.of(EventKind.NARRATION_STOP)
    assert tap.of(EventKind.AGENT_RESUMED)
    assert tap.of(EventKind.SESSION_ENDED)[0]["reason"] == "switched off"
    errors = [e["message"] for e in tap.of(EventKind.ERROR)]
    assert any("never delivered" in m for m in errors)
    assert any("agent turned" in m for m in errors)

    # Back on: what happened in between isn't narrated after the fact.
    _write(path, _claude_text("while it was off"))
    await adapter.handle_hook("claude", _hook("PreToolUse", transcript_path=str(path)))
    _write(path, _claude_text("after"))
    await adapter.handle_hook("claude", _hook("PostToolUse", transcript_path=str(path)))
    said = [e["text"] for e in tap.of(EventKind.AGENT_TEXT)]
    assert said == ["before", "after"]


class _Waker:
    def __init__(self, fails: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fails = fails

    async def __call__(self, thread: str, text: str) -> None:
        if self.fails:
            raise RuntimeError("no daemon")
        self.calls.append((thread, text))


async def _idle_after_a_turn(adapter: HookAdapter, cli: str = "codex") -> None:
    await adapter.handle_hook(cli, _hook("UserPromptSubmit", prompt="do it"))
    await adapter.handle_hook(cli, _hook("Stop"))


async def test_idle_session_is_woken_with_a_real_message(bus: EventBus, tap: _Tap) -> None:
    waker = _Waker()
    adapter = HookAdapter(bus, SessionRegistry(), wakers={"codex": waker})
    await _idle_after_a_turn(adapter)
    await adapter.send_user_message("one more thing")
    assert waker.calls == [("s1", "one more thing")]
    delivered = [e for e in tap.of(EventKind.USER_MESSAGE) if e.get("delivery") == "delivered"]
    assert delivered[0]["via"] == "queue"
    # Already on its way: the next prompt must not carry it a second time.
    reply = await adapter.handle_hook("codex", _hook("UserPromptSubmit", prompt="one more thing"))
    assert reply is None


async def test_working_session_is_not_woken(bus: EventBus) -> None:
    waker = _Waker()
    adapter = HookAdapter(bus, SessionRegistry(), wakers={"codex": waker})
    await adapter.handle_hook("codex", _hook("UserPromptSubmit", prompt="do it"))
    await adapter.send_user_message("stop that")
    assert waker.calls == []
    reply = await adapter.handle_hook("codex", _hook("PreToolUse", tool_name="Bash"))
    assert reply is not None


async def test_blocked_stop_is_not_idle(bus: EventBus) -> None:
    waker = _Waker()
    adapter = HookAdapter(bus, SessionRegistry(), wakers={"codex": waker})
    await adapter.handle_hook("codex", _hook("UserPromptSubmit", prompt="do it"))
    await adapter.send_user_message("first")
    assert await adapter.handle_hook("codex", _hook("Stop")) is not None
    await adapter.send_user_message("second")
    assert waker.calls == []


async def test_cli_without_a_way_in_waits_for_the_next_prompt(bus: EventBus) -> None:
    waker = _Waker()
    adapter = HookAdapter(bus, SessionRegistry(), wakers={"codex": waker})
    await _idle_after_a_turn(adapter, cli="claude")
    await adapter.send_user_message("later")
    assert waker.calls == []
    reply = await adapter.handle_hook("claude", _hook("UserPromptSubmit", prompt="next"))
    assert reply is not None
    assert "later" in reply["hookSpecificOutput"]["additionalContext"]


async def test_failed_wake_says_so_and_keeps_the_message(bus: EventBus, tap: _Tap) -> None:
    adapter = HookAdapter(bus, SessionRegistry(), wakers={"codex": _Waker(fails=True)})
    await _idle_after_a_turn(adapter)
    await adapter.send_user_message("later")
    assert "no daemon" in tap.of(EventKind.ERROR)[0]["message"]
    reply = await adapter.handle_hook("codex", _hook("UserPromptSubmit", prompt="next"))
    assert reply is not None
    assert "later" in reply["hookSpecificOutput"]["additionalContext"]


async def test_prompt_is_visible_in_trace_and_anchors_the_narrator(
    adapter: HookAdapter, tap: _Tap
) -> None:
    await adapter.handle_hook("claude", _hook("UserPromptSubmit", prompt="fix the bug"))
    # The panel's Trace only shows user messages marked as observed.
    assert tap.of(EventKind.USER_MESSAGE)[0]["delivery"] == "observed"
    assert tap.of(EventKind.TURN_STARTED)


async def test_message_refuses_the_next_tool_call_once(adapter: HookAdapter) -> None:
    await adapter.handle_hook("codex", _hook("SessionStart"))
    await adapter.send_user_message("stop, wrong branch")

    reply = await adapter.handle_hook("codex", _hook("PreToolUse", tool_name="Bash"))
    assert reply is not None
    output = reply["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "stop, wrong branch" in output["permissionDecisionReason"]

    assert await adapter.handle_hook("codex", _hook("PreToolUse", tool_name="Bash")) is None


async def test_message_is_not_put_next_to_a_tool_result(adapter: HookAdapter) -> None:
    """Models mostly dismiss text there as an injection from the tool's output."""
    await adapter.handle_hook("claude", _hook("SessionStart"))
    await adapter.send_user_message("use the other file")
    assert await adapter.handle_hook("claude", _hook("PostToolUse", tool_name="Read")) is None
    assert await adapter.handle_hook("claude", _hook("PreToolUse", tool_name="Read")) is not None


async def test_message_said_while_idle_goes_with_the_next_prompt(adapter: HookAdapter) -> None:
    await adapter.handle_hook("claude", _hook("SessionStart"))
    await adapter.send_user_message("and keep it short")

    reply = await adapter.handle_hook("claude", _hook("UserPromptSubmit", prompt="fix it"))
    assert reply is not None
    assert "and keep it short" in reply["hookSpecificOutput"]["additionalContext"]


async def test_stop_with_a_pending_message_continues_and_is_not_the_end_of_the_turn(
    adapter: HookAdapter, tap: _Tap
) -> None:
    await adapter.handle_hook("claude", _hook("SessionStart"))
    await adapter.send_user_message("also add tests")

    reply = await adapter.handle_hook("claude", _hook("Stop"))
    assert reply is not None
    assert reply["decision"] == "block"
    assert "also add tests" in reply["reason"]
    assert tap.of(EventKind.TURN_ENDED)[-1]["final"] is False

    assert await adapter.handle_hook("claude", _hook("Stop")) is None
    assert tap.of(EventKind.TURN_ENDED)[-1]["final"] is True


async def test_message_is_not_handed_to_a_subagent(adapter: HookAdapter) -> None:
    await adapter.handle_hook("claude", _hook("SessionStart"))
    await adapter.send_user_message("for the main agent")

    inner = _hook("PreToolUse", tool_name="Read", agent_id="sub1")
    assert await adapter.handle_hook("claude", inner) is None
    assert await adapter.handle_hook("claude", _hook("PreToolUse", tool_name="Read")) is not None


async def test_message_with_no_session_fails_loud(adapter: HookAdapter) -> None:
    with pytest.raises(RuntimeError, match="no attached agent session"):
        await adapter.send_user_message("anyone there?")


async def test_message_goes_to_the_session_of_the_latest_prompt(adapter: HookAdapter) -> None:
    await adapter.handle_hook("claude", _hook("SessionStart", sid="s1"))
    await adapter.handle_hook("codex", _hook("SessionStart", sid="s2"))
    await adapter.handle_hook("codex", _hook("UserPromptSubmit", sid="s2", prompt="work on b"))
    await adapter.send_user_message("for s2")

    assert await adapter.handle_hook("claude", _hook("PreToolUse", sid="s1")) is None
    assert await adapter.handle_hook("codex", _hook("PreToolUse", sid="s2")) is not None


async def test_pause_holds_the_next_tool_call_until_resume(adapter: HookAdapter) -> None:
    await adapter.handle_hook("codex", _hook("SessionStart"))
    assert await adapter.pause() is True
    assert adapter.is_paused

    held = asyncio.create_task(adapter.handle_hook("codex", _hook("PreToolUse", tool_name="Bash")))
    await asyncio.sleep(0.05)
    assert not held.done()

    # What the user says during the pause goes out with the release.
    await adapter.send_user_message("try the other approach")
    assert await adapter.resume() is True
    reply = await asyncio.wait_for(held, timeout=1.0)
    assert reply is not None
    assert "try the other approach" in reply["hookSpecificOutput"]["permissionDecisionReason"]


async def test_held_hook_lets_go_before_the_cli_would_kill_it(
    adapter: HookAdapter, tap: _Tap
) -> None:
    await adapter.handle_hook("codex", _hook("SessionStart"))
    await adapter.pause()

    await asyncio.wait_for(
        adapter.handle_hook("codex", _hook("PreToolUse", tool_name="Bash")), timeout=1.0
    )
    assert tap.of(EventKind.ERROR)[0]["where"] == "hold"
    assert adapter.is_paused  # Still paused: the next tool call is held again.


async def test_pause_only_holds_the_active_session(adapter: HookAdapter) -> None:
    await adapter.handle_hook("claude", _hook("SessionStart", sid="s1"))
    await adapter.handle_hook("codex", _hook("SessionStart", sid="s2"))
    await adapter.pause()

    other = adapter.handle_hook("codex", _hook("PreToolUse", sid="s2", tool_name="Bash"))
    await asyncio.wait_for(other, timeout=0.1)


async def test_stop_releases_a_held_agent(adapter: HookAdapter) -> None:
    await adapter.handle_hook("codex", _hook("SessionStart"))
    await adapter.pause()
    held = asyncio.create_task(adapter.handle_hook("codex", _hook("PreToolUse", tool_name="Bash")))
    await asyncio.sleep(0.02)
    await adapter.stop()
    await asyncio.wait_for(held, timeout=0.1)


async def test_session_end_reports_an_undelivered_message(adapter: HookAdapter, tap: _Tap) -> None:
    await adapter.handle_hook("claude", _hook("SessionStart"))
    await adapter.send_user_message("too late")
    await adapter.handle_hook("claude", _hook("SessionEnd", reason="exit"))

    assert adapter.session_count == 0
    assert "too late" in tap.of(EventKind.ERROR)[0]["message"]
    assert tap.of(EventKind.SESSION_ENDED)


async def test_hook_without_session_is_ignored(adapter: HookAdapter, tap: _Tap) -> None:
    assert await adapter.handle_hook("claude", {"hook_event_name": "PreToolUse"}) is None
    assert tap.events == []


async def test_unknown_hook_event_costs_nothing(adapter: HookAdapter, tap: _Tap) -> None:
    assert await adapter.handle_hook("claude", _hook("SomethingNew")) is None
    assert tap.events == []


async def test_active_cli_change_is_reported(bus: EventBus) -> None:
    seen: list[str | None] = []
    adapter = HookAdapter(bus, SessionRegistry(), on_active_cli=seen.append)
    await adapter.handle_hook("codex", _hook("SessionStart"))
    await adapter.handle_hook("codex", _hook("PreToolUse", tool_name="Bash"))
    await adapter.handle_hook("codex", _hook("SessionEnd"))
    assert seen == ["codex"]  # Kept after the session ends: narration may still be running.


# ---------------------------------------------------------------------- transcript


def _write(path: Path, *lines: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


def _claude_text(text: str, *, sidechain: bool = False) -> dict[str, Any]:
    return {
        "type": "assistant",
        "isSidechain": sidechain,
        "message": {"content": [{"type": "text", "text": text}]},
    }


async def test_agent_words_come_from_the_transcript_once(
    adapter: HookAdapter, tap: _Tap, tmp_path: Path
) -> None:
    path = tmp_path / "t.jsonl"
    _write(path, _claude_text("First I will read the config."))
    start = _hook("SessionStart", source="startup", transcript_path=str(path))
    await adapter.handle_hook("claude", start)
    await adapter.handle_hook("claude", _hook("PreToolUse", tool_name="Read"))
    assert [e["text"] for e in tap.of(EventKind.AGENT_TEXT)] == ["First I will read the config."]


async def test_resumed_session_does_not_replay_history(
    adapter: HookAdapter, tap: _Tap, tmp_path: Path
) -> None:
    path = tmp_path / "t.jsonl"
    _write(path, _claude_text("old news"))
    start = _hook("SessionStart", source="resume", transcript_path=str(path))
    await adapter.handle_hook("claude", start)
    _write(path, _claude_text("new work"))
    await adapter.handle_hook("claude", _hook("PreToolUse", tool_name="Read"))
    assert [e["text"] for e in tap.of(EventKind.AGENT_TEXT)] == ["new work"]


async def test_half_written_line_waits_for_the_next_hook(
    adapter: HookAdapter, tap: _Tap, tmp_path: Path
) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text("", encoding="utf-8")
    start = _hook("SessionStart", source="startup", transcript_path=str(path))
    await adapter.handle_hook("claude", start)
    line = json.dumps(_claude_text("complete"))
    with path.open("a", encoding="utf-8") as f:
        f.write(line[:10])
    await adapter.handle_hook("claude", _hook("PreToolUse", tool_name="Read"))
    assert tap.of(EventKind.AGENT_TEXT) == []
    with path.open("a", encoding="utf-8") as f:
        f.write(line[10:] + "\n")
    await adapter.handle_hook("claude", _hook("PostToolUse", tool_name="Read"))
    assert [e["text"] for e in tap.of(EventKind.AGENT_TEXT)] == ["complete"]


async def test_subagent_thread_and_empty_thinking_are_skipped(
    adapter: HookAdapter, tap: _Tap, tmp_path: Path
) -> None:
    path = tmp_path / "t.jsonl"
    signature_only = {
        "type": "assistant",
        "message": {"content": [{"type": "thinking", "thinking": "", "signature": "abc"}]},
    }
    _write(path, _claude_text("inner", sidechain=True), signature_only)
    start = _hook("SessionStart", source="startup", transcript_path=str(path))
    await adapter.handle_hook("claude", start)
    assert tap.of(EventKind.AGENT_TEXT) == []
    assert tap.of(EventKind.AGENT_THINKING) == []


async def test_codex_transcript_text_and_reasoning_summary(
    adapter: HookAdapter, tap: _Tap, tmp_path: Path
) -> None:
    path = tmp_path / "rollout.jsonl"
    _write(
        path,
        {"type": "session_meta", "payload": {"id": "x"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "not the agent"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "Checking the discount order"}],
                "encrypted_content": "zzz",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Found the bug."}],
            },
        },
    )
    start = _hook("SessionStart", source="startup", transcript_path=str(path))
    await adapter.handle_hook("codex", start)
    assert [e["text"] for e in tap.of(EventKind.AGENT_THINKING)] == ["Checking the discount order"]
    assert [e["text"] for e in tap.of(EventKind.AGENT_TEXT)] == ["Found the bug."]


async def test_final_answer_is_not_said_twice(
    adapter: HookAdapter, tap: _Tap, tmp_path: Path
) -> None:
    path = tmp_path / "t.jsonl"
    _write(path, _claude_text("All done."))
    start = _hook("SessionStart", source="startup", transcript_path=str(path))
    await adapter.handle_hook("claude", start)
    await adapter.handle_hook("claude", _hook("Stop", last_assistant_message="All done."))
    assert [e["text"] for e in tap.of(EventKind.AGENT_TEXT)] == ["All done."]


async def test_final_answer_is_taken_from_the_hook_when_the_transcript_lags(
    adapter: HookAdapter, tap: _Tap, tmp_path: Path
) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text("", encoding="utf-8")
    start = _hook("SessionStart", source="startup", transcript_path=str(path))
    await adapter.handle_hook("claude", start)
    await adapter.handle_hook("claude", _hook("Stop", last_assistant_message="All done."))
    assert [e["text"] for e in tap.of(EventKind.AGENT_TEXT)] == ["All done."]


async def test_transcript_format_change_only_costs_the_detail(
    adapter: HookAdapter, tap: _Tap, tmp_path: Path
) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text('not json\n[1, 2]\n{"type": "assistant", "message": "odd"}\n', encoding="utf-8")
    start = _hook("SessionStart", source="startup", transcript_path=str(path))
    await adapter.handle_hook("claude", start)
    await adapter.handle_hook("claude", _hook("PreToolUse", tool_name="Read"))
    assert tap.of(EventKind.TOOL_CALL_STARTED)
    assert tap.of(EventKind.AGENT_TEXT) == []


# ---------------------------------------------------------------------- plugin's own mail


async def _start_session(adapter: HookAdapter, cli: str = "claude") -> None:
    await adapter.handle_hook(cli, _hook("SessionStart"))


async def test_mail_goes_to_the_plugin_once_and_hooks_leave_it_alone(
    adapter: HookAdapter, tap: _Tap
) -> None:
    await _start_session(adapter)
    assert await adapter.take_mail("claude", "s1") == []
    await adapter.send_user_message("skip the README")

    pre = _hook("PreToolUse", tool_name="Read", tool_use_id="t1", tool_input={})
    assert await adapter.handle_hook("claude", pre) is None
    assert await adapter.handle_hook("claude", _hook("Stop")) is None

    assert await adapter.take_mail("claude", "s1") == ["skip the README"]
    assert await adapter.take_mail("claude", "s1") == []
    delivered = [p for p in tap.of(EventKind.USER_MESSAGE) if p.get("delivery") == "delivered"]
    assert [p["via"] for p in delivered] == ["plugin"]


async def test_hooks_take_over_when_the_plugin_stops_reading(
    adapter: HookAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _start_session(adapter)
    await adapter.take_mail("claude", "s1")
    await adapter.send_user_message("skip the README")

    import voice_copilot.adapters.hooks as hooks

    monkeypatch.setattr(hooks, "_MAIL_READER_TTL_S", 0.0)
    pre = _hook("PreToolUse", tool_name="Read", tool_use_id="t1", tool_input={})
    reply = await adapter.handle_hook("claude", pre)
    assert reply is not None
    assert "skip the README" in reply["hookSpecificOutput"]["permissionDecisionReason"]


async def test_a_message_said_during_a_pause_stops_the_held_tool_call(
    adapter: HookAdapter,
) -> None:
    await _start_session(adapter)
    await adapter.take_mail("claude", "s1")
    await adapter.pause()
    await adapter.send_user_message("not that file")
    assert await adapter.take_mail("claude", "s1") == []

    pre = _hook("PreToolUse", tool_name="Edit", tool_use_id="t1", tool_input={})
    hook = asyncio.create_task(adapter.handle_hook("claude", pre))
    await asyncio.sleep(0.01)
    await adapter.resume()
    reply = await hook
    assert reply is not None
    assert reply["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert await adapter.take_mail("claude", "s1") == []


async def test_mail_for_an_unknown_session_is_empty(adapter: HookAdapter) -> None:
    assert await adapter.take_mail("claude", "nobody") == []


async def test_a_plugin_can_report_what_the_model_said(adapter: HookAdapter, tap: _Tap) -> None:
    await adapter.handle_hook("pi", _hook("AssistantMessage", thinking="hm", text="done"))
    assert [p["text"] for p in tap.of(EventKind.AGENT_THINKING)] == ["hm"]
    assert [p["text"] for p in tap.of(EventKind.AGENT_TEXT)] == ["done"]


async def test_a_plugin_can_report_its_own_failure(adapter: HookAdapter, tap: _Tap) -> None:
    await adapter.handle_hook("pi", _hook("PluginError", message="couldn't deliver"))
    assert tap.of(EventKind.ERROR)[0]["message"] == "couldn't deliver"


async def test_a_forked_helpers_words_are_not_the_agents(adapter: HookAdapter, tap: _Tap) -> None:
    # Claude Code forks one after a turn to suggest the next prompt.
    await adapter.handle_hook(
        "claude",
        _hook("SubagentStop", agent_id="a1", last_assistant_message="run it again"),
    )
    assert tap.of(EventKind.AGENT_TEXT) == []


async def test_a_prompt_typed_to_a_working_agent_adds_to_the_task(
    adapter: HookAdapter, tap: _Tap
) -> None:
    await adapter.handle_hook("pi", _hook("UserPromptSubmit", prompt="and the docs", mid_turn=True))
    assert tap.of(EventKind.USER_MESSAGE)[0]["delivery"] == "delivered"
    assert tap.of(EventKind.TURN_STARTED) == []


async def test_it_knows_when_a_turn_is_running(adapter: HookAdapter) -> None:
    await _start_session(adapter)
    assert adapter.is_working("s1") is False
    await adapter.handle_hook("codex", _hook("UserPromptSubmit", prompt="fix it"))
    assert adapter.is_working("s1") is True
    await adapter.handle_hook("codex", _hook("Stop"))
    assert adapter.is_working("s1") is False
    assert adapter.is_working("nobody") is False


def _codex_row(tool_id: str, code: int, output: str) -> str:
    item = {
        "type": "CommandExecution",
        "id": tool_id,
        "status": "completed",
        "exit_code": code,
        "aggregated_output": output,
    }
    return json.dumps({"type": "event_msg", "payload": {"type": "item_completed", "item": item}})


async def test_a_long_codex_command_that_never_got_a_hook_still_finishes(
    adapter: HookAdapter, tap: _Tap, tmp_path: Path
) -> None:
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("", encoding="utf-8")
    start = _hook(
        "PreToolUse",
        tool_name="Bash",
        tool_use_id="exec-1",
        tool_input={"command": "pytest"},
        transcript_path=str(rollout),
    )
    await adapter.handle_hook("codex", start)
    rollout.write_text(_codex_row("exec-1", 1, "2 failed, 392 passed") + "\n", encoding="utf-8")

    await adapter.handle_hook("codex", _hook("Stop", transcript_path=str(rollout)))

    finished = tap.of(EventKind.TOOL_CALL_FINISHED)
    assert [(p["tool_use_id"], p["is_error"], p["preview"]) for p in finished] == [
        ("exec-1", True, "2 failed, 392 passed")
    ]


async def test_a_command_with_its_hook_finishes_once(
    adapter: HookAdapter, tap: _Tap, tmp_path: Path
) -> None:
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("", encoding="utf-8")
    path = str(rollout)
    await adapter.handle_hook(
        "codex",
        _hook(
            "PreToolUse",
            tool_name="Bash",
            tool_use_id="exec-1",
            tool_input={"command": "ls"},
            transcript_path=path,
        ),
    )
    await adapter.handle_hook(
        "codex",
        _hook(
            "PostToolUse",
            tool_name="Bash",
            tool_use_id="exec-1",
            tool_response="a.py",
            transcript_path=path,
        ),
    )
    rollout.write_text(_codex_row("exec-1", 0, "a.py") + "\n", encoding="utf-8")
    await adapter.handle_hook("codex", _hook("Stop", transcript_path=path))
    assert [p["tool_use_id"] for p in tap.of(EventKind.TOOL_CALL_FINISHED)] == ["exec-1"]


async def test_what_the_hook_said_first_isnt_said_again_by_the_transcript(
    adapter: HookAdapter, tap: _Tap, tmp_path: Path
) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")
    path = str(transcript)
    start = _hook("SessionStart", source="startup", transcript_path=path)
    await adapter.handle_hook("claude", start)
    await adapter.handle_hook(
        "claude", _hook("Stop", last_assistant_message="All done.", transcript_path=path)
    )
    row = {"type": "assistant", "message": {"content": [{"type": "text", "text": "All done."}]}}
    transcript.write_text(json.dumps(row) + "\n", encoding="utf-8")
    await adapter.handle_hook(
        "claude", _hook("UserPromptSubmit", prompt="next", transcript_path=path)
    )
    assert [p["text"] for p in tap.of(EventKind.AGENT_TEXT)] == ["All done."]


async def test_a_goodbye_from_a_session_we_never_met_is_ignored(
    adapter: HookAdapter, tap: _Tap
) -> None:
    await adapter.handle_hook("codex", _hook("SessionEnd", sid="stranger"))
    assert tap.of(EventKind.SESSION_ENDED) == []
    assert adapter.session_count == 0


async def test_a_hook_arriving_after_the_transcripts_line_isnt_a_second_finish(
    adapter: HookAdapter, tap: _Tap, tmp_path: Path
) -> None:
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("", encoding="utf-8")
    path = str(rollout)
    await adapter.handle_hook(
        "codex",
        _hook(
            "PreToolUse",
            tool_name="Bash",
            tool_use_id="exec-1",
            tool_input={"command": "pytest"},
            transcript_path=path,
        ),
    )
    rollout.write_text(_codex_row("exec-1", 1, "2 failed") + "\n", encoding="utf-8")
    await adapter.handle_hook(
        "codex",
        _hook(
            "PostToolUse",
            tool_name="Bash",
            tool_use_id="exec-1",
            tool_response="2 failed",
            transcript_path=path,
        ),
    )
    finished = tap.of(EventKind.TOOL_CALL_FINISHED)
    assert [(p["is_error"], p["preview"]) for p in finished] == [(True, "2 failed")]


async def test_two_answers_alike_are_both_said(
    adapter: HookAdapter, tap: _Tap, tmp_path: Path
) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")
    path = str(transcript)
    start = _hook("SessionStart", source="startup", transcript_path=path)
    await adapter.handle_hook("claude", start)
    row = {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done."}]}}
    line = json.dumps(row) + "\n"
    transcript.write_text(line, encoding="utf-8")
    await adapter.handle_hook("claude", _hook("UserPromptSubmit", prompt="a", transcript_path=path))
    transcript.write_text(line + line, encoding="utf-8")
    await adapter.handle_hook("claude", _hook("UserPromptSubmit", prompt="b", transcript_path=path))
    assert [p["text"] for p in tap.of(EventKind.AGENT_TEXT)] == ["Done.", "Done."]
