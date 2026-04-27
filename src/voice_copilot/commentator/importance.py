"""Classify events by importance and decide whether to narrate.

The commentator receives the whole event stream — most of it is noise (tool
readbacks, token-level deltas, trivial file lookups). This module is the
single place that decides: "does a human want to hear this?".

Keep rules cheap and local. LLM-based importance scoring could slot in
later, but per-event LLM calls would be slow and wasteful.
"""

from __future__ import annotations

from typing import Literal

from voice_copilot.core.config import CommentatorConfig
from voice_copilot.core.events import Event, EventKind

Importance = Literal["low", "normal", "high"]
_ORDER: dict[Importance, int] = {"low": 0, "normal": 1, "high": 2}

#: Tools that are usually too boring to announce mid-turn. The commentator
#: may still mention them if a burst is *mostly* these, but they don't
#: trigger a flush on their own.
_QUIET_TOOLS = {"Read", "Glob", "Grep", "LS", "TodoWrite"}

#: Event.source prefixes that belong to our own plumbing. Errors originating
#: here (STT decode failure, TTS hiccup, WS glitch) must not be narrated —
#: the user doesn't want to hear about our internals. Agent-side errors
#: (adapters, proxies) are still surfaced.
_INTERNAL_SOURCES = ("stt.", "tts.", "audio.", "web.", "hotkey", "tray")


def classify(event: Event, cfg: CommentatorConfig) -> Importance | None:
    """Return importance, or None if the event should be dropped entirely."""
    k = event.kind
    p = event.payload

    if k is EventKind.ERROR:
        if event.source.startswith(_INTERNAL_SOURCES):
            return None
        return "high"

    if k is EventKind.SESSION_STARTED:
        return "normal"
    if k is EventKind.SESSION_ENDED:
        return "low"

    if k is EventKind.AGENT_TEXT:
        text = (p.get("text") or "").strip()
        if not text:
            return None
        return "normal"

    if k is EventKind.AGENT_THINKING:
        if not cfg.speak_thinking:
            return None
        text = (p.get("text") or "").strip()
        if len(text) < 24:
            return None
        # When thinking is explicitly enabled, treat it as normal so it
        # passes through min_importance: normal and triggers the debounce.
        return "normal"

    if k is EventKind.TOOL_CALL_STARTED:
        if not cfg.speak_tool_calls:
            return None
        tool = p.get("tool") or ""
        return "low" if tool in _QUIET_TOOLS else "normal"

    if k is EventKind.TOOL_CALL_FINISHED:
        if bool(p.get("is_error")):
            return "high"
        return None  # success readbacks are noise; only failures matter

    if k is EventKind.FILE_EDITED:
        if not cfg.speak_file_edits:
            return None
        return "high"

    if k is EventKind.TURN_ENDED:
        return "low"

    if k is EventKind.AGENT_AWAITING_INPUT:
        return "high"

    return None


def meets_threshold(importance: Importance, min_importance: Importance) -> bool:
    return _ORDER[importance] >= _ORDER[min_importance]
