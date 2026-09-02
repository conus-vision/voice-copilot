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
#: How many concrete names (files, commands, patterns) to spell out per
#: activity bullet before switching to "(+N more)". The user asked to hear
#: what is going on in broad strokes, not a manifest.
_MAX_NAMES = 3

#: tool name (lowercased, dashes → underscores) → activity bucket.
_TOOL_BUCKETS: dict[str, str] = {
    "read": "read",
    "view": "read",
    "cat": "read",
    "open": "read",
    "notebookread": "read",
    "read_file": "read",
    "glob": "searched",
    "grep": "searched",
    "ls": "searched",
    "list": "searched",
    "list_dir": "searched",
    "find": "searched",
    "search": "searched",
    "codebase_search": "searched",
    "bash": "ran",
    "shell": "ran",
    "run": "ran",
    "exec": "ran",
    "terminal": "ran",
    "powershell": "ran",
    "run_command": "ran",
    "edit": "edited",
    "write": "edited",
    "multiedit": "edited",
    "notebookedit": "edited",
    "create_file": "edited",
    "update_file": "edited",
    "write_file": "edited",
    "edit_file": "edited",
    "apply_patch": "edited",
    "str_replace_editor": "edited",
    "str_replace_based_edit_tool": "edited",
    "webfetch": "looked up",
    "websearch": "looked up",
    "fetch": "looked up",
    "browse": "looked up",
    "task": "delegated to",
    "agent": "delegated to",
}

#: Input keys worth naming, per bucket, in priority order.
_LABEL_KEYS: dict[str, tuple[str, ...]] = {
    "read": ("file_path", "notebook_path", "path", "filename", "file", "target_file"),
    "edited": ("file_path", "notebook_path", "path", "filename", "file", "target_file"),
    "searched": ("pattern", "query", "glob", "path", "regex"),
    "ran": ("command", "cmd", "script"),
    "looked up": ("url", "query", "prompt"),
    "delegated to": ("description", "subagent_type", "prompt"),
}


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
    opening: bool = False,
) -> str:
    """Build the user message for the narration call.

    style="api"  — uses [BRACKET] section headers (works with system/user split).
    style="cli"  — uses plain inline headers (avoids triggering file-search in
                   copilot-cli, which interprets [SECTION] as grep targets).
    opening      — nothing has been said about this question yet, so the line
                   has to set the scene: name the task, then the work so far.
    """
    formatted = _format_events(events) if events else "(empty)"
    hint = _events_hint(events) if events else "действия агента"
    query_text = ""
    if user_query:
        q = _extract_question(user_query)
        query_text = _trim(q, 400) if q else _trim(user_query, 400)

    if style == "cli":
        # No bracket labels — plain prose headers.
        task = (
            "Это первая реплика по этому запросу: сначала одной фразой назови задачу, "
            "потом коротко — что уже сделано. Ответ (1-2 предложения прозы, без markdown):"
            if opening
            else "Ответ (1-2 предложения прозы, без markdown):"
        )
        parts = [
            f"Пользователь спросил: {query_text or '(неизвестно)'}",
            "",
            f"Уже озвучено: {_trim(summary, _MAX_SUMMARY) if summary else '(ничего)'}",
            "",
            f"Новые события ({hint}):",
            formatted,
            "",
            task,
        ]
    else:
        task = (
            f"[NEW_EVENTS] содержит {hint}. Это первая реплика по этому запросу: сначала "
            "одной фразой назови задачу из [USER_QUERY], потом коротко — что уже сделано. "
            "Ответ (1-2 предложения прозы):"
            if opening
            else f"[NEW_EVENTS] содержит {hint}. "
            "Ответ (1-2 предложения прозы, только по [NEW_EVENTS]):"
        )
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
            task,
        ]
    return "\n".join(parts)


