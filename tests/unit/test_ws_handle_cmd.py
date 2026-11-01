import asyncio

import pytest

from voice_copilot.audio.mic import MicSession
from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import EventKind
from voice_copilot.focus import FocusRouter
from voice_copilot.web.ws import _handle_cmd


@pytest.mark.asyncio
async def test_typed_message_takes_the_route_speech_does() -> None:
    bus = EventBus()
    async with bus.subscribe() as q:
        cmd = {"type": "cmd", "cmd": "user_message", "text": "  use the other file  "}
        await _handle_cmd(bus, MicSession(), cmd, None, None, voice_input=False)
        event = await asyncio.wait_for(q.get(), timeout=1.0)
    assert event.kind is EventKind.USER_MESSAGE
    assert event.source == "web"  # The dialog manager only delivers from sources it trusts.
    assert event.payload["text"] == "use the other file"


@pytest.mark.asyncio
async def test_empty_typed_message_is_dropped() -> None:
    bus = EventBus()
    async with bus.subscribe() as q:
        cmd = {"type": "cmd", "cmd": "user_message", "text": "   "}
        await _handle_cmd(bus, MicSession(), cmd, None, None, voice_input=False)
        assert q.empty()


@pytest.mark.asyncio
async def test_panel_focus_cmd_updates_focus_router() -> None:
    bus = EventBus()
    mic = MicSession()
    router = FocusRouter(narrate_only_when_focused=True)

    await _handle_cmd(
        bus, mic, {"type": "cmd", "cmd": "panel_focus", "focused": True}, None, None, router
    )
    assert router._panel_focused is True

    await _handle_cmd(
        bus, mic, {"type": "cmd", "cmd": "panel_focus", "focused": False}, None, None, router
    )
    assert router._panel_focused is False
