"""Holding the agent while a line is read.

The point is comprehension: without it the agent races ahead while you are
still listening to what it did three steps ago. The hold has to start when the
line starts and end when the browser reports it played — and it must never
outlive a browser that goes quiet.
"""

from __future__ import annotations

import asyncio

import pytest

from voice_copilot.adapters.base import CLIAdapter, QuickAsideCapability
from voice_copilot.core.bus import EventBus
from voice_copilot.core.config import DialogConfig
from voice_copilot.core.events import Event, EventKind
from voice_copilot.dialog import manager as manager_mod
from voice_copilot.dialog.manager import DialogManager


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


def _tts_started() -> Event:
    return Event(kind=EventKind.TTS_STARTED, source="tts.driver", payload={"utterance_id": "u1"})


def _playback_ready() -> Event:
    return Event(kind=EventKind.PLAYBACK_READY, source="web", payload={"reason": "eighty_percent"})


async def _run(cfg: DialogConfig, events: list[Event]) -> _FakeAdapter:
    bus, adapter = EventBus(), _FakeAdapter()
    dialog = DialogManager(bus, adapter, cfg)
    task = asyncio.create_task(dialog.run())
    await asyncio.sleep(0.02)
    for ev in events:
        await bus.publish(ev)
        await asyncio.sleep(0.02)
    task.cancel()
    return adapter


@pytest.mark.asyncio
async def test_off_by_default() -> None:
    adapter = await _run(DialogConfig(), [_tts_started(), _playback_ready()])
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_holds_for_the_line_and_releases_on_playback() -> None:
    cfg = DialogConfig(hold_agent_while_narrating=True)
    adapter = await _run(cfg, [_tts_started(), _playback_ready()])
    assert adapter.calls == ["pause", "resume"]
    assert not adapter.is_paused


@pytest.mark.asyncio
async def test_a_silent_browser_does_not_wedge_the_agent(monkeypatch) -> None:
    # Blocked autoplay, a muted tab or a closed window means the playback
    # report never arrives; the watchdog has to let the agent go anyway.
    monkeypatch.setattr(manager_mod, "_NARRATION_HOLD_TIMEOUT_S", 0.05)
    cfg = DialogConfig(hold_agent_while_narrating=True)
    bus, adapter = EventBus(), _FakeAdapter()
    dialog = DialogManager(bus, adapter, cfg)
    task = asyncio.create_task(dialog.run())
    await asyncio.sleep(0.02)
    await bus.publish(_tts_started())
    await asyncio.sleep(0.02)
    assert adapter.is_paused
    await asyncio.sleep(0.15)
    task.cancel()
    assert adapter.calls == ["pause", "resume"]


@pytest.mark.asyncio
async def test_a_pause_the_user_asked_for_is_left_alone() -> None:
    # The hold only ever releases what it took itself.
    cfg = DialogConfig(hold_agent_while_narrating=True)
    bus, adapter = EventBus(), _FakeAdapter()
    await adapter.pause()
    adapter.calls.clear()
    dialog = DialogManager(bus, adapter, cfg)
    task = asyncio.create_task(dialog.run())
    await asyncio.sleep(0.02)
    await bus.publish(_tts_started())
    await asyncio.sleep(0.02)
    await bus.publish(_playback_ready())
    await asyncio.sleep(0.02)
    task.cancel()
    assert adapter.calls == []
    assert adapter.is_paused


@pytest.mark.asyncio
async def test_the_panel_can_flip_it_live() -> None:
    cfg = DialogConfig()
    bus, adapter = EventBus(), _FakeAdapter()
    dialog = DialogManager(bus, adapter, cfg)
    dialog.set_hold_while_narrating(True)
    task = asyncio.create_task(dialog.run())
    await asyncio.sleep(0.02)
    await bus.publish(_tts_started())
    await asyncio.sleep(0.02)
    task.cancel()
    assert adapter.calls == ["pause"]
