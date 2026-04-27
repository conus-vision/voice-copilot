"""Subscribes to final commentator utterances, synthesises, fans out to WS.

Contract with the browser (`audio_header` → bytes → `audio_end` text frames):
    { "type":"audio_header", "utterance_id":"...", "format":"mp3", "language":"en" }
    <binary frame>*
    { "type":"audio_end",    "utterance_id":"..." }

Utterances are spoken one at a time — the hub lock serializes them so the
framing stays unambiguous for clients.

If the bus publishes USER_SPEAK_REQUESTED(phase=start) while we're speaking,
we abort the current synthesis and tell clients to drop the tail
(`audio_interrupt` text frame). That's server-side barge-in; the client also
pauses its own <audio> element for redundancy.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from uuid import uuid4

from voice_copilot.audio.hub import AudioHub
from voice_copilot.core.bus import EventBus
from voice_copilot.core.config import Language
from voice_copilot.core.events import Event, EventKind
from voice_copilot.providers.tts.base import TTSProvider

log = logging.getLogger(__name__)

_NO_SESSION_KEY = "_none"


@dataclass(slots=True)
class _QueuedUtterance:
    text: str
    language: str
    session_key: str
    session_id: str | None
    query_version: int | None


class TTSDriver:
    def __init__(
        self,
        bus: EventBus,
        hub: AudioHub,
        tts: TTSProvider,
        language: Language,
        muted: bool = False,
    ) -> None:
        self._bus = bus
        self._hub = hub
        self._tts = tts
        self._language = language
        self._muted = muted
        self._current: asyncio.Task[None] | None = None
        self._abort_task: asyncio.Task[None] | None = None
        self._latest_query_text: dict[str, str] = {}
        self._query_versions: dict[str, int] = {}
        self._pending: list[_QueuedUtterance] = []
        self._pending_event = asyncio.Event()

    async def run(self) -> None:
        speaker = asyncio.create_task(self._speaker_loop(), name="tts.worker")
        try:
            async with self._bus.subscribe() as q:
                while True:
                    ev = await q.get()
                    if ev.kind is EventKind.USER_MESSAGE:
                        if self._register_query(ev):
                            self._clear_pending(self._session_key_of(ev.payload))
                            await self._abort_current()
                        continue
                    if ev.kind is EventKind.USER_SPEAK_REQUESTED:
                        if ev.payload.get("phase") == "start":
                            self._clear_pending()
                            await self._abort_current()
                        continue
                    if ev.kind is EventKind.USER_INTERRUPT:
                        self._clear_pending()
                        await self._abort_current()
                        continue
                    if ev.kind is not EventKind.COMMENTATOR_UTTERANCE:
                        continue
                    if ev.payload.get("streaming"):
                        continue  # only speak finalised utterances
                    utterance = self._build_utterance(ev)
                    if utterance is None:
                        continue
                    self._replace_pending(utterance)
        finally:
            speaker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await speaker

    def set_muted(self, muted: bool) -> None:
        self._muted = muted
        if muted:
            self._clear_pending()
            # Fire-and-forget abort — don't block the caller.
            task = asyncio.create_task(self._abort_current(), name="tts.abort")
            self._abort_task = task
            task.add_done_callback(self._clear_abort_task)

    async def _abort_current(self) -> None:
        task = self._current
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        await self._hub.broadcast_text({"type": "audio_interrupt"})

    def _clear_abort_task(self, task: asyncio.Task[None]) -> None:
        if self._abort_task is task:
            self._abort_task = None

    def _register_query(self, ev: Event) -> bool:
        if ev.source.startswith(("stt.", "web", "hotkey")):
            return False
        text = (ev.payload.get("text") or "").strip()
        if not text:
            return False
        key = self._session_key_of(ev.payload)
        if self._latest_query_text.get(key) == text:
            return False
        self._latest_query_text[key] = text
        self._query_versions[key] = self._query_versions.get(key, 0) + 1
        return True

    def _is_stale_utterance(self, ev: Event) -> bool:
        key = self._session_key_of(ev.payload)
        expected = self._query_versions.get(key)
        query_version = ev.payload.get("query_version")
        if expected is None or not isinstance(query_version, int):
            return False
        return query_version < expected

    def _build_utterance(self, ev: Event) -> _QueuedUtterance | None:
        if self._muted or self._is_stale_utterance(ev):
            return None
        text = str(ev.payload.get("read_text") or ev.payload.get("text") or "").strip()
        if not text:
            return None
        session_id = ev.payload.get("session_id")
        query_version = ev.payload.get("query_version")
        return _QueuedUtterance(
            text=text,
            language=str(ev.payload.get("language") or self._language),
            session_key=self._session_key_of(ev.payload),
            session_id=session_id if isinstance(session_id, str) and session_id else None,
            query_version=query_version if isinstance(query_version, int) else None,
        )

    def _replace_pending(self, utterance: _QueuedUtterance) -> None:
        self._pending = [
            item for item in self._pending if item.session_key != utterance.session_key
        ]
        self._pending.append(utterance)
        self._pending_event.set()

    def _clear_pending(self, session_key: str | None = None) -> None:
        if session_key is None:
            self._pending.clear()
        else:
            self._pending = [item for item in self._pending if item.session_key != session_key]
        if not self._pending:
            self._pending_event.clear()

    async def _speaker_loop(self) -> None:
        try:
            while True:
                await self._pending_event.wait()
                utterance = self._pop_next_pending()
                if utterance is None:
                    continue
                if utterance.query_version is not None:
                    expected = self._query_versions.get(utterance.session_key)
                    if expected is not None and utterance.query_version < expected:
                        continue
                if self._muted:
                    continue
                task = asyncio.create_task(self._speak(utterance), name="tts.speak")
                self._current = task
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                finally:
                    if self._current is task:
                        self._current = None
        except asyncio.CancelledError:
            current_task = self._current
            if current_task is not None and not current_task.done():
                current_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await current_task
            raise

    def _pop_next_pending(self) -> _QueuedUtterance | None:
        if not self._pending:
            self._pending_event.clear()
            return None
        utterance = self._pending.pop(0)
        if not self._pending:
            self._pending_event.clear()
        return utterance

    @staticmethod
    def _session_key_of(payload: dict[str, object]) -> str:
        session_id = payload.get("session_id")
        if isinstance(session_id, str) and session_id:
            return session_id
        return _NO_SESSION_KEY

    async def _speak(self, utterance: _QueuedUtterance) -> None:
        utt_id = uuid4().hex
        fmt = self._tts.output_format
        async with self._hub.utterance_lock():
            await self._bus.publish(
                Event(
                    kind=EventKind.TTS_STARTED,
                    source="tts.driver",
                    payload={
                        "utterance_id": utt_id,
                        "format": fmt,
                        "language": utterance.language,
                        "session_id": utterance.session_id,
                        "query_version": utterance.query_version,
                    },
                )
            )
            await self._hub.broadcast_text(
                {
                    "type": "audio_header",
                    "utterance_id": utt_id,
                    "format": fmt,
                    "language": utterance.language,
                    "session_id": utterance.session_id,
                    "query_version": utterance.query_version,
                }
            )
            try:
                stream = self._tts.synthesize(utterance.text, language=utterance.language)
                async for chunk in stream:
                    if chunk.is_last:
                        break
                    if chunk.data:
                        await self._hub.broadcast_bytes(chunk.data)
            except asyncio.CancelledError:
                await self._hub.broadcast_text(
                    {
                        "type": "audio_end",
                        "utterance_id": utt_id,
                        "aborted": True,
                    }
                )
                raise
            except Exception as e:
                log.exception("tts synth failed")
                await self._bus.publish(
                    Event(
                        kind=EventKind.ERROR,
                        source="tts.driver",
                        payload={"where": "tts", "message": str(e)},
                    )
                )
                await self._hub.broadcast_text(
                    {
                        "type": "audio_end",
                        "utterance_id": utt_id,
                        "error": True,
                    }
                )
                return
            await self._hub.broadcast_text(
                {
                    "type": "audio_end",
                    "utterance_id": utt_id,
                }
            )
            await self._bus.publish(
                Event(
                    kind=EventKind.TTS_FINISHED,
                    source="tts.driver",
                    payload={
                        "utterance_id": utt_id,
                        "session_id": utterance.session_id,
                        "query_version": utterance.query_version,
                    },
                )
            )
