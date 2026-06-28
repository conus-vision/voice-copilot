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
import shutil
import sys
import threading
import time
from typing import Any

from voice_copilot.adapters.base import CLIAdapter, QuickAsideCapability
from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import Event, EventKind

log = logging.getLogger(__name__)

if sys.platform == "win32":
    from winpty import PtyProcess as _PtyProcess
else:
    from ptyprocess import PtyProcess as _PtyProcess


def _terminal_size() -> tuple[int, int]:
    """Return the real terminal's (rows, cols), the order PtyProcess expects.

    Falls back to a sane default when there is no real terminal (e.g. output
    piped, or under pytest), so the child PTY matches the visible window
    instead of the library default 24x80 — which otherwise makes the child
    scroll/wrap at the wrong row and column.
    """
    size = shutil.get_terminal_size(fallback=(80, 24))
    cols = size.columns if size.columns > 0 else 80
    rows = size.lines if size.lines > 0 else 24
    return rows, cols


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
        self._write_lock = threading.Lock()

    async def start(self, initial_prompt: str | None = None) -> None:
        rows, cols = _terminal_size()
        self._child = _PtyProcess.spawn(
            self._argv, cwd=self._cwd, env=self._env, dimensions=(rows, cols)
        )
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
        """Suspend the direct child process only; forked worker subprocesses
        (if any) keep running, so this may not actually stop the agent's turn.
        """
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
        with self._write_lock:
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
        ):  # pragma: no cover - win32 console APIs; narrows ctypes.windll for mypy
            return
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.GetStdHandle.restype = wintypes.HANDLE
        kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.ReadConsoleW.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]

        std_input_handle = -10
        std_output_handle = -11
        enable_processed_output = 0x0001
        enable_virtual_terminal_processing = 0x0004
        enable_virtual_terminal_input = 0x0200

        hin = kernel32.GetStdHandle(std_input_handle)
        hout = kernel32.GetStdHandle(std_output_handle)
        old_in = wintypes.DWORD()
        old_out = wintypes.DWORD()
        kernel32.GetConsoleMode(hin, ctypes.byref(old_in))
        kernel32.GetConsoleMode(hout, ctypes.byref(old_out))

        # Output: render the child's VT escape sequences natively instead of
        # printing them as literal text.
        kernel32.SetConsoleMode(
            hout, old_out.value | enable_processed_output | enable_virtual_terminal_processing
        )
        # Input: raw + VT, so arrow/function keys reach the child as the VT
        # sequences it expects. (msvcrt.getwch returns Windows scan codes, not
        # VT, which is why special keys printed garbage.) Clearing line/echo/
        # processed-input bits (mode = only VT-input) gives us raw keystrokes.
        kernel32.SetConsoleMode(hin, enable_virtual_terminal_input)

        # ConPTY paints from the top-left of the viewport, so hand it a clean
        # screen — otherwise its cursor lands on our prior console content.
        # (voice-copilot's own status goes to the browser panel, not here.)
        sys.stdout.write("\x1b[2J\x1b[H")
        sys.stdout.flush()

        def feed_stdin() -> None:
            buf = ctypes.create_unicode_buffer(128)
            nread = wintypes.DWORD()
            while child.isalive():
                ok = kernel32.ReadConsoleW(hin, buf, 128, ctypes.byref(nread), None)
                if not ok or nread.value == 0:
                    continue
                text = buf[: nread.value]
                try:
                    with self._write_lock:
                        child.write(text)
                except Exception:  # child gone
                    return

        def watch_resize() -> None:
            # Windows has no SIGWINCH; poll the console size and tell the child
            # when it changes so its TUI reflows to the real window.
            last = _terminal_size()
            while child.isalive():
                time.sleep(0.2)
                current = _terminal_size()
                if current != last:
                    last = current
                    try:
                        child.setwinsize(current[0], current[1])
                    except Exception:  # child gone or resize unsupported
                        return

        # Daemon thread: ReadConsoleW can't be interrupted, so after the child
        # exits this stays blocked until the next keypress (or process exit)
        # — acceptable because vc tears its whole process down right after.
        feeder = threading.Thread(target=feed_stdin, name="pty.stdin", daemon=True)
        feeder.start()
        threading.Thread(target=watch_resize, name="pty.resize", daemon=True).start()
        try:
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
        finally:
            # Restore the user's original console modes so their shell isn't
            # left in raw/VT mode after vc exits.
            kernel32.SetConsoleMode(hin, old_in.value)
            kernel32.SetConsoleMode(hout, old_out.value)

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
                        with self._write_lock:
                            child.write(user)
        finally:
            termios.tcsetattr(stdin_fd, termios.TCSAFLUSH, old)
