"""Supervisor+'s seatbelt in the plugin hooks: a dangerous command waits before it runs.

The hook is the one place that comes before the command, so this is where the three
reactions differ: hold waits for a yes, pause waits a while and then lets go, say only
raises the alarm. In every case we only ever deny; an allowed call goes on to the
CLI's own permissions.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from voice_copilot.adapters.hooks import HookAdapter
from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import Event, EventKind
from voice_copilot.proxy.session import SessionRegistry


class _Tap:
    def __init__(self, bus: EventBus) -> None:
        self.events: list[Event] = []
        publish = bus.publish

        async def record(ev: Event) -> None:
            self.events.append(ev)
            await publish(ev)

        bus.publish = record  # type: ignore[method-assign]

    def of(self, kind: EventKind) -> list[dict[str, Any]]:
        return [e.payload for e in self.events if e.kind is kind]


def _adapter(reaction: str | None, pause_s: int = 30) -> tuple[HookAdapter, _Tap]:
    bus = EventBus()
    tap = _Tap(bus)
    policy = None if reaction is None else (reaction, pause_s)
    adapter = HookAdapter(bus, SessionRegistry(), hold_timeout_s=0.2, gate=lambda: policy)
    return adapter, tap


def _call(command: str, sid: str = "s1", tool_id: str = "t1", **fields: Any) -> dict[str, Any]:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": sid,
        "cwd": "/repo",
        "tool_name": "Bash",
        "tool_use_id": tool_id,
        "tool_input": {"command": command},
        **fields,
    }


def _denied(reply: dict[str, Any] | None) -> str | None:
    if not reply:
        return None
    out = reply.get("hookSpecificOutput") or {}
    return out.get("permissionDecisionReason") if out.get("permissionDecision") == "deny" else None


async def test_a_harmless_command_is_not_held() -> None:
    adapter, tap = _adapter("hold")
    reply = await asyncio.wait_for(adapter.handle_hook("claude", _call("pytest -q")), 0.1)
    assert _denied(reply) is None
    assert tap.of(EventKind.SUPERVISOR_DANGER) == []


async def test_nothing_is_held_without_supervisor_plus() -> None:
    adapter, tap = _adapter(None)
    reply = await asyncio.wait_for(
        adapter.handle_hook("claude", _call("git push --force")), timeout=0.1
    )
    assert _denied(reply) is None
    assert tap.of(EventKind.SUPERVISOR_DANGER) == []


async def test_hold_waits_and_the_user_allows() -> None:
    adapter, tap = _adapter("hold")
    held = asyncio.create_task(adapter.handle_hook("claude", _call("git push --force")))
    await asyncio.sleep(0.02)
    assert not held.done()
    alarm = tap.of(EventKind.SUPERVISOR_DANGER)[0]
    assert alarm["rule"] == "force_push" and alarm["reaction"] == "hold"
    assert alarm["before_run"] is True
    assert adapter.held_commands == ["t1"]

    assert adapter.decide(None, allow=True) is True
    assert _denied(await asyncio.wait_for(held, 0.1)) is None
    decided = tap.of(EventKind.SUPERVISOR_DANGER_DECIDED)[0]
    assert (decided["allowed"], decided["by"]) == (True, "user")
    assert adapter.held_commands == []
    assert tap.of(EventKind.TOOL_CALL_FINISHED) == []  # it runs: PostToolUse reports it


async def test_hold_and_the_user_refuses_it_never_runs() -> None:
    adapter, tap = _adapter("hold")
    held = asyncio.create_task(adapter.handle_hook("claude", _call("git reset --hard")))
    await asyncio.sleep(0.02)
    assert adapter.decide("t1", allow=False) is True
    reason = _denied(await asyncio.wait_for(held, 0.1))
    assert reason is not None
    assert "Supervisor+" in reason and "declined" in reason
    # The log the supervisor reads says it did not run, so it isn't judged as done.
    finished = tap.of(EventKind.TOOL_CALL_FINISHED)
    assert finished[-1]["is_error"] is True
    assert finished[-1]["tool_use_id"] == "t1"
    assert "before it ran" in finished[-1]["preview"]
    assert "declined" in finished[-1]["preview"]


async def test_hold_with_no_answer_refuses_before_the_cli_would_kill_the_hook() -> None:
    adapter, tap = _adapter("hold")
    reply = await asyncio.wait_for(
        adapter.handle_hook("claude", _call("rm -rf ~/projects")), timeout=1.0
    )
    reason = _denied(reply)
    assert reason is not None and "did not answer" in reason
    assert tap.of(EventKind.SUPERVISOR_DANGER_DECIDED)[0]["by"] == "timeout"


async def test_pause_lets_it_go_when_nobody_objects() -> None:
    adapter, tap = _adapter("pause", pause_s=0)
    adapter._hold_timeout_s = 5.0  # the pause, not the hold limit, decides here
    reply = await asyncio.wait_for(
        adapter.handle_hook("claude", _call("git push -f origin main")), timeout=1.0
    )
    assert _denied(reply) is None
    decided = tap.of(EventKind.SUPERVISOR_DANGER_DECIDED)[0]
    assert decided["allowed"] is True and decided["by"] == "timeout"


async def test_pause_can_still_be_refused() -> None:
    adapter, _ = _adapter("pause", pause_s=30)
    held = asyncio.create_task(adapter.handle_hook("claude", _call("npm publish")))
    await asyncio.sleep(0.02)
    assert adapter.decide(None, allow=False)
    assert _denied(await asyncio.wait_for(held, 0.1)) is not None


async def test_say_only_raises_the_alarm() -> None:
    adapter, tap = _adapter("say")
    reply = await asyncio.wait_for(
        adapter.handle_hook("claude", _call("git push --force")), timeout=0.1
    )
    assert _denied(reply) is None
    assert tap.of(EventKind.SUPERVISOR_DANGER)[0]["reaction"] == "say"
    assert tap.of(EventKind.SUPERVISOR_DANGER_DECIDED) == []


async def test_a_sub_agent_and_a_background_session_are_held_too() -> None:
    adapter, tap = _adapter("hold")
    await adapter.handle_hook(
        "claude", {"hook_event_name": "SessionStart", "session_id": "active", "cwd": "/repo"}
    )
    sub = asyncio.create_task(
        adapter.handle_hook("claude", _call("git push --force", sid="other", agent_id="a1"))
    )
    await asyncio.sleep(0.02)
    assert not sub.done()
    assert tap.of(EventKind.SUPERVISOR_DANGER)[0]["subagent"] is True
    adapter.decide(None, allow=False)
    assert _denied(await asyncio.wait_for(sub, 0.1)) is not None


async def test_parallel_calls_are_answered_one_by_one() -> None:
    adapter, _ = _adapter("hold")
    first = asyncio.create_task(adapter.handle_hook("claude", _call("git push -f", tool_id="a")))
    second = asyncio.create_task(
        adapter.handle_hook("claude", _call("git reset --hard", tool_id="b"))
    )
    await asyncio.sleep(0.02)
    assert adapter.held_commands == ["a", "b"]
    adapter.decide("b", allow=True)
    assert _denied(await asyncio.wait_for(second, 0.1)) is None
    assert not first.done()
    adapter.decide(None, allow=False)
    assert _denied(await asyncio.wait_for(first, 0.1)) is not None


async def test_switching_off_lets_the_cli_decide_and_stopping_refuses() -> None:
    adapter, _ = _adapter("hold")
    held = asyncio.create_task(adapter.handle_hook("claude", _call("git push --force")))
    await asyncio.sleep(0.02)
    await adapter.switch_off()
    assert _denied(await asyncio.wait_for(held, 0.1)) is None

    adapter, _ = _adapter("hold")
    held = asyncio.create_task(adapter.handle_hook("claude", _call("git push --force")))
    await asyncio.sleep(0.02)
    await adapter.stop()
    assert _denied(await asyncio.wait_for(held, 0.1)) is not None


async def test_no_held_command_means_nothing_to_decide() -> None:
    adapter, _ = _adapter("hold")
    assert adapter.decide(None, allow=True) is False
    assert adapter.decide("nope", allow=False) is False


@pytest.mark.parametrize("cli", ["codex", "claude"])
async def test_codex_list_commands_are_checked(cli: str) -> None:
    adapter, tap = _adapter("say")
    call = _call("x")
    call["tool_name"] = "shell"
    call["tool_input"] = {"command": ["bash", "-lc", "git clean -fdx"]}
    await adapter.handle_hook(cli, call)
    assert tap.of(EventKind.SUPERVISOR_DANGER)[0]["rule"] == "clean"


async def test_switching_one_session_off_leaves_anothers_held_command_waiting() -> None:
    adapter, _ = _adapter("hold")
    mine = asyncio.create_task(
        adapter.handle_hook("claude", _call("git push -f", sid="a", tool_id="1"))
    )
    theirs = asyncio.create_task(
        adapter.handle_hook("codex", _call("git reset --hard", sid="b", tool_id="2"))
    )
    await asyncio.sleep(0.02)
    await adapter.switch_off(session_id="a")
    assert _denied(await asyncio.wait_for(mine, 0.1)) is None  # the CLI decides again
    await asyncio.sleep(0.02)
    assert not theirs.done()
    assert adapter.held_commands == ["2"]
    adapter.decide("2", allow=False)
    assert _denied(await asyncio.wait_for(theirs, 0.1)) is not None


async def test_a_refused_edit_never_counts_as_an_edit() -> None:
    adapter, tap = _adapter("hold")
    call = _call("x")
    call["tool_name"] = "Write"
    call["tool_input"] = {"file_path": "/repo/.env", "content": "KEY=1"}
    held = asyncio.create_task(adapter.handle_hook("claude", call))
    await asyncio.sleep(0.02)
    assert tap.of(EventKind.FILE_EDITED) == []
    adapter.decide(None, allow=False)
    assert _denied(await asyncio.wait_for(held, 0.1)) is not None
    assert tap.of(EventKind.FILE_EDITED) == []

    allowed = asyncio.create_task(adapter.handle_hook("claude", {**call, "tool_use_id": "t9"}))
    await asyncio.sleep(0.02)
    adapter.decide(None, allow=True)
    await asyncio.wait_for(allowed, 0.1)
    assert [e["path"] for e in tap.of(EventKind.FILE_EDITED)] == ["/repo/.env"]


async def test_a_panel_opened_late_gets_what_is_still_held() -> None:
    adapter, _ = _adapter("hold")
    adapter._hold_timeout_s = 30.0
    held = asyncio.create_task(adapter.handle_hook("claude", _call("git push --force")))
    await asyncio.sleep(0.02)
    alarms = adapter.held_alarms()
    assert [a["id"] for a in alarms] == ["t1"]
    assert 0 < alarms[0]["wait_s"] <= 30
    adapter.decide(None, allow=True)
    await asyncio.wait_for(held, 0.1)
    assert adapter.held_alarms() == []


@pytest.mark.parametrize(
    "command",
    [
        "vc go",
        "vc react say",
        "vc mode narrator",
        "vc review off",
        "vc off all",
        'bash -lc "voice-copilot go"',
        "voice-copilot-hook --chat claude -- vc go",
    ],
)
async def test_the_agent_cannot_answer_for_the_user(command: str) -> None:
    adapter, tap = _adapter("hold")
    reply = await asyncio.wait_for(adapter.handle_hook("claude", _call(command)), timeout=0.1)
    reason = _denied(reply)
    assert reason is not None and "only the user" in reason
    assert tap.of(EventKind.ERROR)[0]["where"] == "self_approval"


@pytest.mark.parametrize(
    "command", ["vc check", "vc ask why", "vc deny", 'grep -rn "vc go" docs', "git log"]
)
async def test_harmless_vc_words_and_text_about_them_go_through(command: str) -> None:
    adapter, _ = _adapter("hold")
    reply = await asyncio.wait_for(adapter.handle_hook("claude", _call(command)), timeout=0.1)
    assert _denied(reply) is None


async def test_without_supervisor_plus_vc_words_are_the_agents_to_run() -> None:
    adapter, _ = _adapter(None)
    reply = await asyncio.wait_for(adapter.handle_hook("claude", _call("vc mode narrator")), 0.1)
    assert _denied(reply) is None
