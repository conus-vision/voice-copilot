"""What did the human actually ask? Shared by everything that must not
mistake a CLI's own side requests for a new question.

Every request through the proxy carries the whole conversation, and CLIs
inject their own blocks into the user turn — and make requests purely for
themselves (title generation, quota probes, the safety classifier) over the
same connection. The narrator, the TTS driver and the panel all need the same
answer to "is this a new human query?", so the rule lives in one place.
"""

from __future__ import annotations

import re

_SCAFFOLD_BLOCKS = re.compile(
    r"<(system-reminder|recommended_plugins|transcript|session|command-name|command-message|local-command-stdout)>"
    r".*?</\1>",
    re.DOTALL | re.IGNORECASE,
)

#: Markers of a request the CLI made for itself, not for the user: title
#: generation, quota probes, the safety classifier. They ride the same
#: connection as the real conversation, so the proxy sees them too.
_SCAFFOLD_MARKERS = (
    "write the title in the predominant language",
    "respond with <severity>",
    "your entire response must begin with <block>",
    "stage 1 does not apply user intent",
    "analyze if this message indicates a new conversation topic",
    "please write a 5-10 word title",
    # codex: "Generate a concise, single-line task title of at most 36 characters…"
    "single-line task title",
)


def clean_user_query(text: str) -> str | None:
    """The human's actual question, or None if this was the CLI talking to itself.

    Every request through the proxy carries the whole conversation, and CLIs
    inject their own blocks into the user turn. What survives stripping is the
    part a human typed — and it stays stable across the many requests one turn
    produces, so the narrator keeps its anchor instead of resetting on each
    tool round-trip.
    """
    stripped = _SCAFFOLD_BLOCKS.sub(" ", text).strip()
    if not stripped:
        return None
    lowered = stripped.lower()
    if any(marker in lowered for marker in _SCAFFOLD_MARKERS):
        return None
    if lowered in ("quota", "ping", "test"):
        return None
    return stripped
