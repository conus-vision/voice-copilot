"""Extract the latest user message from a provider request body.

The commentator needs to know *what the user asked the agent* so it can
anchor every narration against the original question. Each provider shapes
its request differently; this module normalises them.

Safe by design: returns `None` on any malformed / unexpected input, never
raises.
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)


def extract_user_query(body: bytes, *, provider: str, path: str) -> str | None:
    """Return the most recent user-role message text, or None if absent."""
    if not body:
        return None
    try:
        doc = json.loads(body.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(doc, dict):
        return None

    if provider == "anthropic":
        return _last_user_from_messages(doc.get("messages"))

    if provider == "gemini":
        return _last_user_from_gemini(doc.get("contents"))

    if provider == "ollama" and path.startswith("api/"):
        if path.startswith("api/chat"):
            return _last_user_from_messages(doc.get("messages"))
        if path.startswith("api/generate"):
            prompt = doc.get("prompt")
            return prompt if isinstance(prompt, str) and prompt.strip() else None

    # OpenAI-compatible (openai, openrouter, groq, mistral, ollama/v1)
    return _last_user_from_messages(doc.get("messages"))


def _last_user_from_messages(messages: Any) -> str | None:
    if not isinstance(messages, list):
        return None
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        text = _content_to_text(content)
        if text:
            return text
    return None


def _last_user_from_gemini(contents: Any) -> str | None:
    if not isinstance(contents, list):
        return None
    for item in reversed(contents):
        if not isinstance(item, dict):
            continue
        if item.get("role") and item.get("role") != "user":
            continue
        parts = item.get("parts")
        if not isinstance(parts, list):
            continue
        text = " ".join(
            p.get("text", "")
            for p in parts
            if isinstance(p, dict) and isinstance(p.get("text"), str)
        ).strip()
        if text:
            return text
    return None


def _content_to_text(content: Any) -> str | None:
    """Messages API content can be a string or a list of typed blocks."""
    if isinstance(content, str):
        s = content.strip()
        return s or None
    if isinstance(content, list):
        pieces: list[str] = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                pieces.append(block["text"])
        joined = " ".join(pieces).strip()
        return joined or None
    return None
