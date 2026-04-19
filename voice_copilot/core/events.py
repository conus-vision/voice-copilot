"""Canonical event types flowing through the internal bus.

Adapters normalise vendor-specific CLI events into these; consumers
(commentator, dialog manager, web UI) subscribe to them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


class EventKind(StrEnum):
    SESSION_STARTED = "session.started"
    SESSION_ENDED = "session.ended"

    AGENT_TEXT = "agent.text"
    AGENT_THINKING = "agent.thinking"
    AGENT_AWAITING_INPUT = "agent.awaiting_input"
    TURN_STARTED = "turn.started"
    TURN_ENDED = "turn.ended"

    TOOL_CALL_STARTED = "tool.call.started"
    TOOL_CALL_FINISHED = "tool.call.finished"
    FILE_EDITED = "file.edited"

    USER_SPEAK_REQUESTED = "user.speak.requested"
    USER_MESSAGE = "user.message"
    USER_INTERRUPT = "user.interrupt"
    USER_PAUSE_TOGGLE = "user.pause.toggle"

    AGENT_PAUSED = "agent.paused"
    AGENT_RESUMED = "agent.resumed"

    COMMENTATOR_UTTERANCE = "commentator.utterance"
    TTS_STARTED = "tts.started"
    TTS_FINISHED = "tts.finished"

    ERROR = "error"


Urgency = Literal["low", "normal", "high"]


class Event(BaseModel):
    """Base envelope for every event on the bus."""

    id: str = Field(default_factory=lambda: uuid4().hex)
    kind: EventKind
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = "core"
    payload: dict[str, Any] = Field(default_factory=dict)

    model_config = {"frozen": True}
