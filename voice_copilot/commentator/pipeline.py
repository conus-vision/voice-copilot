"""Commentator pipeline: debounce events, stream an LLM, emit utterances.

Flow:
    bus → classify/filter → debounce buffer → LLM.stream_chat → bus(COMMENTATOR_UTTERANCE)

We emit *two* event variants:
  * streaming deltas (`streaming: True`) — useful if a consumer wants to start
    speaking before the sentence is complete; TTS providers that buffer can
    ignore these and wait for the final one.
  * final utterance (`streaming: False`) — the full sentence, ready to speak.

A high-importance event (error, file edit, agent awaiting input) forces an
immediate flush so we don't leave the user in silence when something urgent
just happened.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from voice_copilot.commentator.format import format_events
from voice_copilot.commentator.importance import classify, meets_threshold
from voice_copilot.commentator.prompts import load as load_prompt
from voice_copilot.core.bus import EventBus
from voice_copilot.core.config import CommentatorConfig, Language
from voice_copilot.core.events import Event, EventKind
from voice_copilot.providers import registry
from voice_copilot.providers.llm.base import LLMMessage, LLMProvider

log = logging.getLogger(__name__)


class Commentator:
    def __init__(
        self,
        bus: EventBus,
        cfg: CommentatorConfig,
        language: Language,
        llm: LLMProvider | None = None,
    ) -> None:
        self._bus = bus
        self._cfg = cfg
        self._language = language
        self._llm = llm or _build_llm(cfg)
        self._system_prompt = load_prompt(language)

        self._buffer: list[Event] = []
        self._max_buffer = 60
        self._speak_task: asyncio.Task[None] | None = None

    async def run(self) -> None:
        """Main loop — subscribe to the bus and narrate until cancelled."""
        debounce_s = max(0.05, self._cfg.debounce_ms / 1000.0)

        async with self._bus.subscribe() as q:
            while True:
                event = await q.get()
                imp = classify(event, self._cfg)
                if imp is None or not meets_threshold(imp, self._cfg.min_importance):
                    continue
                if event.source.startswith("commentator"):
                    continue  # don't narrate our own utterances

                self._buffer.append(event)
                if len(self._buffer) > self._max_buffer:
                    # Keep the tail — older context is less relevant by now.
                    self._buffer = self._buffer[-self._max_buffer:]

                if imp == "high":
                    await self._flush()
                    continue

                # Wait for the bus to go quiet for debounce_s. Fresh events
                # restart the wait; a timeout triggers flush.
                try:
                    while True:
                        event = await asyncio.wait_for(q.get(), timeout=debounce_s)
                        imp = classify(event, self._cfg)
                        if imp is None or not meets_threshold(imp, self._cfg.min_importance):
                            continue
                        if event.source.startswith("commentator"):
                            continue
                        self._buffer.append(event)
                        if len(self._buffer) > self._max_buffer:
                            self._buffer = self._buffer[-self._max_buffer:]
                        if imp == "high":
                            break
                except asyncio.TimeoutError:
                    pass

                await self._flush()

    async def _flush(self) -> None:
        if not self._buffer:
            return
        events, self._buffer = self._buffer, []

        # Cancel any in-flight narration — a new batch supersedes it.
        if self._speak_task is not None and not self._speak_task.done():
            self._speak_task.cancel()
            try:
                await self._speak_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        self._speak_task = asyncio.create_task(
            self._narrate(events), name="commentator.narrate",
        )

    async def _narrate(self, events: list[Event]) -> None:
        user_content = format_events(events)
        messages = [LLMMessage(role="user", content=user_content)]
        pieces: list[str] = []
        utterance_id = events[-1].id  # tie utterance to the last event for ordering

        try:
            async for delta in self._llm.stream_chat(
                messages, system=self._system_prompt, max_tokens=160, temperature=0.4,
            ):
                if not delta:
                    continue
                pieces.append(delta)
                await self._emit(
                    {
                        "utterance_id": utterance_id,
                        "text": delta,
                        "streaming": True,
                        "language": self._language,
                    }
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("commentator LLM error")
            await self._bus.publish(
                Event(
                    kind=EventKind.ERROR,
                    source="commentator",
                    payload={"where": "llm", "message": str(e)},
                )
            )
            return

        full = "".join(pieces).strip()
        if not full:
            return
        await self._emit(
            {
                "utterance_id": utterance_id,
                "text": full,
                "streaming": False,
                "language": self._language,
            }
        )

    async def _emit(self, payload: dict[str, Any]) -> None:
        await self._bus.publish(
            Event(kind=EventKind.COMMENTATOR_UTTERANCE, source="commentator", payload=payload)
        )


def _build_llm(cfg: CommentatorConfig) -> LLMProvider:
    return registry.build("llm", cfg.provider.name, dict(cfg.provider.options))