def build_supervisor_user(
    *,
    user_query: str | None,
    summary: str | None,
    history: list[str],
    reason: str,
    style: str = "api",
) -> str:
    """Build the user message for a supervisor checkpoint.

    Unlike narration this carries the running transcript, not just the last
    batch: whether the agent is on track is a question about the whole turn.
    """
    question = _extract_question(user_query) if user_query else None
    goal = _trim(question or user_query or "", 600) or "(unknown)"
    memo = summary or "(nothing yet)"
    transcript = "\n".join(history[-80:]) or "(no events yet)"
    if style == "cli":
        return (
            f"Цель пользователя: {goal}\n\n"
            f"Что сделано (саммари): {memo}\n\n"
            f"Повод для проверки: {reason}\n\n"
            f"Последние события:\n{transcript}\n\n"
            "Вердикт (первая строка OK / WARN / STOP, дальше 1-2 предложения):"
        )
    return (
        f"[GOAL]\n{goal}\n\n"
        f"[SUMMARY_SO_FAR]\n{memo}\n\n"
        f"[CHECKPOINT_REASON]\n{reason}\n\n"
        f"[RECENT_EVENTS]\n{transcript}\n\n"
        "Verdict (first line OK / WARN / STOP, then 1-2 sentences):"
    )


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
    """Render the batch as bullets, collapsing runs of tool activity.

    A turn can contain twenty Read calls; twenty bullets would drown the
    thinking and the answer, and their raw inputs (a whole new file body for
    an Edit) would eat the context window. Consecutive tool events collapse
    into one line per activity — "read 6 files: a.py, b.py (+4 more)" — so
    the narrator can mention what is going on without reciting it. Failures
    stay on their own line: they are the part worth hearing verbatim.
    """
    bullets: list[str] = []
    run: list[Event] = []

    def close_run() -> None:
        if run:
            bullets.extend(_activity_bullets(run))
            run.clear()

    for ev in events[-_MAX_BULLETS:]:
        if _is_activity(ev):
            run.append(ev)
            continue
        close_run()
        bullets.append(_format_one(ev))
    close_run()
    return "\n".join(f"- {b}" for b in bullets)


def _is_activity(ev: Event) -> bool:
    """Tool traffic we collapse. Failures are excluded — they get their own line."""
    if ev.kind is EventKind.FILE_EDITED:
        return True
    if ev.kind is EventKind.TOOL_CALL_STARTED:
        return True
    if ev.kind is EventKind.TOOL_CALL_FINISHED:
        return not ev.payload.get("is_error")
    return False


def _activity_bullets(events: list[Event]) -> list[str]:
    """One bullet per activity bucket, in first-seen order."""
    buckets: dict[str, list[str]] = {}
    counts: dict[str, int] = {}
    for ev in events:
        bucket, label = _bucket_and_label(ev)
        if bucket is None:
            continue
        counts[bucket] = counts.get(bucket, 0) + 1
        names = buckets.setdefault(bucket, [])
        if label and label not in names:
            names.append(label)

    bullets: list[str] = []
    for bucket, names in buckets.items():
        count = counts[bucket]
        shown = names[:_MAX_NAMES]
        hidden = len(names) - len(shown)
        head = f"{bucket} {count}x" if count > 1 and not shown else bucket
        if shown:
            tail = ", ".join(shown)
            if hidden > 0:
                tail += f" (+{hidden} more)"
            bullets.append(f"{head}: {tail}")
        else:
            bullets.append(head)
    return bullets


def _bucket_and_label(ev: Event) -> tuple[str | None, str | None]:
    p = ev.payload
    if ev.kind is EventKind.FILE_EDITED:
        return "edited", _short_path(p.get("path"))

    tool = str(p.get("tool") or "").strip()
    if not tool:
        return None, None
    key = tool.lower().replace("-", "_")
    bucket = _TOOL_BUCKETS.get(key)
    if bucket is None:
        # Unknown tool: name it as-is so the narrator can still say what ran.
        return f"used {tool}", None

    tool_input = p.get("input")
    label: str | None = None
    if isinstance(tool_input, dict):
        for field in _LABEL_KEYS.get(bucket, ()):
            value = tool_input.get(field)
            if isinstance(value, str) and value.strip():
                label = value.strip()
                break
    elif isinstance(tool_input, str) and tool_input.strip():
        label = tool_input.strip()

    if label is None:
        return bucket, None
    if bucket in ("read", "edited"):
        return bucket, _short_path(label)
    return bucket, _trim(label, 70)


def _short_path(path: Any) -> str | None:
    """Keep the last two segments — enough to locate, short enough to speak."""
    if not isinstance(path, str) or not path.strip():
        return None
    parts = [seg for seg in path.replace("\\", "/").split("/") if seg]
    if not parts:
        return None
    return "/".join(parts[-2:])


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
        _, label = _bucket_and_label(ev)
        return f"tool {tool} started: {label}" if label else f"tool {tool} started"
    if k is EventKind.TOOL_CALL_FINISHED:
        tool = p.get("tool") or "?"
        err = p.get("is_error")
        tag = "FAILED" if err else "ok"
        return f"tool {tool} {tag}: {_trim(p.get('preview'))}"

    if k is EventKind.FILE_EDITED:
        return f"file edited: {p.get('path')}"

    if k is EventKind.TURN_ENDED:
        # A model response that ends by calling tools is not the end of the
        # agent's turn — the next request continues it. Naming it "turn ended"
        # had the narrator announcing completion after every tool round.
        if p.get("final") is False:
            return "step done, continuing with tools"
        if p.get("subagent"):
            return "sub-agent finished (main task continues)"
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
