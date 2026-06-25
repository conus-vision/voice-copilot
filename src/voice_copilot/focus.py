"""Per-instance focus tracking, used to gate narration and push-to-talk so
multiple simultaneously running `vc` instances don't act on the same event
or speak over each other.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

from voice_copilot.core.config import config_path

log = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.5


def is_console_window_focused() -> bool:
    """True if THIS process's own console window is the OS foreground window.

    Always False on non-Windows — there's no single, desktop-environment-
    independent POSIX equivalent of GetForegroundWindow/GetConsoleWindow.
    """
    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    return bool(user32.GetForegroundWindow() == kernel32.GetConsoleWindow())


def shared_focus_state_path() -> Path:
    return config_path().parent / "focus-state.json"


def record_focus(pid: int | None = None) -> None:
    """Claim "most recently focused" system-wide — used for sticky
    narration when `narrate_only_when_focused` is off and several `vc`
    instances are running at once."""
    path = shared_focus_state_path()
    payload = {"pid": pid if pid is not None else os.getpid(), "ts": time.time()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        log.debug("could not record focus state: %s", e)


def is_last_focused(pid: int | None = None) -> bool:
    path = shared_focus_state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(data.get("pid") == (pid if pid is not None else os.getpid()))


class FocusRouter:
    def __init__(self, *, narrate_only_when_focused: bool) -> None:
        self._narrate_only_when_focused = narrate_only_when_focused
        self._panel_focused = False
        self._current = False
        self._task: asyncio.Task[None] | None = None
        self._on_narrate_gate: Callable[[bool], None] | None = None

    def set_panel_focus(self, focused: bool) -> None:
        self._panel_focused = focused

    def on_narrate_gate(self, callback: Callable[[bool], None]) -> None:
        self._on_narrate_gate = callback

    @property
    def current_focus(self) -> bool:
        return self._current

    def start(self) -> None:
        self._task = asyncio.create_task(self._poll(), name="focus-router")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def _tick(self) -> None:
        terminal_focused = await asyncio.to_thread(is_console_window_focused)
        self._current = terminal_focused or self._panel_focused
        if self._current:
            record_focus()

    def _should_narrate(self) -> bool:
        if self._narrate_only_when_focused:
            return self._current
        return is_last_focused()

    async def _poll(self) -> None:
        last: bool | None = None
        while True:
            try:
                await self._tick()
                should = self._should_narrate()
                if should != last:
                    last = should
                    if self._on_narrate_gate is not None:
                        self._on_narrate_gate(should)
            except Exception:
                log.exception("focus poll iteration failed")
            await asyncio.sleep(_POLL_INTERVAL_S)
