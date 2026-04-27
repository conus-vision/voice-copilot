"""Format events + session state into the narration and summary prompts.

The commentator is anchored against three pieces of state, all kept
per-session by `pipeline.SessionContext`:

  1. The user's original question to the agent (sniffed from the request
     body by the proxy, or `None` when we don't know it).
  2. A rolling summary of what the agent has already done *and* what we
     already narrated — produced by a second LLM call after every narration
     so the model doesn't lose the thread over long turns.
  3. The new events that triggered this narration (a fresh thinking chunk,
     a final answer, a file edit, etc.).

`build_narration_user` composes all three into the user-message payload for
the TTS narration call. `build_summary_user` is the payload for the short
summary-update call.
"""

from __future__ import annotations

import json
from typing import Any

from voice_copilot.core.events import Event, EventKind

_MAX_TEXT = 1500
_MAX_BULLETS = 40
_MAX_SUMMARY = 600
_MAX_NARRATION_ECHO = 400


def _extract_question(text: str) -> str:
    """Return only the user's actual question, stripping injected system prompts.

    CLI agents (copilot, aider, etc.) often append their own instructions to
    the user turn ("To suggest changes... MUST return entire file..."). We only
    want the human's original question, which is always the first paragraph.
    """
    # Split on blank lines or on the first occurrence of a markdown code fence
    # or a long line of only dashes/equals (rule separators).
    import re

    # Take everything up to first blank line or ``` or obvious rule-injection marker.
    first = re.split(r"\n\s*\n|```|\n[-=]{4,}", text, maxsplit=1)[0]
    return first.strip()


def _events_hint(events: list[Event]) -> str:
    """Describe what type of content is in the events batch."""
    has_thinking = any(e.kind is EventKind.AGENT_THINKING for e in events)
    has_answer = any(e.kind in (EventKind.AGENT_TEXT, EventKind.TURN_ENDED) for e in events)
    if has_answer and not has_thinking:
        return "финальный ответ агента"
    if has_thinking and not has_answer:
        return "размышления агента (ещё не ответил)"
    if has_thinking and has_answer:
        return "размышления и финальный ответ агента"
    return "действия агента"


def build_narration_user(
    *,
    user_query: str | None,
    summary: str | None,
    events: list[Event],
    style: str = "api",
) -> str:
    """Build the user message for the narration call.

    style="api"  — uses [BRACKET] section headers (works with system/user split).
    style="cli"  — uses plain inline headers (avoids triggering file-search in
                   copilot-cli, which interprets [SECTION] as grep targets).
    """
    formatted = _format_events(events) if events else "(empty)"
    hint = _events_hint(events) if events else "действия агента"
    query_text = ""
    if user_query:
        q = _extract_question(user_query)
        query_text = _trim(q, 400) if q else _trim(user_query, 400)

    if style == "cli":
        # No bracket labels — plain prose headers.
        parts = [
            f"Пользователь спросил: {query_text or '(неизвестно)'}",
            "",
            f"Уже озвучено: {_trim(summary, _MAX_SUMMARY) if summary else '(ничего)'}",
            "",
            f"Новые события ({hint}):",
            formatted,
            "",
            "Ответ (1-2 предложения прозы, без markdown): Агент",
        ]
    else:
        parts = [
            "[USER_QUERY]",
            query_text or "(unknown yet)",
            "",
            "[ALREADY_DONE_AND_SAID]",
            _trim(summary, _MAX_SUMMARY) if summary else "(nothing yet)",
            "",
            "[NEW_EVENTS]",
            formatted,
            "",
            f"[NEW_EVENTS] содержит {hint}. Ответ (1-2 предложения прозы, только по [NEW_EVENTS]):",
        ]
    return "\n".join(parts)


def build_summary_user(
    *,
    prev_summary: str | None,
    events: list[Event],
    narration: str,
    style: str = "api",
) -> str:
    """User message for the summary-update call."""
    formatted = _format_events(events) if events else "(empty)"
    narr_text = _trim(narration, _MAX_NARRATION_ECHO) if narration else "(nothing)"
    prev_text = _trim(prev_summary, _MAX_SUMMARY) if prev_summary else "(empty)"

    if style == "cli":
        parts = [
            f"Предыдущее саммери: {prev_text}",
            "",
            f"Только что произошло:\n{formatted}",
            "",
            f"Только что озвучено пользователю: {narr_text}",
            "",
            "Обновлённое саммери (2-3 предложения прозы, без markdown):",
        ]
    else:
        parts = [
            "[PREVIOUS_SUMMARY]",
            prev_text,
            "",
            "[JUST_HAPPENED]",
            formatted,
            "",
            "[JUST_NARRATED_TO_USER]",
            narr_text,
            "",
            "Саммери (2-3 предложения прозы, без markdown):",
        ]
    return "\n".join(parts)


def _format_events(events: list[Event]) -> str:
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
    except Exception:
        return str(obj)


def _trim(s: Any, limit: int = _MAX_TEXT) -> str:
    if not s:
        return ""
    text = str(s).replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# Back-compat shim: older tests / imports may still reach for `format_events`.
def format_events(events: list[Event]) -> str:
    return _format_events(events)
