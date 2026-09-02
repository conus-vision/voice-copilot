"""WebSocket bridge between the browser popup and the event bus.

Protocol:

  Text frames (either direction):
    Server → client:
      { "type":"event", "kind":..., "ts":..., "payload":{...} }
            { "type":"audio_header", "utterance_id":..., "format":"mp3", "language":"en", "session_id"?:..., "query_version"?:... }
      { "type":"audio_end", "utterance_id":..., "aborted"?:bool, "error"?:bool }
      { "type":"audio_interrupt" }
      { "type":"pong" }
    Client → server:
      { "type":"cmd", "cmd":"play"|"pause"|"mute"|"unmute"|"interrupt" }
      { "type":"cmd", "cmd":"hold_while_narrating", "enabled":true|false }
      { "type":"cmd", "cmd":"agent_toggle_pause" }
      { "type":"cmd", "cmd":"speak_start"|"speak_end" }
      { "type":"cmd", "cmd":"mic_start", "codec":"webm"|"ogg"|"wav" }
      { "type":"cmd", "cmd":"mic_end" }
      { "type":"cmd", "cmd":"panel_focus", "focused":true|false }
            { "type":"cmd", "cmd":"playback_rate", "playback_rate":1.2, "session_id"?:... }
            { "type":"cmd", "cmd":"playback_ready", "reason":"eighty_percent"|"skipped", "playback_rate"?:1.2, "session_id"?:..., "utterance_id"?:... }
      { "type":"ping" }

  Binary frames:
    Server → client: TTS audio bytes (belonging to the most recent audio_header).
    Client → server: mic audio bytes (belonging to the most recent mic_start).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from voice_copilot.audio.hub import AudioHub
from voice_copilot.audio.mic import MicSession, transcribe_and_publish
from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import Event, EventKind
from voice_copilot.focus import FocusRouter
from voice_copilot.providers.stt.base import STTProvider

log = logging.getLogger(__name__)
_background_tasks: set[asyncio.Task[Any]] = set()


def _track_background_task(task: asyncio.Task[Any]) -> None:
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _encode(event: Event) -> str:
    return json.dumps(
        {
            "type": "event",
            "id": event.id,
            "kind": event.kind.value,
            "ts": event.ts.isoformat(),
            "source": event.source,
            "payload": event.payload,
        }
    )


async def _pump_bus_to_ws(bus: EventBus, ws: WebSocket) -> None:
    async with bus.subscribe() as q:
        while True:
            event = await q.get()
            await ws.send_text(_encode(event))


async def _handle_cmd(
    bus: EventBus,
    mic: MicSession,
    data: dict[str, Any],
    stt: STTProvider | None,
    language: str | None,
    focus_router: FocusRouter | None = None,
    voice_input: bool = True,
    dialog: Any = None,
) -> None:
    cmd = data.get("cmd")
    if not voice_input and cmd in ("speak_start", "speak_end", "mic_start", "mic_end"):
        # Voice input is off (config `voice_input.enabled`); the panel hides the
        # mic, so anything arriving here is a stale tab — drop it silently.
        return
    if cmd == "playback_rate":
        state_payload: dict[str, Any] = {}
        rate = data.get("playback_rate")
        if isinstance(rate, (int, float)) and rate > 0:
            state_payload["playback_rate"] = float(rate)
        session_id = data.get("session_id")
        if isinstance(session_id, str) and session_id:
            state_payload["session_id"] = session_id
        await bus.publish(
            Event(
                kind=EventKind.PLAYBACK_STATE,
                source="web",
                payload=state_payload,
            )
        )
        return
    if cmd == "playback_ready":
        ready_payload: dict[str, Any] = {}
        reason = data.get("reason")
        if isinstance(reason, str) and reason:
            ready_payload["reason"] = reason
        rate = data.get("playback_rate")
        if isinstance(rate, (int, float)) and rate > 0:
            ready_payload["playback_rate"] = float(rate)
        session_id = data.get("session_id")
        if isinstance(session_id, str) and session_id:
            ready_payload["session_id"] = session_id
        utterance_id = data.get("utterance_id")
        if isinstance(utterance_id, str) and utterance_id:
            ready_payload["utterance_id"] = utterance_id
        await bus.publish(
            Event(
                kind=EventKind.PLAYBACK_READY,
                source="web",
                payload=ready_payload,
            )
        )
        return
    if cmd in ("speak_start", "speak_end"):
        phase = "start" if cmd == "speak_start" else "end"
        await bus.publish(
            Event(
                kind=EventKind.USER_SPEAK_REQUESTED,
                source="web",
                payload={"phase": phase},
            )
        )
        return
    if cmd == "interrupt":
        await bus.publish(Event(kind=EventKind.USER_INTERRUPT, source="web", payload={}))
        return
    if cmd == "agent_toggle_pause":
        # The panel's way to release an agent the supervisor (or the user)
        # paused — same event the alt+p hotkey publishes.
        await bus.publish(Event(kind=EventKind.USER_PAUSE_TOGGLE, source="web", payload={}))
        return
    if cmd == "hold_while_narrating":
        # Live toggle: the panel button flips it for this run without making
        # the user open Settings and save.
        if dialog is not None:
            dialog.set_hold_while_narrating(bool(data.get("enabled")))
        return
    if cmd == "mic_start":
        mic.start(data.get("codec"))
        return
    if cmd == "mic_end":
        result = mic.finish()
        if result is None:
            return
        audio, container = result
        if stt is None:
            await bus.publish(
                Event(
                    kind=EventKind.ERROR,
                    source="stt.driver",
                    payload={"message": "STT provider not configured"},
                )
            )
            return
        # Fire-and-forget — STT can take a second, we don't want to block WS.
        _track_background_task(
            asyncio.create_task(
                transcribe_and_publish(bus, stt, audio, container, language),
                name="stt.transcribe",
            )
        )
        return
    if cmd == "panel_focus":
        focused = data.get("focused")
        if isinstance(focused, bool) and focus_router is not None:
            focus_router.set_panel_focus(focused)
        return


def register_ws(app: FastAPI) -> None:
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        bus: EventBus = ws.app.state.bus
        hub: AudioHub = ws.app.state.audio_hub
        stt: STTProvider | None = getattr(ws.app.state, "stt_provider", None)
        language: str | None = getattr(ws.app.state, "human_language", None)
        voice_input: bool = bool(getattr(ws.app.state, "voice_input_enabled", True))
        focus_router: FocusRouter | None = getattr(ws.app.state, "focus_router", None)
        dialog = getattr(ws.app.state, "dialog", None)
        await ws.accept()
        await hub.register(ws)

        mic = MicSession()
        pump = asyncio.create_task(_pump_bus_to_ws(bus, ws), name="ws.pump")
        try:
            while True:
                msg = await ws.receive()
                if "text" in msg and msg["text"] is not None:
                    try:
                        data = json.loads(msg["text"])
                    except json.JSONDecodeError:
                        await ws.send_text(json.dumps({"type": "error", "message": "bad json"}))
                        continue
                    mtype = data.get("type")
                    if mtype == "cmd":
                        await _handle_cmd(
                            bus,
                            mic,
                            data,
                            stt,
                            language,
                            focus_router,
                            voice_input=bool(
                                getattr(ws.app.state, "voice_input_enabled", voice_input)
                            ),
                            dialog=dialog,
                        )
                    elif mtype == "ping":
                        await ws.send_text(json.dumps({"type": "pong"}))
                elif "bytes" in msg and msg["bytes"] is not None:
                    if getattr(ws.app.state, "voice_input_enabled", voice_input):
                        mic.feed(msg["bytes"])
                elif msg.get("type") == "websocket.disconnect":
                    break
        except WebSocketDisconnect:
            pass
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
            await hub.unregister(ws)
