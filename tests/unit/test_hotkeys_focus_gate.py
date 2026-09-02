import asyncio

import pytest
from pynput import keyboard

from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import EventKind
from voice_copilot.hotkeys import HEADLESS, Binding, HotkeyService

# pynput's dummy backend (headless Linux) folds every Key into one value, so
# combos cannot be told apart there; the real backends run these on the
# Windows and macOS legs of CI.
pytestmark = pytest.mark.skipif(HEADLESS, reason="no display: pynput dummy backend")


@pytest.mark.asyncio
async def test_push_to_talk_is_skipped_when_not_focused() -> None:
    bus = EventBus()
    loop = asyncio.get_running_loop()
    bindings = [
        Binding(
            name="push_to_talk",
            combo="alt+space",
            press_kind=EventKind.USER_SPEAK_REQUESTED,
            press_payload={"phase": "start"},
        )
    ]
    svc = HotkeyService(bus, loop, bindings, is_focused=lambda: False)

    async with bus.subscribe() as q:
        svc._on_press(keyboard.Key.alt)
        svc._on_press(keyboard.Key.space)
        await asyncio.sleep(0.05)
        assert q.empty()


@pytest.mark.asyncio
async def test_push_to_talk_fires_when_focused() -> None:
    bus = EventBus()
    loop = asyncio.get_running_loop()
    bindings = [
        Binding(
            name="push_to_talk",
            combo="alt+space",
            press_kind=EventKind.USER_SPEAK_REQUESTED,
            press_payload={"phase": "start"},
        )
    ]
    svc = HotkeyService(bus, loop, bindings, is_focused=lambda: True)

    async with bus.subscribe() as q:
        svc._on_press(keyboard.Key.alt)
        svc._on_press(keyboard.Key.space)
        event = await asyncio.wait_for(q.get(), timeout=1)
        assert event.kind == EventKind.USER_SPEAK_REQUESTED


@pytest.mark.asyncio
async def test_other_bindings_are_unaffected_by_focus_gate() -> None:
    bus = EventBus()
    loop = asyncio.get_running_loop()
    bindings = [
        Binding(name="interrupt", combo="alt+shift+space", press_kind=EventKind.USER_INTERRUPT)
    ]
    svc = HotkeyService(bus, loop, bindings, is_focused=lambda: False)

    async with bus.subscribe() as q:
        svc._on_press(keyboard.Key.alt)
        svc._on_press(keyboard.Key.shift)
        svc._on_press(keyboard.Key.space)
        event = await asyncio.wait_for(q.get(), timeout=1)
        assert event.kind == EventKind.USER_INTERRUPT
