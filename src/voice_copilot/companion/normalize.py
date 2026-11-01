"""Turn a CLI's hook payload into canonical bus events.

Claude Code, Codex, Droid, Qwen and the DeepSeek Harness bridge share one hook
vocabulary (`hook_event_name`, `tool_name`, `tool_input`, `tool_response`), so a
single table covers them. An event we don't know maps to nothing: hooks are a
moving target, and a new one must not cost the user their session.
"""

from __future__ import annotations

import json
import re
from typing import Any

from voice_copilot.core.events import EventKind
from voice_copilot.proxy.tool_events import file_path_from_tool

HookEvent = tuple[EventKind, dict[str, Any]]

#: Codex edits through `apply_patch`, whose input names files inside the patch.
_PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)


def _edited_paths(tool: str | None, tool_input: Any) -> list[str]:
    path = file_path_from_tool(tool, tool_input)
    if path:
        return [path]
    if not tool or tool.strip().lower() != "apply_patch":
        return []
    values = tool_input.values() if isinstance(tool_input, dict) else [tool_input]
    patch = "\n".join(v for v in values if isinstance(v, str))
    return [path.strip() for path in _PATCH_FILE.findall(patch)]


def _tool_failed(response: Any) -> bool:
    if not isinstance(response, dict):
        return False
    if response.get("is_error") or response.get("isError") or response.get("success") is False:
        return True
    code = response.get("exit_code", response.get("exitCode"))
    return isinstance(code, int) and code != 0


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def events_from_hook(cli: str, payload: dict[str, Any]) -> list[HookEvent]:
    """The bus events one hook call stands for, in order."""
    event = payload.get("hook_event_name")
    sid = payload.get("session_id")
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    base: dict[str, Any] = {"session_id": sid}
    if payload.get("agent_id"):
        base["subagent"] = True

    if event == "SessionStart":
        return [
            (
                EventKind.SESSION_STARTED,
                {
                    **base,
                    "target": cli,
                    "model": payload.get("model"),
                    "cwd": payload.get("cwd"),
                    "source": payload.get("source"),
                },
            )
        ]
    if event == "SessionEnd":
        return [(EventKind.SESSION_ENDED, {**base, "reason": payload.get("reason")})]
    if event == "UserPromptSubmit":
        text = _as_text(payload.get("prompt"))
        if payload.get("mid_turn"):
            # Typed to a working agent, where the CLI lets a plugin tell: it adds to the task.
            return [(EventKind.USER_MESSAGE, {**base, "text": text, "delivery": "delivered"})]
        return [
            (EventKind.USER_MESSAGE, {**base, "text": text, "delivery": "observed"}),
            (EventKind.TURN_STARTED, dict(base)),
        ]
    if event == "PreToolUse":
        started: dict[str, Any] = {
            **base,
            "tool_use_id": payload.get("tool_use_id"),
            "tool": tool,
            "input": tool_input,
        }
        edits = [
            (EventKind.FILE_EDITED, {**base, "path": path, "via": "tool_call"})
            for path in _edited_paths(tool, tool_input)
        ]
        return [(EventKind.TOOL_CALL_STARTED, started), *edits]
    if event in ("PostToolUse", "PostToolUseFailure"):
        response = payload.get("tool_response", payload.get("error"))
        return [
            (
                EventKind.TOOL_CALL_FINISHED,
                {
                    **base,
                    "tool_use_id": payload.get("tool_use_id"),
                    "tool": tool,
                    "is_error": event == "PostToolUseFailure" or _tool_failed(response),
                    "preview": _as_text(response),
                },
            )
        ]
    if event == "AssistantMessage":
        # Our own, for a plugin that sees the model's words and has no transcript file.
        said: list[HookEvent] = []
        if payload.get("thinking"):
            said.append((EventKind.AGENT_THINKING, {**base, "text": payload["thinking"]}))
        if payload.get("text"):
            said.append((EventKind.AGENT_TEXT, {**base, "text": payload["text"]}))
        return said
    if event == "PluginError":
        return [(EventKind.ERROR, {**base, "where": "plugin", "message": payload.get("message")})]
    if event == "SubagentStop":
        return [(EventKind.TURN_ENDED, {**base, "subagent": True})]
    if event == "Interrupt":
        return [(EventKind.TURN_ENDED, {**base, "final": True, "interrupted": True})]
    if event == "PermissionRequest":
        return [(EventKind.AGENT_AWAITING_INPUT, {**base, "tool": tool, "input": tool_input})]
    if event == "Notification" and payload.get("notification_type") in (
        "permission_prompt",
        "idle_prompt",
    ):
        return [(EventKind.AGENT_AWAITING_INPUT, {**base, "message": payload.get("message")})]
    return []
