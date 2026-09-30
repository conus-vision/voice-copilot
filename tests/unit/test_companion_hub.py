"""The companion hub: plugin and hook reports become bus events; replies control the agent.

Payload shapes follow what Claude Code 2.1 actually posted to an HTTP hook
(captured while building this), plus the documented Qwen, Gemini and Copilot
variants.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from voice_copilot.companion.hub import CompanionHub
from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import Event, EventKind
from voice_copilot.proxy.session import SessionRegistry

SID = "4c19f792-f34e-5e18-87b2-c5cef4558d12"


def claude(event: str, **fields: Any) -> dict[str, Any]:
    return {
        "session_id": SID,
        "transcript_path": "/home/dev/.claude/projects/x/t.jsonl",
        "cwd": "/home/dev/project",
        "permission_mode": "default",
        "hook_event_name": event,
        **fields,
    }


class Recorder:
    def __init__(self, bus: EventBus) -> None:
        self.events: list[Event] = []
        self._bus = bus
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Recorder:
        ready = asyncio.Event()

        async def pump() -> None:
            async with self._bus.subscribe() as q:
                ready.set()
                while True:
                    self.events.append(await q.get())

        self._task = asyncio.create_task(pump())
        await ready.wait()
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._task is not None
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)

    async def settle(self) -> None:
        for _ in range(5):
            await asyncio.sleep(0)

    def kinds(self) -> list[str]:
        return [e.kind.value for e in self.events]

    def of(self, kind: EventKind) -> list[Event]:
        return [e for e in self.events if e.kind is kind]


def _hub(**kwargs: Any) -> tuple[EventBus, SessionRegistry, CompanionHub]:
    bus = EventBus()
    registry = SessionRegistry()
    return bus, registry, CompanionHub(bus, registry, port=8765, **kwargs)


async def test_a_claude_turn_becomes_bus_events() -> None:
    bus, registry, hub = _hub()
    async with Recorder(bus) as rec:
        await hub.handle_hook(
            "claude", "claude", claude("UserPromptSubmit", prompt="fix the parser")
        )
        await hub.handle_hook(
            "claude",
            "claude",
            claude(
                "PreToolUse",
                tool_name="Edit",
                tool_input={"file_path": "/p/a.py"},
                tool_use_id="t1",
            ),
        )
        await hub.handle_hook(
            "claude",
            "claude",
            claude(
                "PostToolUse",
                tool_name="Edit",
                tool_input={"file_path": "/p/a.py"},
                tool_response={"filePath": "/p/a.py"},
                tool_use_id="t1",
            ),
        )
        await hub.handle_hook(
            "claude",
            "claude",
            claude(
                "PostToolUseFailure",
                tool_name="Bash",
                tool_input={"command": "pytest"},
                error="3 failed",
                tool_use_id="t2",
            ),
        )
        await hub.handle_hook(
            "claude",
            "claude",
            claude(
                "MessageDisplay",
                turn_id="u",
                message_id="m1",
                index=0,
                final=True,
                delta="Fixed the parser.",
            ),
        )
        await hub.handle_hook(
            "claude",
            "claude",
            claude("Stop", stop_hook_active=False, last_assistant_message="Fixed the parser."),
        )
        await hub.handle_hook("claude", "claude", claude("SessionEnd", reason="other"))
        await rec.settle()

    assert rec.kinds() == [
        "session.started",
        "turn.started",
        "user.message",
        "tool.call.started",
        "tool.call.finished",
        "file.edited",
        "tool.call.finished",
        "agent.text",
        "turn.ended",
        "session.ended",
    ]
    key = rec.events[0].payload["session_id"]
    assert key.startswith("claude-")
    assert all(e.payload["session_id"] == key for e in rec.events)
    assert rec.of(EventKind.USER_MESSAGE)[0].payload["text"] == "fix the parser"
    failed = rec.of(EventKind.TOOL_CALL_FINISHED)[1].payload
    assert failed["is_error"] is True and failed["preview"] == "3 failed"
    # The answer is spoken once: MessageDisplay carried it, Stop does not repeat it.
    assert len(rec.of(EventKind.AGENT_TEXT)) == 1
    # The session ended: it leaves the registry again.
    assert registry.all() == []


async def test_stop_speaks_the_answer_when_no_message_hook_carried_it() -> None:
    bus, _, hub = _hub()
    async with Recorder(bus) as rec:
        await hub.handle_hook("claude", "codex", claude("UserPromptSubmit", prompt="hi"))
        await hub.handle_hook("claude", "codex", claude("Stop", last_assistant_message="Done."))
        await rec.settle()
    assert [e.payload["text"] for e in rec.of(EventKind.AGENT_TEXT)] == ["Done."]


async def test_accumulated_message_text_is_narrated_as_deltas() -> None:
    # Qwen resends the whole message so far on every MessageDisplay.
    bus, _, hub = _hub()
    async with Recorder(bus) as rec:
        for text in ("Reading", "Reading the parser", "Reading the parser now."):
            await hub.handle_hook(
                "claude", "qwen", claude("MessageDisplay", message_id="m", displayed_text=text)
            )
        await rec.settle()
    assert [e.payload["text"] for e in rec.of(EventKind.AGENT_TEXT)] == [
        "Reading",
        " the parser",
        " now.",
    ]


async def test_a_new_prompt_makes_its_session_the_narrated_one() -> None:
    _, registry, hub = _hub()
    registry.identify({"user-agent": "claude-cli/2.1"}, provider="anthropic")
    proxied = registry.get_active_id()
    await hub.handle_hook("claude", "claude", claude("UserPromptSubmit", prompt="go"))
    assert registry.get_active_id() != proxied
    assert registry.get_active_id().startswith("claude-")  # type: ignore[union-attr]


async def test_permission_prompt_is_spoken_once() -> None:
    bus, _, hub = _hub()
    async with Recorder(bus) as rec:
        await hub.handle_hook(
            "claude",
            "claude",
            claude("PermissionRequest", tool_name="Bash", tool_input={"command": "rm -rf build"}),
        )
        await hub.handle_hook(
            "claude",
            "claude",
            claude(
                "Notification",
                message="Claude needs your permission to use Bash",
                notification_type="permission_prompt",
            ),
        )
        await hub.handle_hook(
            "claude",
            "claude",
            claude(
                "Notification",
                message="Claude is waiting for your input",
                notification_type="idle_prompt",
            ),
        )
        await rec.settle()
    waiting = rec.of(EventKind.AGENT_AWAITING_INPUT)
    assert len(waiting) == 1
    assert waiting[0].payload["tool"] == "Bash"


async def test_launches_of_other_instances_and_duplicate_plugins_are_ignored() -> None:
    bus, _, hub = _hub()
    async with Recorder(bus) as rec:
        foreign = await hub.handle_hook(
            "claude", "claude", claude("UserPromptSubmit", prompt="x"), launch="9999-abc"
        )
        duplicate = await hub.handle_hook(
            "claude", "claude", claude("UserPromptSubmit", prompt="x"), header_launch="8765-abc"
        )
        await rec.settle()
    assert foreign is None and duplicate is None
    assert rec.events == []
    assert hub.status()["sessions"] == []


async def test_a_proxied_launch_only_adds_what_the_proxy_cannot_see() -> None:
    bus, registry, hub = _hub()
    registry.identify({"user-agent": "claude-cli/2.1"}, provider="anthropic")
    launch = hub.new_launch("claude", proxied=True)
    assert launch.id.startswith("8765-")
    async with Recorder(bus) as rec:
        for payload in (
            claude("UserPromptSubmit", prompt="go"),
            claude("PreToolUse", tool_name="Read", tool_input={}),
            claude("PermissionRequest", tool_name="Bash", tool_input={"command": "ls"}),
        ):
            await hub.handle_hook("claude", "claude", payload, launch=launch.id)
        await rec.settle()
    assert rec.kinds() == ["agent.awaiting_input"]
    # Filed under the proxy's session, which the narrator is following.
    assert rec.events[0].payload["session_id"] == registry.get_active_id()


async def test_a_restarted_instance_accepts_its_own_old_launch_ids() -> None:
    _, _, hub = _hub()
    reply_bus_events = await hub.handle_hook(
        "claude",
        "claude",
        claude("UserPromptSubmit", prompt="x"),
        launch="8765-old",
        mode="control",
    )
    assert reply_bus_events is None
    assert hub.status()["sessions"][0]["mode"] == "control"


async def test_pause_holds_the_next_tool_call_until_resumed() -> None:
    bus, _, hub = _hub(owns_dialog=True)
    await hub.handle_hook("claude", "claude", claude("UserPromptSubmit", prompt="go"))
    async with Recorder(bus) as rec:
        await hub.pause("toggle")
        gate = asyncio.create_task(
            hub.handle_hook(
                "claude",
                "claude",
                claude("PreToolUse", tool_name="Bash", tool_input={"command": "ls"}),
            )
        )
        await asyncio.sleep(0.05)
        assert not gate.done()
        await hub.resume("toggle")
        assert await asyncio.wait_for(gate, 1) is None  # empty reply: the tool runs
        await rec.settle()
    assert rec.kinds().count("agent.paused") == 1
    assert rec.kinds().count("agent.resumed") == 1


async def test_supervisor_stop_refuses_every_tool_until_the_user_takes_over() -> None:
    _, _, hub = _hub(owns_dialog=True)
    await hub.handle_hook("claude", "claude", claude("UserPromptSubmit", prompt="go"))
    await hub.stop("edits outside the task")
    for tool in ("Read", "Bash"):  # agents call tools in parallel
        reply = await hub.handle_hook("claude", "claude", claude("PreToolUse", tool_name=tool))
        assert reply is not None
        assert reply["continue"] is False
        assert reply["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "edits outside the task" in reply["hookSpecificOutput"]["permissionDecisionReason"]
    # A new prompt from the user ends the stop.
    await hub.handle_hook("claude", "claude", claude("UserPromptSubmit", prompt="carry on"))
    assert await hub.handle_hook("claude", "claude", claude("PreToolUse", tool_name="Read")) is None


async def test_voice_message_rides_the_next_tool_boundary() -> None:
    bus, _, hub = _hub(owns_dialog=True)
    await hub.handle_hook("claude", "claude", claude("UserPromptSubmit", prompt="go"))
    async with Recorder(bus) as rec:
        assert await hub.send_message("also update the docs")
        reply = await hub.handle_hook(
            "claude", "claude", claude("PostToolUse", tool_name="Read", tool_response="x")
        )
        await rec.settle()
    assert reply is not None
    context = reply["hookSpecificOutput"]["additionalContext"]
    assert "also update the docs" in context
    assert reply["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    deliveries = [e.payload["delivery"] for e in rec.of(EventKind.USER_MESSAGE)]
    assert deliveries == ["queued", "sent"]


async def test_voice_message_left_at_the_end_of_a_turn_keeps_the_agent_going() -> None:
    _, _, hub = _hub(owns_dialog=True)
    await hub.handle_hook("claude", "claude", claude("UserPromptSubmit", prompt="go"))
    await hub.send_message("and add a test")
    reply = await hub.handle_hook(
        "claude", "claude", claude("Stop", last_assistant_message="Done.")
    )
    assert reply == {
        "decision": "block",
        "reason": "Message from the user, said by voice while you were working: and add a test",
    }
    # Delivered once: the next stop lets the turn end.
    assert await hub.handle_hook("claude", "claude", claude("Stop", stop_hook_active=True)) is None


async def test_gemini_and_copilot_replies_use_their_own_format() -> None:
    _, _, hub = _hub(owns_dialog=True)
    gemini = {"session_id": "g1", "hook_event_name": "BeforeTool", "tool_name": "run_shell"}
    await hub.handle_hook(
        "gemini", "gemini", {"session_id": "g1", "hook_event_name": "BeforeAgent", "prompt": "go"}
    )
    await hub.stop("wrong files")
    reply = await hub.handle_hook("gemini", "gemini", gemini)
    assert reply is not None and reply["decision"] == "deny" and reply["continue"] is False

    _, _, hub = _hub(owns_dialog=True)
    await hub.handle_hook(
        "copilot", "copilot", {"prompt": "go", "cwd": "/p"}, event="userPromptSubmitted"
    )
    await hub.stop("wrong files")
    reply = await hub.handle_hook(
        "copilot",
        "copilot",
        {"toolName": "bash", "toolArgs": '{"command": "ls"}', "cwd": "/p"},
        event="preToolUse",
    )
    assert reply == {
        "permissionDecision": "deny",
        "permissionDecisionReason": "Voice Copilot supervisor stopped the agent: wrong files",
    }


async def test_plugin_events_and_commands() -> None:
    bus, _, hub = _hub(owns_dialog=True)
    async with Recorder(bus) as rec:
        ok = await hub.handle_events(
            "pi",
            "0199aa-session",
            [
                {"kind": "session.started", "payload": {"cwd": "/p"}},
                {"kind": "user.message", "payload": {"text": "read the readme"}},
                {"kind": "agent.thinking", "payload": {"text": "The user wants the README."}},
                {
                    "kind": "tool.call.started",
                    "payload": {"id": "c1", "tool": "read", "args": {"path": "README.md"}},
                },
                {
                    "kind": "tool.call.finished",
                    "payload": {"id": "c1", "tool": "write", "args": {"path": "a.py"}, "ok": True},
                },
                {"kind": "agent.output", "payload": {"text": "It says hello."}},
                {"kind": "turn.ended", "payload": {"final": True}},
            ],
        )
        await rec.settle()
    assert ok
    assert rec.kinds() == [
        "session.started",
        "user.message",
        "agent.thinking",
        "tool.call.started",
        "tool.call.finished",
        "file.edited",
        "agent.text",
        "turn.ended",
    ]
    await hub.send_message("also count the lines")
    command = await hub.next_command("pi", "0199aa-session", wait_s=1)
    assert command is not None and command["name"] == "send_user_message"
    assert command["payload"]["text"] == "also count the lines"
    assert await hub.next_command("pi", "0199aa-session", wait_s=0.05) is None

    await hub.stop("stop now")
    command = await hub.next_command("pi", "0199aa-session", wait_s=1)
    assert command is not None and command["name"] == "interrupt"
    verdict = await hub.gate("pi", "0199aa-session")
    assert verdict["decision"] == "deny" and verdict["stop"] is True


def test_time_ordered_session_ids_get_distinct_keys() -> None:
    _, _, hub = _hub()
    a, _ = hub._session(
        "pi",
        "01a0f2b2-0000-7000-8000-000000000001",
        transport="plugin",
        proxied=False,
        launch_id=None,
        cwd=None,
    )
    b, _ = hub._session(
        "pi",
        "01a0f2b2-0000-7000-8000-000000000002",
        transport="plugin",
        proxied=False,
        launch_id=None,
        cwd=None,
    )
    assert a.key != b.key


async def test_controls_are_left_to_the_dialog_manager_when_it_runs() -> None:
    bus, _, hub = _hub(owns_dialog=False)
    await hub.handle_hook("claude", "claude", claude("UserPromptSubmit", prompt="go"))
    runner = asyncio.create_task(hub.run())
    await bus.publish(Event(kind=EventKind.SUPERVISOR_STOP, source="t", payload={"message": "x"}))
    await asyncio.sleep(0.01)
    assert runner.done()  # nothing to do: `vc` owns the controls
    assert await hub.handle_hook("claude", "claude", claude("PreToolUse", tool_name="Read")) is None


@pytest.mark.parametrize(
    ("dialect", "payload", "event", "expected"),
    [
        ("gemini", {"hook_event_name": "AfterAgent", "prompt_response": "ok"}, None, "Stop"),
        ("gemini", {"hook_event_name": "BeforeTool", "tool_name": "x"}, None, "PreToolUse"),
        ("copilot", {"toolName": "x"}, "postToolUse", "PostToolUse"),
        ("copilot", {"error": {"message": "boom"}}, "errorOccurred", "StopFailure"),
        ("claude", {"hook_event_name": "StopCancelled"}, None, "Interrupt"),
    ],
)
def test_dialect_event_names_map_to_one_vocabulary(
    dialect: str, payload: dict[str, Any], event: str | None, expected: str
) -> None:
    from voice_copilot.companion import dialects

    assert dialects.parse(dialect, dialect, payload, event).event == expected
