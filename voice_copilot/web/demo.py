"""Synthetic event generator — lets you see the popup working without any LLM CLI attached.

Enabled by `voice-copilot serve --demo`.
"""

from __future__ import annotations

import asyncio
import itertools

from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import Event, EventKind

_SCRIPT: list[tuple[EventKind, dict[str, object]]] = [
    (EventKind.SESSION_STARTED, {"target": "claude", "mode": "demo"}),
    (EventKind.TURN_STARTED, {"turn": 1}),
    (EventKind.AGENT_THINKING, {"text": "Reading the failing test first to see the assertion."}),
    (EventKind.TOOL_CALL_STARTED, {"tool": "Read", "args": {"path": "tests/test_auth.py"}}),
    (EventKind.TOOL_CALL_FINISHED, {"tool": "Read", "ok": True}),
    (EventKind.AGENT_TEXT, {"text": "The test expects a 403 but the handler returns 401."}),
    (EventKind.TOOL_CALL_STARTED, {"tool": "Edit", "args": {"path": "app/auth.py"}}),
    (EventKind.FILE_EDITED, {"path": "app/auth.py", "lines_changed": 4}),
    (EventKind.TOOL_CALL_FINISHED, {"tool": "Edit", "ok": True}),
    (EventKind.TOOL_CALL_STARTED, {"tool": "Bash", "args": {"cmd": "pytest tests/test_auth.py"}}),
    (EventKind.TOOL_CALL_FINISHED, {"tool": "Bash", "ok": True, "result": "1 passed"}),
    (EventKind.AGENT_TEXT, {"text": "Test passes. Running full suite to check for regressions."}),
    (EventKind.TURN_ENDED, {"turn": 1}),
]


async def run_demo(bus: EventBus, interval: float = 2.0) -> None:
    for kind, payload in itertools.cycle(_SCRIPT):
        await bus.publish(Event(kind=kind, source="demo", payload=payload))
        await asyncio.sleep(interval)
