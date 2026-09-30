"""Read the hook payloads of coding CLIs and answer them in their own format.

Most coding CLIs copied Claude Code's hook format: one JSON object on stdin
(or in an HTTP POST) named by ``hook_event_name``, and a JSON reply that can
deny a tool call, add context for the model or keep a turn going. Three
families cover the CLIs Voice Copilot integrates with:

* ``claude``: Claude Code and the CLIs that reuse its format (Qwen Code,
  Codex, Droid, Kimi, Grok Build, OpenHands, Auggie, Goose).
* ``gemini``: Gemini CLI, same idea with its own event names.
* ``copilot``: GitHub Copilot CLI, camelCase events whose payload does not
  name the event, so the forwarder passes it along.

`parse` turns a payload into a `HookCall` with one canonical event name;
`Reply` builders render a decision back into the dialect. Field names are read
defensively (snake_case and camelCase) because only the Claude format was
checked against a live CLI; the others follow their published docs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# Canonical events, named after Claude Code's.
SESSION_START = "SessionStart"
SESSION_END = "SessionEnd"
USER_PROMPT = "UserPromptSubmit"
PRE_TOOL = "PreToolUse"
POST_TOOL = "PostToolUse"
POST_TOOL_FAILURE = "PostToolUseFailure"
MESSAGE = "MessageDisplay"
STOP = "Stop"
STOP_FAILURE = "StopFailure"
SUBAGENT_STOP = "SubagentStop"
NOTIFICATION = "Notification"
PERMISSION_REQUEST = "PermissionRequest"
INTERRUPT = "Interrupt"

DIALECTS = ("claude", "gemini", "copilot")

_GEMINI_EVENTS = {
    "SessionStart": SESSION_START,
    "SessionEnd": SESSION_END,
    "BeforeAgent": USER_PROMPT,
    "AfterAgent": STOP,
    "BeforeTool": PRE_TOOL,
    "AfterTool": POST_TOOL,
    "Notification": NOTIFICATION,
}

_COPILOT_EVENTS = {
    "sessionstart": SESSION_START,
    "sessionend": SESSION_END,
    "userpromptsubmitted": USER_PROMPT,
    "pretooluse": PRE_TOOL,
    "posttooluse": POST_TOOL,
    "posttoolusefailure": POST_TOOL_FAILURE,
    "agentstop": STOP,
    "subagentstop": SUBAGENT_STOP,
    "erroroccurred": STOP_FAILURE,
    "notification": NOTIFICATION,
    "permissionrequest": PERMISSION_REQUEST,
}

#: Claude-format events that other CLIs spell differently.
_CLAUDE_ALIASES = {
    "PromptSubmit": USER_PROMPT,  # Auggie
    "StopCancelled": INTERRUPT,  # Grok Build
}


def _first(body: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = body.get(key)
        if value not in (None, ""):
            return value
    return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _maybe_json(value: Any) -> Any:
    """Copilot sends tool arguments as a JSON string."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in ("{", "["):
            try:
                return json.loads(stripped)
            except ValueError:
                return value
    return value


@dataclass
class HookCall:
    """One hook invocation, reduced to what Voice Copilot acts on."""

    dialect: str
    cli: str
    event: str
    native_event: str
    session_id: str
    cwd: str | None
    body: dict[str, Any] = field(repr=False)

    # Filled per event; empty when the event does not carry it.
    prompt: str = ""
    tool: str = ""
    tool_input: Any = None
    tool_use_id: str | None = None
    tool_output: str = ""
    tool_failed: bool = False
    text: str = ""
    message_id: str | None = None
    final: bool = False
    notice: str = ""
    notice_type: str = ""
    active_stop_hook: bool = False


def parse(dialect: str, cli: str, body: dict[str, Any], event: str | None = None) -> HookCall:
    """Normalize one hook payload. `event` overrides the name in the body."""
    native = event or _text(_first(body, "hook_event_name", "hookEventName", "event")) or ""
    if dialect == "gemini":
        canonical = _GEMINI_EVENTS.get(native, native)
    elif dialect == "copilot":
        canonical = _COPILOT_EVENTS.get(native.replace("_", "").lower(), native)
    else:
        canonical = _CLAUDE_ALIASES.get(native, native)

    session = _text(_first(body, "session_id", "sessionId", "conversation_id", "thread_id"))
    cwd = _first(body, "cwd", "workspace", "project_dir")
    call = HookCall(
        dialect=dialect,
        cli=cli,
        event=canonical,
        native_event=native,
        session_id=session,
        cwd=cwd if isinstance(cwd, str) else None,
        body=body,
    )

    if canonical in (USER_PROMPT, SESSION_START):
        call.prompt = _text(_first(body, "prompt", "initialPrompt", "user_prompt"))
    if canonical in (PRE_TOOL, POST_TOOL, POST_TOOL_FAILURE, PERMISSION_REQUEST):
        call.tool = _text(_first(body, "tool_name", "toolName", "tool"))
        call.tool_input = _maybe_json(_first(body, "tool_input", "toolArgs", "tool_args", "input"))
        use_id = _first(body, "tool_use_id", "toolUseId", "tool_call_id", "toolCallId")
        call.tool_use_id = _text(use_id) or None
    if canonical in (POST_TOOL, POST_TOOL_FAILURE):
        _read_tool_result(call, body)
    if canonical == MESSAGE:
        call.text = _text(_first(body, "delta", "displayed_text", "text", "message"))
        call.message_id = _text(_first(body, "message_id", "messageId")) or None
        call.final = bool(_first(body, "final", "is_final"))
    if canonical in (STOP, SUBAGENT_STOP):
        call.text = _text(
            _first(body, "last_assistant_message", "prompt_response", "response", "lastMessage")
        )
        call.active_stop_hook = bool(_first(body, "stop_hook_active", "stopHookActive"))
    if canonical == SESSION_END:
        call.notice = _text(_first(body, "reason", "end_reason"))
    if canonical in (NOTIFICATION, PERMISSION_REQUEST, STOP_FAILURE):
        call.notice = _text(_first(body, "message", "error", "details", "title"))
        error = body.get("error")
        if isinstance(error, dict):
            call.notice = _text(error.get("message") or error)
        call.notice_type = _text(_first(body, "notification_type", "notificationType", "type"))
    return call


def _read_tool_result(call: HookCall, body: dict[str, Any]) -> None:
    result = _first(body, "tool_response", "toolResult", "tool_result", "result", "output")
    failed = call.event == POST_TOOL_FAILURE
    if isinstance(result, dict):
        kind = _text(result.get("resultType") or result.get("status")).lower()
        if kind in ("failure", "error", "denied", "rejected"):
            failed = True
        if result.get("is_error") or result.get("isError") or result.get("interrupted"):
            failed = True
        result = (
            _first(
                result,
                "textResultForLlm",
                "stderr",
                "stdout",
                "error",
                "content",
                "output",
                "message",
            )
            or result
        )
    error = _first(body, "error", "error_message")
    if error is not None:
        failed = True
        result = error if result in (None, "") else result
    call.tool_output = _text(result)
    call.tool_failed = failed


# ---------------------------------------------------------------- replies


def deny(call: HookCall, reason: str, *, halt: bool = False) -> dict[str, Any]:
    """Refuse the tool call; with `halt`, also end the agent's turn."""
    if call.dialect == "gemini":
        reply: dict[str, Any] = {"decision": "deny", "reason": reason}
    elif call.dialect == "copilot":
        reply = {"permissionDecision": "deny", "permissionDecisionReason": reason}
    else:
        reply = {
            "hookSpecificOutput": {
                "hookEventName": call.native_event or PRE_TOOL,
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    if halt and call.dialect != "copilot":
        reply["continue"] = False
        reply["stopReason"] = reason
    return reply


def halt(call: HookCall, reason: str) -> dict[str, Any]:
    """End the agent's turn at any hook that is not a tool gate."""
    if call.dialect == "copilot":
        return {}
    return {"continue": False, "stopReason": reason}


def add_context(call: HookCall, text: str) -> dict[str, Any] | None:
    """Hand the model extra context at this hook, or None if the hook can't carry it."""
    if call.dialect == "copilot":
        if call.event in (POST_TOOL, NOTIFICATION):
            return {"additionalContext": text}
        return None
    if call.event not in (POST_TOOL, USER_PROMPT, SESSION_START):
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": call.native_event or call.event,
            "additionalContext": text,
        }
    }


def keep_going(call: HookCall, text: str) -> dict[str, Any] | None:
    """At the end of a turn, make the agent continue with `text` as its next input."""
    if call.event != STOP:
        return None
    if call.dialect == "gemini":
        return {"decision": "deny", "reason": text}
    return {"decision": "block", "reason": text}
