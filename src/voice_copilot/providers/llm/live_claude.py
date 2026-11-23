"""A `claude -p` that stays running and keeps the conversation.

One-shot `claude -p` sends the whole prompt each time, and Claude Code marks it for the
prompt cache each time: every call writes the full message to the cache at a premium
and reads back only the system prompt. One process fed message after message reads
everything before the new message from the cache and writes only the new part.
Measured on the supervisor's real message (`opus`, 4.5k-token first review, then eight
tool lines a checkpoint): about $0.05 a checkpoint one-shot, $0.0055 live.

A turn either completes or the process is gone: one cut short (cancelled, timed out)
leaves a reply half-written on its stdout, so the process is killed, never reused.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from voice_copilot.core.child_env import child_env

log = logging.getLogger(__name__)

#: stream-json lines carry whole messages; asyncio's default 64 KiB line limit is too small.
_LINE_LIMIT = 16 * 1024 * 1024
_STDERR_KEPT = 2000


@dataclass
class LiveReply:
    text: str
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def context_tokens(self) -> int:
        """How big the conversation is now, as the model counted it."""
        u = self.usage
        return (
            u.get("input_tokens", 0)
            + u.get("cache_creation_input_tokens", 0)
            + u.get("cache_read_input_tokens", 0)
        )


class LiveClaude:
    """One running `claude -p --input-format stream-json`: a conversation, turn by turn."""

    def __init__(
        self,
        binary: str,
        *,
        system: str,
        model: str | None,
        args: Sequence[str],
    ) -> None:
        self._binary = binary
        self._system = system
        self._model = model
        self._args = list(args)
        self._proc: asyncio.subprocess.Process | None = None
        self._system_path: str | None = None
        self._stderr: list[str] = []
        self._stderr_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self.turns = 0
        self.last_turn_at = 0.0
        self.context_tokens = 0

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def _start(self) -> asyncio.subprocess.Process:
        fd, self._system_path = tempfile.mkstemp(suffix=".txt", prefix="vc-live-sys-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(self._system)
        argv = [
            self._binary,
            *(["--model", self._model] if self._model else []),
            "--system-prompt-file",
            self._system_path,
            *[a for a in self._args if a != "-p"],
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env(),
            limit=_LINE_LIMIT,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr(proc), name="live-claude.stderr")
        log.info("live claude: started (pid %s, model %s)", proc.pid, self._model or "default")
        return proc

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        """Read stderr as it comes: a full pipe would stall the process."""
        assert proc.stderr is not None
        with contextlib.suppress(Exception):
            async for raw in proc.stderr:
                self._stderr.append(raw.decode("utf-8", errors="replace"))
                while sum(len(s) for s in self._stderr) > _STDERR_KEPT and len(self._stderr) > 1:
                    self._stderr.pop(0)

    def _why_gone(self) -> str:
        tail = "".join(self._stderr).strip()[-400:]
        return tail or "no message"

    async def turn(self, content: str) -> LiveReply:
        """Send one user message and wait for the whole reply."""
        async with self._lock:
            if self._proc is None:
                self._proc = await self._start()
            proc = self._proc
            if proc.returncode is not None or proc.stdin is None or proc.stdout is None:
                raise RuntimeError(f"the live claude session has ended: {self._why_gone()}")
            completed = False
            try:
                line = {"type": "user", "message": {"role": "user", "content": content}}
                proc.stdin.write((json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8"))
                await proc.stdin.drain()
                while True:
                    raw = await proc.stdout.readline()
                    if not raw:
                        raise RuntimeError(
                            f"the live claude session ended mid-reply: {self._why_gone()}"
                        )
                    try:
                        event = json.loads(raw)
                    except ValueError:
                        continue
                    if event.get("type") != "result":
                        continue
                    if event.get("is_error"):
                        raise RuntimeError(f"claude: {str(event.get('result') or event)[:400]}")
                    usage = {
                        k: int(v)
                        for k, v in (event.get("usage") or {}).items()
                        if isinstance(v, int)
                    }
                    reply = LiveReply(text=str(event.get("result") or ""), usage=usage)
                    completed = True
                    self.turns += 1
                    self.last_turn_at = time.monotonic()
                    self.context_tokens = reply.context_tokens
                    return reply
            finally:
                if not completed:
                    # Half a reply may still be coming: this process can't be trusted again.
                    await self._kill()

    async def close(self) -> None:
        async with self._lock:
            await self._kill()

    async def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            # On Windows `claude` is a .cmd: the node it starts is a child, not this process.
            with contextlib.suppress(Exception):
                import psutil

                for child in psutil.Process(proc.pid).children(recursive=True):
                    with contextlib.suppress(psutil.Error):
                        child.kill()
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=5)
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            self._stderr_task = None
        if self._system_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(self._system_path)
            self._system_path = None


def stats(usage: dict[str, Any]) -> str:
    """One log line: what a turn wrote to the cache and read from it."""
    return (
        f"in={usage.get('input_tokens', 0)} write={usage.get('cache_creation_input_tokens', 0)} "
        f"read={usage.get('cache_read_input_tokens', 0)} out={usage.get('output_tokens', 0)}"
    )
