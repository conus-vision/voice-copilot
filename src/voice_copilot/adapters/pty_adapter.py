"""Live-terminal adapter for `vc <name>`.

Spawns the child in a PTY and bridges it to the user's real terminal so the
wrapped CLI's TUI renders exactly as if run directly, while still letting us
inject push-to-talk text into its stdin. Narration comes entirely from the
proxy (see `proxy/cli_shims.py`'s `resolve_cli_for_vc`) — this adapter never
parses the child's rendered output.

Windows uses pywinpty's ConPTY-backed `PtyProcess`; POSIX uses
`ptyprocess.PtyProcess`. Both expose the same spawn/read/write/isalive/
terminate surface, so a single bidirectional pump drives either. We write
that pump ourselves because the maintained ConPTY binding has no
`.interact()`.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from voice_copilot.adapters.base import CLIAdapter, QuickAsideCapability
from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import Event, EventKind

log = logging.getLogger(__name__)

if sys.platform == "win32":
    from winpty import PtyProcess as _PtyProcess
else:
    from ptyprocess import PtyProcess as _PtyProcess


class PtyAdapter(CLIAdapter):
    name = "pty"
    quick_aside = QuickAsideCapability.QUEUE

    def __init__(
        self,
        bus: EventBus,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self._bus = bus
        self._argv = argv
        self._env = env
        self._cwd = cwd
        self._child: Any = None
        self._pump_task: asyncio.Task[None] | None = None
        self._paused = False

    async def start(self, initial_prompt: str | None = None) -> None:
        self._child = _PtyProcess.spawn(self._argv, cwd=self._cwd, env=self._env)
        await self._bus.publish(
            Event(kind=EventKind.SESSION_STARTED, source="pty", payload={"argv": self._argv})
        )
        self._pump_task = asyncio.create_task(asyncio.to_thread(self._pump), name="pty.pump")

    async def send_user_message(self, text: str, *, urgent: bool = False) -> None:
        if self._child is None or not self._child.isalive():
            return
        # PtyProcess writes a single string; a trailing CR submits the line
        # to the child just like an Enter keypress in the terminal would.
        await asyncio.to_thread(self._write, text + "\r")

    async def stop(self) -> None:
        if self._child is not None and self._child.isalive():
            try:
                self._child.terminate(force=True)
            except Exception as e:  # terminate is best-effort on shutdown
                log.warning("pty terminate failed: %s", e)
        if self._pump_task is not None:
            self._pump_task.cancel()
        await self._bus.publish(Event(kind=EventKind.SESSION_ENDED, source="pty"))

    async def pause(self) -> bool:
        if self._child is None or not self._child.isalive() or self._paused:
            return False
        try:
            import psutil

            psutil.Process(self._child.pid).suspend()
        except Exception as e:
            log.warning("pty pause failed: %s", e)
            return False
        self._paused = True
        return True

    async def resume(self) -> bool:
        if self._child is None or not self._paused:
            return False
        try:
            import psutil

            psutil.Process(self._child.pid).resume()
        except Exception as e:
            log.warning("pty resume failed: %s", e)
            return False
        self._paused = False
        return True

    def exit_task(self) -> asyncio.Task[None] | None:
        return self._pump_task

    def _write(self, data: str) -> None:
        child = self._child
        if child is None:
            return
        # ptyprocess.write expects bytes; winpty.PtyProcess.write expects str.
        if sys.platform == "win32":
            child.write(data)
        else:
            child.write(data.encode())

    def _pump(self) -> None:
        """Bridge the child PTY to the real terminal until the child exits.

        Runs in a worker thread. When stdin is not a TTY (under pytest, or
        when output is piped) there is no terminal to bridge: we skip
        raw-mode setup and the interactive loop and just wait for the child
        to finish, so the adapter stays importable and unit-testable without
        touching real console state.
        """
        child = self._child
        if child is None:
            return

        if not sys.stdin.isatty():
            self._drain_until_exit(child)
            return

        if sys.platform == "win32":
            self._pump_windows(child)
        else:
            self._pump_posix(child)

    def _drain_until_exit(self, child: Any) -> None:
        while True:
            try:
                child.read(1024)
            except EOFError:
                return
            except Exception:  # any read failure means the child is gone
                return
            if not child.isalive():
                return

    def _pump_windows(self, child: Any) -> None:
        if (
            sys.platform != "win32"
        ):  # pragma: no cover - narrows msvcrt to win32 for the type checker
            return
        import msvcrt
        import threading

        def feed_stdin() -> None:
            while child.isalive():
                ch = msvcrt.getwch()
                try:
                    child.write(ch)
                except Exception:  # child gone
                    return

        # Daemon thread: getwch() can't be interrupted, so after the child
        # exits this stays blocked until the next keypress (or process exit)
        # — acceptable because vc tears its whole process down right after.
        feeder = threading.Thread(target=feed_stdin, name="pty.stdin", daemon=True)
        feeder.start()
        while True:
            try:
                data = child.read(1024)
            except EOFError:
                return
            except Exception:  # child gone
                return
            if data:
                sys.stdout.write(data)
                sys.stdout.flush()
            if not child.isalive():
                return

    def _pump_posix(self, child: Any) -> None:
        if (
            sys.platform == "win32"
        ):  # pragma: no cover - narrows termios/tty to posix for the type checker
            return
        import os
        import select
        import termios
        import tty

        stdin_fd = sys.stdin.fileno()
        old = termios.tcgetattr(stdin_fd)
        try:
            tty.setraw(stdin_fd)
            child_fd = child.fileno()
            while child.isalive():
                rlist, _, _ = select.select([child_fd, stdin_fd], [], [], 0.05)
                if child_fd in rlist:
                    try:
                        data = child.read(1024)
                    except EOFError:
                        break
                    if data:
                        os.write(sys.stdout.fileno(), data)
                if stdin_fd in rlist:
                    user = os.read(stdin_fd, 1024)
                    if user:
                        child.write(user)
        finally:
            termios.tcsetattr(stdin_fd, termios.TCSAFLUSH, old)
