"""CLI adapter interface.

An adapter spawns and talks to a target LLM coding CLI. Its job:
  * normalise vendor events into `EventKind` and push them on the bus
  * accept user messages (text) from the dialog manager and inject them
  * support suspend/resume so the user can speak without the agent racing ahead

Adapters declare one of three `QuickAsideCapability` values so the dialog
manager knows how urgently a user message can be delivered.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from enum import StrEnum

log = logging.getLogger(__name__)


class QuickAsideCapability(StrEnum):
    NATIVE = "native"
    """CLI has a built-in side-question channel (inject immediately mid-turn)."""
    QUEUE = "queue"
    """No side channel, but stdin stays open — message lands on next turn boundary."""
    MANUAL = "manual"
    """External process — fall back to clipboard + popup text."""


class CLIAdapter(ABC):
    name: str = "unknown"
    quick_aside: QuickAsideCapability = QuickAsideCapability.MANUAL

    _proc: asyncio.subprocess.Process | None = None
    _paused: bool = False

    @abstractmethod
    async def start(self, initial_prompt: str | None = None) -> None:
        """Spawn the CLI subprocess and start reading its events into the bus."""
        raise NotImplementedError

    @abstractmethod
    async def send_user_message(self, text: str, *, urgent: bool = False) -> None:
        """Deliver a user message into the running session."""
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> None:
        """Close stdin and terminate the subprocess."""
        raise NotImplementedError

    async def pause(self) -> bool:
        """Freeze the child process so the user can talk uninterrupted.

        Returns True if the process was actually paused (False if already
        paused, not started, or pause unsupported on this platform). Uses
        psutil.suspend — works on Win/macOS/Linux without sending signals.
        """
        if self._proc is None or self._proc.returncode is not None:
            return False
        if self._paused:
            return False
        try:
            import psutil

            psutil.Process(self._proc.pid).suspend()
        except Exception as e:  # noqa: BLE001
            log.warning("pause failed: %s", e)
            return False
        self._paused = True
        return True

    async def resume(self) -> bool:
        if self._proc is None or self._proc.returncode is not None:
            return False
        if not self._paused:
            return False
        try:
            import psutil

            psutil.Process(self._proc.pid).resume()
        except Exception as e:  # noqa: BLE001
            log.warning("resume failed: %s", e)
            return False
        self._paused = False
        return True

    @property
    def is_paused(self) -> bool:
        return self._paused
