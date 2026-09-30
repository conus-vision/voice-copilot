"""On macOS, Option+letter hotkeys match the key, not the character it types.

pynput reports the character a key produces, and Option changes it: Option+P
arrives as "π", Option+N as a dead key with no character at all. So the
default `alt+p` (pause) and `alt+shift+n` never fired on a Mac. The key code
that comes with every macOS key event still names the key.
"""

from __future__ import annotations

import pytest
from pynput import keyboard

from voice_copilot import hotkeys
from voice_copilot.hotkeys import HEADLESS, Binding, HotkeyService


@pytest.fixture
def mac(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hotkeys.sys, "platform", "darwin")


def test_option_letters_are_read_by_key_code(mac: None) -> None:
    assert hotkeys._canonical_key(keyboard.KeyCode(vk=0x23, char="π")) == "p"
    assert hotkeys._canonical_key(keyboard.KeyCode(vk=0x2D)) == "n"  # dead key
    assert hotkeys._canonical_key(keyboard.KeyCode(vk=0x2E, char="µ")) == "m"
    # Plain typing is unchanged.
    assert hotkeys._canonical_key(keyboard.KeyCode(vk=0x23, char="P")) == "p"


def test_other_systems_keep_the_character(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hotkeys.sys, "platform", "linux")
    assert hotkeys._canonical_key(keyboard.KeyCode(vk=0x23, char="π")) == "π"


@pytest.mark.skipif(HEADLESS, reason="no display: pynput dummy backend folds keys")
def test_option_p_fires_the_pause_hotkey(mac: None) -> None:
    import asyncio

    from voice_copilot.core.bus import EventBus
    from voice_copilot.core.events import EventKind

    fired: list[str] = []
    svc = HotkeyService(
        EventBus(),
        asyncio.new_event_loop(),
        [Binding(name="pause_toggle", combo="alt+p", press_kind=EventKind.USER_PAUSE_TOGGLE)],
    )
    svc._publish = lambda kind, payload: fired.append(payload["name"])  # type: ignore[method-assign]
    svc._on_press(keyboard.Key.alt)
    svc._on_press(keyboard.KeyCode(vk=0x23, char="π"))
    assert fired == ["pause_toggle"]
