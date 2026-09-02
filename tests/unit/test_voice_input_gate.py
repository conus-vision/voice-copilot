"""Voice input is off by default — nothing in the mic path may fire while it is."""

import pytest

from voice_copilot.audio.mic import MicSession
from voice_copilot.core.bus import EventBus
from voice_copilot.core.config import Config, load_config
from voice_copilot.hotkeys import default_bindings
from voice_copilot.web.ws import _handle_cmd


@pytest.fixture
def cfg(tmp_path) -> Config:
    return load_config(tmp_path / "missing.yaml")


def test_voice_input_is_disabled_by_default(cfg) -> None:
    assert cfg.voice_input.enabled is False


def test_push_to_talk_binding_is_dropped_when_voice_input_is_off(cfg) -> None:
    names = {b.name for b in default_bindings(cfg.hotkeys, voice_input=False)}
    assert "push_to_talk" not in names
    # The rest of the transport keeps working.
    assert {"interrupt", "mute_toggle", "pause_toggle", "skip_current"} <= names

    with_voice = {b.name for b in default_bindings(cfg.hotkeys)}
    assert "push_to_talk" in with_voice


@pytest.mark.asyncio
@pytest.mark.parametrize("cmd", ["speak_start", "speak_end", "mic_start", "mic_end"])
async def test_mic_commands_are_ignored_when_voice_input_is_off(cmd: str) -> None:
    bus = EventBus()
    mic = MicSession()
    async with bus.subscribe() as q:
        await _handle_cmd(bus, mic, {"type": "cmd", "cmd": cmd}, None, None, voice_input=False)
        assert q.empty()


@pytest.mark.asyncio
async def test_speak_command_still_publishes_when_voice_input_is_on() -> None:
    bus = EventBus()
    mic = MicSession()
    async with bus.subscribe() as q:
        await _handle_cmd(
            bus, mic, {"type": "cmd", "cmd": "speak_start"}, None, None, voice_input=True
        )
        event = await q.get()
        assert event.payload == {"phase": "start"}
