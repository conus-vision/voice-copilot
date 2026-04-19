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
import logging
from uuid import uuid4

from voice_copilot.audio.hub import AudioHub
from voice_copilot.core.bus import EventBus
from voice_copilot.core.config import Language
from voice_copilot.core.events import Event, EventKind
from voice_copilot.providers.tts.base import TTSProvider

log = logging.getLogger(__name__)


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

    async def run(self) -> None:
        async with self._bus.subscribe() as q:
            while True:
                ev = await q.get()
                if ev.kind is EventKind.USER_SPEAK_REQUESTED:
                    if ev.payload.get("phase") == "start":
                        await self._abort_current()
                    continue
                if ev.kind is EventKind.USER_INTERRUPT:
                    await self._abort_current()
                    continue
                if ev.kind is not EventKind.COMMENTATOR_UTTERANCE:
                    continue
                if ev.payload.get("streaming"):
                    continue  # only speak finalised utterances
                text = (ev.payload.get("text") or "").strip()
                if not text or self._muted:
                    continue
                # Serialise utterances — start next only after previous finishes.
                if self._current is not None and not self._current.done():
                    try:
                        await self._current
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
                self._current = asyncio.create_task(
                    self._speak(text, ev.payload.get("language") or self._language),
                    name="tts.speak",
                )

    def set_muted(self, muted: bool) -> None:
        self._muted = muted
        if muted:
            # Fire-and-forget abort — don't block the caller.
            asyncio.create_task(self._abort_current(), name="tts.abort")

    async def _abort_current(self) -> None:
        task = self._current
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await self._hub.broadcast_text({"type": "audio_interrupt"})

    async def _speak(self, text: str, language: str) -> None:
        utt_id = uuid4().hex
        fmt = self._tts.output_format
        async with self._hub.utterance_lock():
            await self._bus.publish(Event(
                kind=EventKind.TTS_STARTED,
                source="tts.driver",
                payload={"utterance_id": utt_id, "format": fmt, "language": language},
            ))
            await self._hub.broadcast_text({
                "type": "audio_header",
                "utterance_id": utt_id,
                "format": fmt,
                "language": language,
            })
            try:
                async for chunk in self._tts.synthesize(text, language=language):
                    if chunk.is_last:
                        break
                    if chunk.data:
                        await self._hub.broadcast_bytes(chunk.data)
            except asyncio.CancelledError:
                await self._hub.broadcast_text({
                    "type": "audio_end", "utterance_id": utt_id, "aborted": True,
                })
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("tts synth failed")
                await self._bus.publish(Event(
                    kind=EventKind.ERROR,
                    source="tts.driver",
                    payload={"where": "tts", "message": str(e)},
                ))
                await self._hub.broadcast_text({
                    "type": "audio_end", "utterance_id": utt_id, "error": True,
                })
                return
            await self._hub.broadcast_text({
                "type": "audio_end", "utterance_id": utt_id,
            })
            await self._bus.publish(Event(
                kind=EventKind.TTS_FINISHED,
                source="tts.driver",
                payload={"utterance_id": utt_id},
            ))
