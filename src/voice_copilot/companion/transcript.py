"""Read what the agent said between tool calls out of its session transcript.

Hooks name tools and files, but carry the agent's own words only at the very
end of a turn. Its reasoning-as-it-goes lives in the transcript the hook payload
points at, and that is what makes the narration about *why*, not just *what*.

The formats are the CLIs' private files, so unknown lines are skipped and a
format change only costs the narration its detail: hooks keep working.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from voice_copilot.core.events import EventKind

Said = tuple[EventKind, str]


def _from_claude(line: dict[str, Any]) -> list[Said]:
    if line.get("isSidechain"):
        return []  # A sub-agent's own thread; its result reaches the main one.
    message = line.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    said: list[Said] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and block.get("text"):
            said.append((EventKind.AGENT_TEXT, str(block["text"])))
        # Empty unless the user enabled thinking summaries: only a signature is stored.
        elif block.get("type") == "thinking" and block.get("thinking"):
            said.append((EventKind.AGENT_THINKING, str(block["thinking"])))
    return said


def _from_codex(line: dict[str, Any]) -> list[Said]:
    item = line.get("payload")
    if not isinstance(item, dict):
        return []
    if item.get("type") == "message" and item.get("role") == "assistant":
        return [
            (EventKind.AGENT_TEXT, str(part["text"]))
            for part in item.get("content") or []
            if isinstance(part, dict) and part.get("text")
        ]
    if item.get("type") == "reasoning":
        # Empty unless `model_reasoning_summary` is set: the rest is encrypted.
        return [
            (EventKind.AGENT_THINKING, str(part["text"]))
            for part in item.get("summary") or []
            if isinstance(part, dict) and part.get("text")
        ]
    return []


def _codex_command_end(line: dict[str, Any]) -> tuple[str, int, str] | None:
    """(tool id, exit code, output) of a finished command, from a Codex `item_completed` line."""
    payload = line.get("payload")
    item = payload.get("item") if isinstance(payload, dict) else None
    if not isinstance(item, dict) or item.get("type") != "CommandExecution":
        return None
    tool_id, code, status = item.get("id"), item.get("exit_code"), item.get("status")
    if not isinstance(tool_id, str):
        return None
    output = str(item.get("aggregated_output") or "")
    if isinstance(code, int):
        return tool_id, code, output
    return (tool_id, 1, output) if status == "failed" else None


class TranscriptTail:
    """Yields only what was appended since the last read."""

    def __init__(self, path: Path, *, from_start: bool) -> None:
        self._path = path
        self._offset = 0
        #: Codex's hooks get a command's output but not how it ended: that is only here.
        self.exit_codes: dict[str, int] = {}
        #: A command that outlasts Codex's wait for it gets no `PostToolUse` either.
        self.outputs: dict[str, str] = {}
        if not from_start:
            try:
                self._offset = path.stat().st_size
            except OSError:
                self._offset = 0

    def read_new(self) -> list[Said]:
        try:
            with self._path.open("rb") as f:
                f.seek(self._offset)
                data = f.read()
        except OSError:
            return []
        # A line still being written has no newline yet: leave it for next time.
        end = data.rfind(b"\n")
        if end < 0:
            return []
        self._offset += end + 1
        said: list[Said] = []
        for raw in data[:end].splitlines():
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(line, dict):
                continue
            if line.get("type") == "assistant":
                said.extend(_from_claude(line))
            elif line.get("type") == "response_item":
                said.extend(_from_codex(line))
            elif line.get("type") == "event_msg":
                ended = _codex_command_end(line)
                if ended is not None:
                    self.exit_codes[ended[0]] = ended[1]
                    self.outputs[ended[0]] = ended[2]
        return said
