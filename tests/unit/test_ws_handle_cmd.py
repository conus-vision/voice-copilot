import pytest

from voice_copilot.audio.mic import MicSession
from voice_copilot.core.bus import EventBus
from voice_copilot.focus import FocusRouter
from voice_copilot.web.ws import _handle_cmd


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
