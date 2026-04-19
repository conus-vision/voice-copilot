"""Codex CLI adapter (`codex exec --json`).

Codex uses a `ThreadEvent` JSON stream:

  * `thread.started`   → SESSION_STARTED
  * `turn.started`     → TURN_STARTED
  * `turn.completed`   → TURN_ENDED
  * `turn.failed`      → TURN_ENDED (is_error)
  * `item.started`     → TOOL_CALL_STARTED (for command_execution / mcp_tool_call / web_search)
  * `item.completed`   → AGENT_TEXT / AGENT_THINKING / TOOL_CALL_FINISHED / FILE_EDITED
  * `error`            → ERROR

Reference: https://developers.openai.com/codex/noninteractive

Caveat — `codex exec` is one-shot: a single turn per process. True multi-turn
with queued user messages needs `codex proto` (proto stream JSON-RPC). Here
we wire the single-turn case correctly; the dialog manager (Э10) will
orchestrate follow-up turns by re-spawning with thread resumption.
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


class CodexAdapter(CLIAdapter):
    name = "codex"
    quick_aside = QuickAsideCapability.QUEUE

    #: Flags that make `codex exec` behave sensibly under a non-interactive wrapper.
    #: The user can override or extend via `extra_args`.
    DEFAULT_ARGS: tuple[str, ...] = ("--skip-git-repo-check",)

    def __init__(
        self,
        bus: EventBus,
        binary: str = "codex",
        extra_args: list[str] | None = None,
        sandbox: str | None = "workspace-write",
        env: dict[str, str] | None = None,
    ) -> None:
        self._bus = bus
        self._binary = binary
        extras = list(self.DEFAULT_ARGS)
        if sandbox:
            extras += ["--sandbox", sandbox]
        if extra_args:
            extras += extra_args
        self._extra_args = extras
        self._env = env or {}
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._thread_id: str | None = None
        self._pending: list[str] = []

    async def start(self, initial_prompt: str | None = None) -> None:
        if shutil.which(self._binary) is None:
            raise RuntimeError(
                f"`{self._binary}` not found in PATH. "
                f"Install Codex CLI: https://github.com/openai/codex"
            )
        if not initial_prompt:
            raise RuntimeError("codex exec needs an initial prompt; pass `-p '…'`.")

        argv = [self._binary, "exec", "--json", *self._extra_args, initial_prompt]
        log.info("spawning %s", " ".join(argv))
        merged_env = {**os.environ, **self._env} if self._env else None
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
        )
        self._reader_task = asyncio.create_task(self._read_stdout(), name="codex.stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name="codex.stderr")

    async def send_user_message(self, text: str, *, urgent: bool = False) -> None:
        # `codex exec` doesn't accept follow-up messages. Queue for Э10 to pick up
        # and spawn a new turn with thread resumption.
        self._pending.append(text)
        await self._emit(
            EventKind.USER_MESSAGE,
            {"text": text, "urgent": urgent, "delivery": "pending-next-turn"},
        )
        log.info("codex user message queued (%d pending)", len(self._pending))

    async def stop(self) -> None:
        for t in (self._reader_task, self._stderr_task):
            if t is not None:
                t.cancel()
        if self._proc is not None:
            try:
                if self._proc.stdin is not None and not self._proc.stdin.is_closing():
                    self._proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        await self._emit(EventKind.SESSION_ENDED, {"thread_id": self._thread_id})

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
                log.warning("non-json codex stdout: %r", line[:200])
                continue
            try:
                await self._handle_event(msg)
            except Exception:  # noqa: BLE001
                log.exception("failed to handle codex event")

    async def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break
            log.info("codex stderr: %s", line.decode("utf-8", errors="replace").rstrip())

    # ------------------------------------------------------------------ dispatch

    async def _handle_event(self, msg: dict[str, Any]) -> None:
        etype = msg.get("type")

        if etype == "thread.started":
            self._thread_id = msg.get("thread_id")
            await self._emit(
                EventKind.SESSION_STARTED,
                {"target": "codex", "thread_id": self._thread_id},
            )
            return

        if etype == "turn.started":
            await self._emit(EventKind.TURN_STARTED, {"thread_id": self._thread_id})
            return

        if etype in ("turn.completed", "turn.failed"):
            usage = msg.get("usage") or {}
            await self._emit(
                EventKind.TURN_ENDED,
                {
                    "is_error": etype == "turn.failed",
                    "input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "cached_input_tokens": usage.get("cached_input_tokens"),
                },
            )
            return

        if etype == "item.started":
            await self._on_item(msg, phase="started")
            return
        if etype == "item.updated":
            # Most items only get a single completion event; skip updates to avoid
            # spamming the commentator. Dialog manager can opt-in later if needed.
            return
        if etype == "item.completed":
            await self._on_item(msg, phase="completed")
            return

        if etype == "error":
            await self._emit(EventKind.ERROR, {"message": msg.get("message"), "raw": msg})
            return

    async def _on_item(self, msg: dict[str, Any], *, phase: str) -> None:
        item = msg.get("item") or {}
        itype = item.get("item_type") or item.get("type")
        item_id = item.get("id") or item.get("item_id")

        if itype == "agent_message" and phase == "completed":
            await self._emit(EventKind.AGENT_TEXT, {"text": item.get("text", "")})
            return

        if itype == "reasoning" and phase == "completed":
            await self._emit(
                EventKind.AGENT_THINKING,
                {"text": item.get("summary") or item.get("text", "")},
            )
            return

        if itype == "command_execution":
            if phase == "started":
                await self._emit(
                    EventKind.TOOL_CALL_STARTED,
                    {"tool_use_id": item_id, "tool": "Bash", "input": {"cmd": item.get("command")}},
                )
            else:
                await self._emit(
                    EventKind.TOOL_CALL_FINISHED,
                    {
                        "tool_use_id": item_id,
                        "tool": "Bash",
                        "is_error": (item.get("exit_code") or 0) != 0,
                        "exit_code": item.get("exit_code"),
                        "preview": (item.get("aggregated_output") or "")[:400],
                    },
                )
            return

        if itype == "file_change" and phase == "completed":
            for change in item.get("changes") or []:
                path = change.get("path") or change.get("file_path")
                if path:
                    await self._emit(EventKind.FILE_EDITED, {"path": path})
            return

        if itype in ("mcp_tool_call", "collab_tool_call"):
            if phase == "started":
                await self._emit(
                    EventKind.TOOL_CALL_STARTED,
                    {
                        "tool_use_id": item_id,
                        "tool": item.get("tool") or itype,
                        "server": item.get("server"),
                        "input": item.get("arguments"),
                    },
                )
            else:
                await self._emit(
                    EventKind.TOOL_CALL_FINISHED,
                    {
                        "tool_use_id": item_id,
                        "tool": item.get("tool") or itype,
                        "is_error": bool(item.get("error")),
                        "preview": (str(item.get("result") or "")[:400]),
                    },
                )
            return

        if itype == "web_search":
            if phase == "started":
                await self._emit(
                    EventKind.TOOL_CALL_STARTED,
                    {"tool_use_id": item_id, "tool": "WebSearch", "input": {"query": item.get("query")}},
                )
            else:
                await self._emit(
                    EventKind.TOOL_CALL_FINISHED,
                    {"tool_use_id": item_id, "tool": "WebSearch"},
                )
            return

        if itype == "error":
            await self._emit(EventKind.ERROR, {"message": item.get("message"), "raw": item})
            return

        # todo_list and unknown items — fall through silently.

    async def _emit(self, kind: EventKind, payload: dict[str, Any]) -> None:
        await self._bus.publish(Event(kind=kind, source="codex.adapter", payload=payload))
