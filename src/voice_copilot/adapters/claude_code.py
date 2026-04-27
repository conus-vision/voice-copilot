"""Claude Code adapter.

Spawns `claude --output-format stream-json --input-format stream-json --verbose`
and bridges its NDJSON event stream onto the bus. User messages are written to
stdin as `{"type":"user","message":{"role":"user","content":"..."}}` — the CLI
picks them up at the next turn boundary (queue semantics).

Reference: https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from typing import Any

from voice_copilot.adapters.base import CLIAdapter, QuickAsideCapability
from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import Event, EventKind

log = logging.getLogger(__name__)

_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


class ClaudeCodeAdapter(CLIAdapter):
    name = "claude"
    quick_aside = QuickAsideCapability.QUEUE

    def __init__(
        self,
        bus: EventBus,
        binary: str = "claude",
        extra_args: list[str] | None = None,
        env: dict[str, str] | None = None,
        suppress_llm_events: bool = False,
    ) -> None:
        self._bus = bus
        self._binary = binary
        self._extra_args = extra_args or []
        self._env = env or {}
        self._suppress_llm = suppress_llm_events
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._tool_use_to_path: dict[str, str] = {}
        self._session_id: str | None = None
        self._stdin_lock = asyncio.Lock()

    # ------------------------------------------------------------------ lifecycle

    async def start(self, initial_prompt: str | None = None) -> None:
        if shutil.which(self._binary) is None:
            raise RuntimeError(
                f"`{self._binary}` not found in PATH. "
                f"Install Claude Code: https://claude.com/product/claude-code"
            )
        argv = [
            self._binary,
            "--output-format",
            "stream-json",
            "--input-format",
            "stream-json",
            "--verbose",
            *self._extra_args,
        ]
        log.info("spawning %s", " ".join(argv))
        merged_env = {**os.environ, **self._env} if self._env else None
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
        )
        self._reader_task = asyncio.create_task(self._read_stdout(), name="claude.stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name="claude.stderr")

        if initial_prompt:
            await self.send_user_message(initial_prompt)

    async def stop(self) -> None:
        for t in (self._reader_task, self._stderr_task):
            if t is not None:
                t.cancel()
        if self._proc is not None:
            try:
                if self._proc.stdin is not None and not self._proc.stdin.is_closing():
                    self._proc.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        await self._emit(EventKind.SESSION_ENDED, {"session_id": self._session_id})

    # ------------------------------------------------------------------ input

    async def send_user_message(self, text: str, *, urgent: bool = False) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("Claude adapter not started")
        frame = {"type": "user", "message": {"role": "user", "content": text}}
        payload = (json.dumps(frame) + "\n").encode("utf-8")
        async with self._stdin_lock:
            self._proc.stdin.write(payload)
            await self._proc.stdin.drain()
        await self._emit(
            EventKind.USER_MESSAGE,
            {"text": text, "urgent": urgent, "delivery": "queued"},
        )

    # ------------------------------------------------------------------ readers

    async def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                log.warning("non-json stdout line: %r", line[:200])
                continue
            try:
                await self._handle_message(msg)
            except Exception:
                log.exception("failed to handle claude event")

    async def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break
            log.info("claude stderr: %s", line.decode("utf-8", errors="replace").rstrip())

    # ------------------------------------------------------------------ dispatch

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")

        if mtype == "system":
            if msg.get("subtype") == "init":
                self._session_id = msg.get("session_id")
                await self._emit(
                    EventKind.SESSION_STARTED,
                    {
                        "target": "claude",
                        "session_id": self._session_id,
                        "model": msg.get("model"),
                        "cwd": msg.get("cwd"),
                        "tools": msg.get("tools"),
                    },
                )
            return

        if mtype == "assistant":
            content = ((msg.get("message") or {}).get("content")) or []
            if not isinstance(content, list):
                return
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    if not self._suppress_llm:
                        await self._emit(EventKind.AGENT_TEXT, {"text": block.get("text", "")})
                elif btype == "thinking":
                    if not self._suppress_llm:
                        await self._emit(
                            EventKind.AGENT_THINKING,
                            {"text": block.get("thinking", "")},
                        )
                elif btype == "tool_use":
                    tid = block.get("id", "")
                    name = block.get("name", "")
                    inp = block.get("input") or {}
                    # Still record file paths locally so TOOL_CALL_FINISHED can
                    # emit FILE_EDITED — the proxy never sees tool results.
                    if name in _EDIT_TOOLS and isinstance(inp, dict) and "file_path" in inp:
                        self._tool_use_to_path[tid] = str(inp["file_path"])
                    if not self._suppress_llm:
                        await self._emit(
                            EventKind.TOOL_CALL_STARTED,
                            {"tool_use_id": tid, "tool": name, "input": inp},
                        )
            return

        if mtype == "user":
            content = ((msg.get("message") or {}).get("content")) or []
            if not isinstance(content, list):
                return
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tid = block.get("tool_use_id", "")
                is_error = bool(block.get("is_error"))
                body = block.get("content", "")
                preview = body if isinstance(body, str) else json.dumps(body)
                await self._emit(
                    EventKind.TOOL_CALL_FINISHED,
                    {"tool_use_id": tid, "is_error": is_error, "preview": preview[:400]},
                )
                if not is_error and tid in self._tool_use_to_path:
                    await self._emit(
                        EventKind.FILE_EDITED,
                        {"path": self._tool_use_to_path.pop(tid)},
                    )
            return

        if mtype == "result":
            await self._emit(
                EventKind.TURN_ENDED,
                {
                    "subtype": msg.get("subtype"),
                    "duration_ms": msg.get("duration_ms"),
                    "total_cost_usd": msg.get("total_cost_usd"),
                    "usage": msg.get("usage"),
                    "is_error": bool(msg.get("is_error")),
                    "result": msg.get("result"),
                },
            )
            return

    async def _emit(self, kind: EventKind, payload: dict[str, Any]) -> None:
        await self._bus.publish(Event(kind=kind, source="claude.adapter", payload=payload))
