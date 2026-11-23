"""The supervisor's running conversation: what each review sends, and when it starts over."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

import voice_copilot.commentator.pipeline as pipeline
from voice_copilot.commentator.pipeline import Commentator, _Batch, _SessionContext
from voice_copilot.core.bus import EventBus
from voice_copilot.core.config import CommentatorConfig, ProviderConfig, SupervisorConfig
from voice_copilot.core.events import Event, EventKind
from voice_copilot.providers.llm.live_claude import LiveReply


class _FakeLive:
    made: ClassVar[list[_FakeLive]] = []

    def __init__(self, binary: str, *, system: str, model: str | None, args: Any) -> None:
        self.model = model
        self.sent: list[str] = []
        self.turns = 0
        self.last_turn_at = 0.0
        self.context_tokens = 0
        self.alive = True
        self.closed = False
        self.fail_next = False
        _FakeLive.made.append(self)

    async def turn(self, content: str) -> LiveReply:
        import time

        if self.fail_next:
            self.alive = False
            raise RuntimeError("the live claude session ended mid-reply: rate limit")
        self.sent.append(content)
        self.turns += 1
        self.last_turn_at = time.monotonic()
        self.context_tokens = 5000 * self.turns
        return LiveReply("OK", {"cache_read_input_tokens": 4000, "input_tokens": 3})

    async def close(self) -> None:
        self.closed = True
        self.alive = False


@pytest.fixture(autouse=True)
def fake_live(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeLive.made = []
    monkeypatch.setattr(pipeline, "LiveClaude", _FakeLive)


def _commentator(keep: bool = True) -> Commentator:
    cfg = CommentatorConfig(
        provider=ProviderConfig(name="auto", options={"cli": "claude", "binary": "claude"}),
        supervisor=SupervisorConfig(mode="watch", model="opus", keep_session=keep),
    )
    return Commentator(EventBus(), cfg, "en", llm=object())  # type: ignore[arg-type]


def _tool(i: int) -> Event:
    return Event(
        kind=EventKind.TOOL_CALL_STARTED,
        source="hooks.claude",
        payload={"session_id": "s", "tool": "Bash", "input": {"command": f"step {i}"}},
    )


async def _review(c: Commentator, *events: Event) -> None:
    c._record_events("s", list(events))
    ctx = c._contexts["s"]
    await c._supervise(
        _Batch(events=list(events), session_key="s", user_query="fix it"), "8 tool calls"
    )
    assert ctx is c._contexts["s"]


async def test_the_first_review_is_the_full_message_then_only_what_is_new() -> None:
    c = _commentator()
    c._contexts["s"] = _SessionContext(user_query="fix it")
    await _review(c, _tool(1), _tool(2))
    await _review(c, _tool(3))
    live = _FakeLive.made[0]
    assert len(_FakeLive.made) == 1 and live.model == "opus"
    assert live.sent[0].startswith("GOAL:")
    assert "step 1" in live.sent[0]
    assert live.sent[1].startswith("NEW SINCE YOUR LAST VERDICT")
    assert "step 3" in live.sent[1] and "step 1" not in live.sent[1]
    assert c.usage()["supervisor.cache_read"] == 8000


async def test_a_new_task_starts_the_conversation_over() -> None:
    c = _commentator()
    c._contexts["s"] = _SessionContext(user_query="fix it")
    await _review(c, _tool(1))
    c._consume_control_event(
        Event(
            kind=EventKind.USER_MESSAGE,
            source="hooks.claude",
            payload={"session_id": "s", "text": "now do the docs", "delivery": "observed"},
        )
    )
    await _review(c, _tool(2))
    assert len(_FakeLive.made) == 2
    assert _FakeLive.made[1].sent[0].startswith("GOAL:")


async def test_a_failed_turn_is_reported_and_the_next_review_starts_over() -> None:
    c = _commentator()
    c._contexts["s"] = _SessionContext(user_query="fix it")
    await _review(c, _tool(1))
    _FakeLive.made[0].fail_next = True
    async with c._bus.subscribe() as q:
        await _review(c, _tool(2))
        errors = []
        while not q.empty():
            event = q.get_nowait()
            if event.kind is EventKind.ERROR:
                errors.append(event)
    assert errors and "rate limit" in errors[0].payload["message"]
    await _review(c, _tool(3))
    assert len(_FakeLive.made) == 2
    assert _FakeLive.made[1].sent[0].startswith("GOAL:")


async def test_a_long_pause_starts_over_rather_than_rewriting_a_cold_cache() -> None:
    c = _commentator()
    c._contexts["s"] = _SessionContext(user_query="fix it")
    await _review(c, _tool(1))
    _FakeLive.made[0].last_turn_at -= pipeline._LIVE_IDLE_S + 1
    await _review(c, _tool(2))
    assert len(_FakeLive.made) == 2


async def test_switched_off_it_reviews_one_shot() -> None:
    c = _commentator(keep=False)
    assert c._live_binary() is None


def test_only_the_claude_cli_keeps_a_conversation() -> None:
    c = _commentator()
    assert c._live_binary() is not None
    c._cfg.provider = ProviderConfig(name="auto", options={"cli": "codex"})
    assert c._live_binary() is None
    c._cfg.supervisor.cli = "claude"  # Claude supervising Codex: through the Claude CLI
    assert c._live_binary() is not None
    c._cfg.supervisor.cli = None
    c._cfg.provider = ProviderConfig(name="anthropic", options={"model": "x"})
    assert c._live_binary() is None


async def test_a_burst_of_events_stays_in_the_conversation() -> None:
    c = _commentator()
    c._contexts["s"] = _SessionContext(user_query="fix it")
    await _review(c, _tool(1))
    # Three batches between reviews, a line for each said-and-done: more than the 80 lines
    # the transcript keeps (one batch is at most 40).
    for start in (100, 200, 300):
        burst: list[Event] = []
        for i in range(start, start + 25):
            burst.append(
                Event(
                    kind=EventKind.AGENT_TEXT,
                    source="hooks.claude",
                    payload={"session_id": "s", "text": f"thought {i}"},
                )
            )
            burst.append(_tool(i))
        c._record_events("s", burst)
    await _review(c, _tool(999))
    live = _FakeLive.made[0]
    assert len(_FakeLive.made) == 1  # no costly start over
    assert "earlier lines not shown" in live.sent[1]
    assert "step 999" in live.sent[1]
