"""Global hotkey listener (pynput) bridged to the asyncio event bus.

Runs the pynput keyboard.Listener in its own OS thread — so every
publish goes through `run_coroutine_threadsafe` onto the main loop.

Hotkey syntax: modifier names (`alt`, `ctrl`, `shift`, `cmd`, `win`)
joined by `+`, with one non-modifier key — either a single character
(`m`, `s`) or a named key (`space`, `enter`, `tab`, `esc`).
Examples: `alt+space`, `alt+shift+space`, `alt+m`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from pynput import keyboard

from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import Event, EventKind

log = logging.getLogger(__name__)

_MODIFIERS = {"alt", "ctrl", "shift", "cmd", "win"}
_NAMED_KEYS: dict[str, keyboard.Key] = {
    "space": keyboard.Key.space,
    "enter": keyboard.Key.enter,
    "esc": keyboard.Key.esc,
    "tab": keyboard.Key.tab,
    "backspace": keyboard.Key.backspace,
}


def _parse_combo(combo: str) -> tuple[frozenset[str], Any]:
    """Return (modifiers-as-canonical-names, key-object-or-char-string)."""
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    mods: set[str] = set()
    key: Any = None
    for p in parts:
        if p == "win":
            mods.add("cmd")
        elif p in _MODIFIERS:
            mods.add(p)
        elif p in _NAMED_KEYS:
            key = _NAMED_KEYS[p]
        elif len(p) == 1:
            key = p  # single character, stored lowercase
        else:
            raise ValueError(f"unrecognised hotkey token: {p!r}")
    if key is None:
        raise ValueError(f"hotkey {combo!r} has no non-modifier key")
    return frozenset(mods), key


def _canonical_key(k: Any) -> Any:
    """Normalise a pynput key event into our internal representation."""
    name = getattr(k, "name", None)
    if name:
        if name.startswith("alt"):
            return "alt"
        if name.startswith("ctrl"):
            return "ctrl"
        if name.startswith("shift"):
            return "shift"
        if name.startswith("cmd"):
            return "cmd"
    if isinstance(k, keyboard.KeyCode) and k.char:
        return k.char.lower()
    return k  # a keyboard.Key enum value (space, enter, …)


@dataclass(frozen=True)
class Binding:
    name: str
    combo: str
    press_kind: EventKind | None = None
    release_kind: EventKind | None = None
    press_payload: dict[str, Any] | None = None
    release_payload: dict[str, Any] | None = None


class HotkeyService:
    """Thread-backed global hotkey listener."""

    def __init__(
        self,
        bus: EventBus,
        loop: asyncio.AbstractEventLoop,
        bindings: list[Binding],
    ) -> None:
        self._bus = bus
        self._loop = loop
        self._bindings: list[tuple[Binding, frozenset[str], Any]] = []
        for b in bindings:
            try:
                mods, key = _parse_combo(b.combo)
            except ValueError as e:
                log.warning("skipping hotkey %s: %s", b.name, e)
                continue
            self._bindings.append((b, mods, key))

        self._pressed: set[Any] = set()
        self._active: set[str] = set()
        self._listener: keyboard.Listener | None = None

    def _pressed_mods(self) -> frozenset[str]:
        return frozenset(m for m in ("alt", "ctrl", "shift", "cmd") if m in self._pressed)

    def _satisfied(self, mods: frozenset[str], key: Any) -> bool:
        return self._pressed_mods() == mods and key in self._pressed

    def _publish(self, kind: EventKind, payload: dict[str, Any]) -> None:
        event = Event(kind=kind, source="hotkey", payload=payload)
        try:
            asyncio.run_coroutine_threadsafe(self._bus.publish(event), self._loop)
        except RuntimeError:
            log.debug("event loop gone, dropping hotkey event %s", kind)

    def _on_press(self, k: Any) -> None:
        self._pressed.add(_canonical_key(k))
        for binding, mods, key in self._bindings:
            if binding.name in self._active:
                continue
            if not self._satisfied(mods, key):
                continue
            self._active.add(binding.name)
            if binding.press_kind is not None:
                payload = {"hotkey": binding.combo, "name": binding.name}
                if binding.press_payload:
                    payload.update(binding.press_payload)
                self._publish(binding.press_kind, payload)

    def _on_release(self, k: Any) -> None:
        self._pressed.discard(_canonical_key(k))
        for binding, mods, key in self._bindings:
            if binding.name not in self._active:
                continue
            if self._satisfied(mods, key):
                continue
            self._active.discard(binding.name)
            if binding.release_kind is not None:
                payload = {"hotkey": binding.combo, "name": binding.name}
                if binding.release_payload:
                    payload.update(binding.release_payload)
                self._publish(binding.release_kind, payload)

    def start(self) -> None:
        self._listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self._listener.daemon = True
        self._listener.start()
        log.info("hotkeys registered: %s", [b[0].combo for b in self._bindings])

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None


def default_bindings(hotkeys_cfg: Any) -> list[Binding]:
    """Map the config section to concrete bindings."""
    return [
        Binding(
            name="push_to_talk",
            combo=hotkeys_cfg.push_to_talk,
            press_kind=EventKind.USER_SPEAK_REQUESTED,
            release_kind=EventKind.USER_SPEAK_REQUESTED,
            press_payload={"phase": "start"},
            release_payload={"phase": "end"},
        ),
        Binding(
            name="interrupt",
            combo=hotkeys_cfg.interrupt,
            press_kind=EventKind.USER_INTERRUPT,
        ),
        Binding(
            name="mute_toggle",
            combo=hotkeys_cfg.mute_toggle,
            # No dedicated kind yet — ride on USER_MESSAGE-less channel via payload.
            press_kind=EventKind.USER_MESSAGE,
            press_payload={"control": "mute_toggle"},
        ),
        Binding(
            name="pause_toggle",
            combo=hotkeys_cfg.pause_toggle,
            press_kind=EventKind.USER_PAUSE_TOGGLE,
        ),
    ]
