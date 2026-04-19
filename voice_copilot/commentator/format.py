"""Turn a batch of bus events into a compact user-message for the LLM.

We give the commentator the last N events as plain bullet points — the model
already knows the canonical event kinds from the system prompt. Keep payloads
trimmed; long tool results explode tokens for no narration benefit.
"""

from __future__ import annotations

import json
from typing import Any

from voice_copilot.core.events import Event, EventKind

_MAX_TEXT = 240
_MAX_BULLETS = 40


def format_events(events: list[Event]) -> str:
    bullets: list[str] = []
    for ev in events[-_MAX_BULLETS:]:
        bullets.append(f"- {_format_one(ev)}")
    return "\n".join(bullets)


def _format_one(ev: Event) -> str:
    k = ev.kind
    p = ev.payload

    if k is EventKind.SESSION_STARTED:
        t = p.get("target") or "agent"
        model = p.get("model") or ""
        return f"session started: target={t} model={model}"

    if k is EventKind.AGENT_TEXT:
        return f"agent said: {_trim(p.get('text'))}"
    if k is EventKind.AGENT_THINKING:
        return f"agent thinking: {_trim(p.get('text'))}"

    if k is EventKind.TOOL_CALL_STARTED:
        tool = p.get("tool") or "?"
        inp = p.get("input")
        return f"tool {tool} started: {_trim(_summarize(inp))}"
    if k is EventKind.TOOL_CALL_FINISHED:
        tool = p.get("tool") or "?"
        err = p.get("is_error")
        tag = "FAILED" if err else "ok"
        return f"tool {tool} {tag}: {_trim(p.get('preview'))}"

    if k is EventKind.FILE_EDITED:
        return f"file edited: {p.get('path')}"

    if k is EventKind.TURN_ENDED:
        return "turn ended"
    if k is EventKind.AGENT_AWAITING_INPUT:
        return "agent awaiting user input"

    if k is EventKind.ERROR:
        return f"error: {_trim(p.get('message'))}"

    return f"{k.value}: {_trim(_summarize(p))}"


def _summarize(obj: Any) -> str:
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return str(obj)


def _trim(s: str | None) -> str:
    if not s:
        return ""
    s = str(s).replace("\n", " ").strip()
    return s if len(s) <= _MAX_TEXT else s[: _MAX_TEXT - 1] + "…"
