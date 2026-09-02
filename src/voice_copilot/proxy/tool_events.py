"""Turn a provider's completed tool call into bus events.

Both SSE parsers converge on the same shape once the arguments have finished
streaming: a tool name plus a decoded JSON input. This module publishes that
as `TOOL_CALL_STARTED` and, for the file-writing tools, a derived
`FILE_EDITED` — the proxy never sees tool *results*, so an edit is only ever
visible through the call that requests it.

Why it matters: the tool input is where the nouns live (which file, which
command, which pattern). Without it the commentator can only say "the agent
used a tool", which is exactly the narration nobody wants to hear.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import Event, EventKind

log = logging.getLogger(__name__)

#: Tools whose invocation means a file is being written. Names are matched
#: case-insensitively across CLIs (Claude Code, Codex, aider, OpenHands …).
_EDITING_TOOLS = {
    "edit",
    "write",
    "multiedit",
    "notebookedit",
    "create_file",
    "update_file",
    "str_replace_editor",
    "str_replace_based_edit_tool",
    "apply_patch",
    "write_file",
    "edit_file",
}

#: Input keys that carry a path, in priority order.
_PATH_KEYS = (
    "file_path",
    "notebook_path",
    "filePath",
    "path",
    "filename",
    "file",
    "target_file",
)


def decode_tool_input(raw: str | None) -> Any:
    """Decode accumulated `input_json_delta` / `arguments` text.

    Returns the parsed object, or the raw string when the stream was cut
    mid-JSON — a partial blob still names the file more often than not.
    """
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text


def file_path_from_tool(tool: str | None, tool_input: Any) -> str | None:
    """Path this tool call writes to, or None if it isn't an edit."""
    if not tool or not isinstance(tool_input, dict):
        return None
    if tool.strip().lower().replace("-", "_") not in _EDITING_TOOLS:
        return None
    for key in _PATH_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


async def publish_tool_call(
    bus: EventBus,
    *,
    source: str,
    session_id: str | None,
    tool: str | None,
    tool_input: Any,
    tool_use_id: str | None = None,
) -> None:
    """Publish TOOL_CALL_STARTED (+ FILE_EDITED for writes)."""

    async def _emit(kind: EventKind, payload: dict[str, Any]) -> None:
        if session_id is not None:
            payload = {**payload, "session_id": session_id}
        await bus.publish(Event(kind=kind, source=source, payload=payload))

    await _emit(
        EventKind.TOOL_CALL_STARTED,
        {"tool_use_id": tool_use_id, "tool": tool, "input": tool_input},
    )
    path = file_path_from_tool(tool, tool_input)
    if path:
        await _emit(EventKind.FILE_EDITED, {"path": path, "via": "tool_call"})
