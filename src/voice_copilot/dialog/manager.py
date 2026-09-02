"""Dialog manager — routes user voice messages into the running CLI.

Listens to:
  * `USER_MESSAGE`       — transcribed speech (from STT) or typed text.
                           Routed to the adapter per its QuickAsideCapability.
  * `USER_INTERRUPT`     — hotkey / button pressed. Pauses the adapter so the
                           user can speak without the agent racing ahead.
  * `USER_PAUSE_TOGGLE`  — explicit pause/resume hotkey.
  * `USER_SPEAK_REQUESTED` phase=start/end — push-to-talk holds. If
                           `dialog.auto_pause_on_speak` is on, we pause while
                           the button is held and resume when released.
  * `TTS_STARTED` / `PLAYBACK_READY` — if `dialog.hold_agent_while_narrating`
                           is on, the agent is suspended from the moment a line
                           starts until the browser reports it played, so it
                           doesn't run ahead of you. A watchdog lifts the hold
                           anyway if no panel ever reports back (muted tab,
                           blocked autoplay, closed window), because a silent
                           browser must never wedge the agent.
  * `TURN_ENDED`         — for `dialog.deliver_immediately=False`, flush any
                           pending user messages queued during the turn.

Emits `AGENT_PAUSED` / `AGENT_RESUMED` so the UI can reflect state.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from voice_copilot.core.bus import EventBus
from voice_copilot.core.config import DialogConfig
from voice_copilot.core.events import Event, EventKind

if TYPE_CHECKING:
    from voice_copilot.adapters.base import CLIAdapter

log = logging.getLogger(__name__)

#: Longest the agent may stay held for one line before we let it go anyway.
#: Generous — a long sentence at 1.2x can run a while — but bounded.
_NARRATION_HOLD_TIMEOUT_S = 90.0


class DialogManager:
    def __init__(self, bus: EventBus, adapter: CLIAdapter, cfg: DialogConfig) -> None:
        self._bus = bus
        self._adapter = adapter
        self._cfg = cfg
        self._pending: list[tuple[str, bool]] = []  # (text, urgent)
        self._auto_paused_by_ptt = False
        self._auto_paused_by_narration = False
        self._narration_watchdog: asyncio.Task[None] | None = None

    def set_hold_while_narrating(self, enabled: bool) -> None:
        """Live toggle from the panel; the saved config supplies the default."""
        self._cfg.hold_agent_while_narrating = enabled

    async def run(self) -> None:
        async with self._bus.subscribe() as q:
            while True:
                ev = await q.get()
                try:
                    await self._handle(ev)
                except Exception:
                    log.exception("dialog handler failed on %s", ev.kind)

    async def _handle(self, ev: Event) -> None:
        k = ev.kind

        if k is EventKind.USER_MESSAGE:
            if ev.source.startswith(("stt.", "web", "hotkey", "dialog")):
                # Ignore our own re-publications.
                if ev.source == "dialog.manager":
                    return
                text = (ev.payload.get("text") or "").strip()
                if not text:
                    return
                urgent = bool(ev.payload.get("urgent"))
                if self._cfg.deliver_immediately:
                    await self._deliver(text, urgent=urgent)
                else:
                    self._pending.append((text, urgent))
                    log.info("queued user message (%d pending)", len(self._pending))
            return

        if k is EventKind.USER_INTERRUPT:
            # Treat as "pause to talk". The user will push-to-talk next.
            if await self._adapter.pause():
                await self._emit_paused("interrupt")
            return

        if k is EventKind.USER_PAUSE_TOGGLE:
            if self._adapter.is_paused:
                if await self._adapter.resume():
                    await self._emit_resumed("toggle")
            else:
                if await self._adapter.pause():
                    await self._emit_paused("toggle")
            return

        if k is EventKind.USER_SPEAK_REQUESTED:
            if not self._cfg.auto_pause_on_speak:
                return
            phase = ev.payload.get("phase")
            if phase == "start" and not self._adapter.is_paused and await self._adapter.pause():
                self._auto_paused_by_ptt = True
                await self._emit_paused("auto_on_speak")
            elif phase == "end" and self._auto_paused_by_ptt:
                if await self._adapter.resume():
                    self._auto_paused_by_ptt = False
                    await self._emit_resumed("auto_on_speak")
            return

        if k is EventKind.SUPERVISOR_STOP:
            # Supervisor+ judged the agent off track. Pause it and leave it
            # paused: resuming is the user's call, from the panel or the hotkey.
            if not self._adapter.is_paused and await self._adapter.pause():
                await self._emit_paused("supervisor")
            return

        if k is EventKind.TTS_STARTED:
            if not self._cfg.hold_agent_while_narrating:
                return
            # Never fight a pause the user asked for — only resume what we
            # suspended ourselves, and only once the line has been read.
            if not self._adapter.is_paused and await self._adapter.pause():
                self._auto_paused_by_narration = True
                await self._emit_paused("hold_while_narrating")
                self._arm_narration_watchdog()
            return

        if k is EventKind.PLAYBACK_READY:
            await self._release_narration_hold()
            return

        if k is EventKind.TURN_ENDED:
            if not self._pending:
                return
            buffered, self._pending = self._pending, []
            for text, urgent in buffered:
                await self._deliver(text, urgent=urgent)
            return

    async def _deliver(self, text: str, *, urgent: bool) -> None:
        try:
            await self._adapter.send_user_message(text, urgent=urgent)
        except Exception as e:
            log.exception("adapter send_user_message failed")
            await self._bus.publish(
                Event(
                    kind=EventKind.ERROR,
                    source="dialog.manager",
                    payload={"where": "deliver", "message": str(e)},
                )
            )
            return
        await self._bus.publish(
            Event(
                kind=EventKind.USER_MESSAGE,
                source="dialog.manager",
                payload={
                    "text": text,
                    "urgent": urgent,
                    "delivery": "sent",
                    "adapter": self._adapter.name,
                    "capability": self._adapter.quick_aside.value,
                },
            )
        )

    def _arm_narration_watchdog(self) -> None:
        """Lift the hold even if the panel never reports playback.

        A muted tab, blocked autoplay or a closed window means `PLAYBACK_READY`
        never arrives; without this the agent would sit suspended forever.
        """
        self._cancel_narration_watchdog()
        self._narration_watchdog = asyncio.create_task(
            self._narration_timeout(), name="dialog.narration-hold"
        )

    def _cancel_narration_watchdog(self) -> None:
        task = self._narration_watchdog
        self._narration_watchdog = None
        if task is not None and not task.done():
            task.cancel()

    async def _narration_timeout(self) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(_NARRATION_HOLD_TIMEOUT_S)
            log.warning(
                "narration hold: no playback report in %ss — releasing the agent",
                _NARRATION_HOLD_TIMEOUT_S,
            )
            await self._release_narration_hold()

    async def _release_narration_hold(self) -> None:
        if not self._auto_paused_by_narration:
            return
        self._cancel_narration_watchdog()
        if await self._adapter.resume():
            self._auto_paused_by_narration = False
            await self._emit_resumed("hold_while_narrating")

    async def _emit_paused(self, reason: str) -> None:
        await self._bus.publish(
            Event(
                kind=EventKind.AGENT_PAUSED,
                source="dialog.manager",
                payload={"reason": reason, "adapter": self._adapter.name},
            )
        )

    async def _emit_resumed(self, reason: str) -> None:
        await self._bus.publish(
            Event(
                kind=EventKind.AGENT_RESUMED,
                source="dialog.manager",
                payload={"reason": reason, "adapter": self._adapter.name},
            )
        )
