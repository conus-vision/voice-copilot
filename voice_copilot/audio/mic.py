"""Per-WS mic session: buffer binary frames between mic_start / mic_end,
then hand the blob to the STT provider and publish USER_MESSAGE on the bus.

Keeping state per WS connection means multiple clients could record
independently (rare but clean). Buffers are capped — a stuck client can't
balloon memory.
"""

from __future__ import annotations

import logging
from typing import cast

from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import Event, EventKind
from voice_copilot.providers.stt.base import AudioContainer, STTProvider

log = logging.getLogger(__name__)

_MAX_BYTES = 10 * 1024 * 1024  # 10 MB — ~10 min of opus/webm. Plenty for one turn.


class MicSession:
    def __init__(self) -> None:
        self._buf: bytearray | None = None
        self._codec: AudioContainer = "webm"

    @property
    def active(self) -> bool:
        return self._buf is not None

    def start(self, codec: str | None) -> None:
        self._buf = bytearray()
        if codec in ("webm", "ogg", "wav", "mp3", "raw_pcm16"):
            self._codec = cast(AudioContainer, codec)
        else:
            self._codec = "webm"

    def feed(self, data: bytes) -> None:
        if self._buf is None:
            return
        if len(self._buf) + len(data) > _MAX_BYTES:
            log.warning("mic buffer overflow — dropping frame")
            return
        self._buf.extend(data)

    def finish(self) -> tuple[bytes, AudioContainer] | None:
        if self._buf is None:
            return None
        data = bytes(self._buf)
        codec = self._codec
        self._buf = None
        if not data:
            return None
        return data, codec


async def transcribe_and_publish(
    bus: EventBus,
    stt: STTProvider,
    audio: bytes,
    container: AudioContainer,
    language: str | None,
) -> None:
    """Run STT on a completed mic buffer and publish USER_MESSAGE."""
    try:
        result = await stt.transcribe(audio, container=container, language=language)
    except Exception as e:  # noqa: BLE001
        log.exception("stt failed")
        await bus.publish(Event(
            kind=EventKind.ERROR,
            source="stt.driver",
            payload={"where": "stt", "message": str(e)},
        ))
        return

    text = (result.text or "").strip()
    if not text:
        return
    await bus.publish(Event(
        kind=EventKind.USER_MESSAGE,
        source="stt.driver",
        payload={"text": text, "language": result.language, "delivery": "pending"},
    ))
