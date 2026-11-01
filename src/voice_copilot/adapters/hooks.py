"""Plugin-hooks adapter — attach to a CLI the user started themselves.

Unlike the other adapters this one spawns nothing: the CLI's plugin calls us on
each hook (see `companion/hook_forward.py`), for any number of sessions. It covers
what a plugin gets without a proxy, in the TUI and in the IDE extension alike:

  * events: hooks, plus the agent's own words from its transcript;
  * user messages: a plugin that can say them as the user asks for them (`take_mail`).
    For the rest they wait in a mailbox until the next tool call, which is refused
    with the message as the reason, or until the turn is about to end, which then
    continues with it (`QuickAsideCapability.STEP`). An idle session is woken with
    a real user message where the CLI allows it (`companion/wake.py`);
  * pause: we don't own the process to suspend it, so the next `PreToolUse` hook
    simply doesn't answer until resume. The agent stops before its next action,
    which is what Supervisor+ and the hold-while-narrating mode need.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from voice_copilot.adapters.base import CLIAdapter, QuickAsideCapability
from voice_copilot.companion.normalize import events_from_hook
from voice_copilot.companion.transcript import TranscriptTail
from voice_copilot.companion.wake import WAKERS, Waker
from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import Event, EventKind
from voice_copilot.proxy.session import SessionRegistry

log = logging.getLogger(__name__)

#: Longest one hook may be held. The CLIs kill a hook at 600 s, and a killed hook
#: tells us nothing, so we let go first and say so.
HOLD_TIMEOUT_S = 540.0

#: Plain statements of where the text came from. A model weighs hook text against
#: its task and may decline it, which is right: a file could say the same words.
#:
#: Why refuse a tool call rather than add context to one: measured on Claude Code,
#: models acted on a refusal's reason in 7 of 7 runs, and in only 3 of 7 on the same
#: correction as `additionalContext` after a tool result, where they mostly took it
#: for an injection from the tool's output. See docs/plugins.md.
_MESSAGE_FRAME = "Message from the user, relayed by the Voice Copilot plugin:\n"
_REFUSAL_FRAME = (
    "The user stopped this tool call through the Voice Copilot plugin, so it did not run. "
    "Their message:\n"
)

#: CLIs whose hooks don't say how a command ended. Without this a failed command would
#: reach the narrator and the supervisor as "ok".
_EXIT_CODE_IN_TRANSCRIPT = {"codex"}

#: A plugin polling for mail is alive if it did so this recently. Past that it was
#: unloaded or broke, and the hooks take over so that no message is stranded.
_MAIL_READER_TTL_S = 5.0

#: These exit with 1 when they simply find nothing, which isn't a failure to report.
_SEARCH_COMMAND = re.compile(r"(?:^|[;&|]\s*)(?:rg|grep|egrep|fgrep|findstr|git grep)\b")


def _found_nothing(code: int, body: dict[str, Any], command: Any) -> bool:
    return (
        code == 1
        and not str(body.get("preview") or "").strip()
        and isinstance(command, str)
        and _SEARCH_COMMAND.search(command) is not None
    )


@dataclass
class _HookSession:
    cli: str
    tail: TranscriptTail | None = None
    tail_path: str | None = None
    last_text: str | None = None
    #: What a hook said before the transcript caught up, so that it isn't said twice.
    hook_said: str | None = None
    mailbox: list[str] = field(default_factory=list)
    #: The last turn is over and no new one has started.
    idle: bool = False
    #: A turn is running. Not `not idle`: a session with no turn yet is neither.
    working: bool = False
    #: Finished tools waiting for their exit code to reach the transcript, by tool id.
    unsettled: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: The commands behind them, by tool id: an exit code means little without one.
    commands: dict[str, Any] = field(default_factory=dict)
    #: Reported from the transcript. A late `PostToolUse` for one of these says nothing new.
    finished_from_transcript: set[str] = field(default_factory=set)
    #: When its plugin last asked for mail, if it ever did.
    mail_read_at: float | None = None
    #: What to show the user in the CLI: an answer, or a warning nobody is there to hear.
    notices: list[str] = field(default_factory=list)

    @property
    def has_mail_reader(self) -> bool:
        return (
            self.mail_read_at is not None
            and time.monotonic() - self.mail_read_at < _MAIL_READER_TTL_S
        )


class HookAdapter(CLIAdapter):
    name = "hooks"
    quick_aside = QuickAsideCapability.STEP

    def __init__(
        self,
        bus: EventBus,
        registry: SessionRegistry,
        *,
        hold_timeout_s: float = HOLD_TIMEOUT_S,
        on_active_cli: Callable[[str | None], None] | None = None,
        wakers: dict[str, Waker] | None = None,
        greeting: Callable[[], str] | None = None,
    ) -> None:
        self._bus = bus
        self._registry = registry
        self._hold_timeout_s = hold_timeout_s
        self._on_active_cli = on_active_cli
        self._wakers = WAKERS if wakers is None else wakers
        self._greeting = greeting
        self._sessions: dict[str, _HookSession] = {}
        self._active_cli: str | None = None
        self._resume = asyncio.Event()
        self._resume.set()
        self._last_hook = time.monotonic()

    # ------------------------------------------------------------------ CLIAdapter

    async def start(self, initial_prompt: str | None = None) -> None:
        return None  # The user starts the CLI; its plugin finds us.

    async def stop(self) -> None:
        self._resume.set()  # Never leave an agent waiting on a dying server.

    async def send_user_message(self, text: str, *, urgent: bool = False) -> None:
        # `urgent` changes nothing: every message already stops the next tool call.
        sid = self._registry.get_active_id()
        session = self._sessions.get(sid) if sid else None
        if sid is None or session is None:
            raise RuntimeError("no attached agent session to deliver the message to")
        waker = self._wakers.get(session.cli) if session.idle else None
        if waker is None or session.has_mail_reader:
            session.mailbox.append(text)
            return
        source = f"hooks.{session.cli}"
        try:
            await waker(sid, text)
        except Exception as exc:
            # Not lost: it still goes with the next prompt. The user should know why it waits.
            session.mailbox.append(text)
            await self._bus.publish(
                Event(
                    kind=EventKind.ERROR,
                    source=source,
                    payload={
                        "session_id": sid,
                        "where": "deliver",
                        "message": f"Couldn't wake the agent, so this waits for your next "
                        f"prompt: {exc}",
                    },
                )
            )
            return
        await self._announce_delivery(sid, source, text, "queue")

    async def take_mail(self, cli: str, sid: str) -> list[str]:
        """Hand over what the user said, for a plugin that can say it as the user.

        Polled, so that hooks know to leave these messages alone. During a pause they
        stay here: the held tool call is about to run, and the refusal stops it.
        """
        session = self._sessions.get(sid)
        if session is None:
            return []
        session.mail_read_at = time.monotonic()
        if not session.mailbox or self._paused:
            return []
        pending, session.mailbox = session.mailbox, []
        await self._announce_delivery(sid, f"hooks.{cli}", "\n\n".join(pending), "plugin")
        return pending

    def notify(self, sid: str | None, text: str) -> None:
        """Show this in the session's CLI as soon as it has a way to."""
        session = self._sessions.get(sid or self._registry.get_active_id() or "")
        if session is not None:
            session.notices.append(text)

    def take_notices(self, sid: str) -> list[str]:
        session = self._sessions.get(sid)
        if session is None:
            return []
        pending, session.notices = session.notices, []
        return pending

    def is_working(self, sid: str) -> bool:
        session = self._sessions.get(sid)
        return session is not None and session.working

    async def pause(self) -> bool:
        if self._paused:
            return False
        self._paused = True
        self._resume.clear()
        return True

    async def resume(self) -> bool:
        if not self._paused:
            return False
        self._paused = False
        self._resume.set()
        return True

    async def switch_off(self, *, by_agent: bool = False, session_id: str | None = None) -> None:
        """Stop narrating one session or all, as of now: the hooks go quiet once the
        switch file is written.

        Sessions are let go rather than left waiting. If the user turns this back on,
        the next hook attaches them again from the end of their transcript, so what
        happened in between isn't narrated after the fact.
        """
        was_paused = self._paused
        left = [sid for sid in self._sessions if session_id not in (None, sid)]
        if was_paused and not left:
            # A pause holds whichever session is active, so it ends with the last of them.
            self._paused = False
            self._resume.set()
            await self._bus.publish(
                Event(kind=EventKind.AGENT_RESUMED, source="hooks", payload={"reason": "switch"})
            )
        for sid, session in list(self._sessions.items()):
            if sid in left:
                continue
            source = f"hooks.{session.cli}"
            await self._bus.publish(
                Event(
                    kind=EventKind.SESSION_ENDED,
                    source=source,
                    payload={"session_id": sid, "reason": "switched off"},
                )
            )
            await self._detach(sid, source)
        if by_agent:
            await self._bus.publish(
                Event(
                    kind=EventKind.ERROR,
                    source="hooks",
                    payload={"where": "switch", "message": "The agent turned Voice Copilot off."},
                )
            )
        stop = {"session_id": session_id} if session_id else {}
        await self._bus.publish(Event(kind=EventKind.NARRATION_STOP, source="hooks", payload=stop))

    # ------------------------------------------------------------------ state

    @property
    def session_count(self) -> int:
        return len(self._sessions)

    @property
    def active_cli(self) -> str | None:
        return self._active_cli

    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_hook

    # ------------------------------------------------------------------ hooks

    async def handle_hook(self, cli: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Publish what this hook reports and return the CLI's instructions, if any."""
        self._last_hook = time.monotonic()
        sid = payload.get("session_id")
        event = payload.get("hook_event_name")
        if not isinstance(sid, str) or not sid:
            log.warning("hook without session_id from %s: %s", cli, event)
            return None
        if event == "SessionEnd" and sid not in self._sessions:
            # One we never met (Codex's panel ends a chat it never used) or already let go.
            return None

        session = self._attach(cli, sid, payload)
        source = f"hooks.{cli}"
        await self._publish_said(session, sid, source, payload)

        reply: dict[str, Any] | None = None
        hook_events = events_from_hook(cli, payload)
        if event == "Stop":
            # Decide first: a stop we block isn't the end of the turn, and neither
            # the narrator nor the supervisor should treat it as one.
            reply = await self._take_stop_reply(session, sid, source, payload)
            hook_events.append((EventKind.TURN_ENDED, {"session_id": sid, "final": reply is None}))
            session.idle = reply is None and not payload.get("agent_id")
            if session.idle:
                session.working = False
        elif event in ("PreToolUse", "UserPromptSubmit") and not payload.get("agent_id"):
            session.idle = False
            session.working = True
        elif event == "Interrupt":
            session.working = False
        for kind, body in hook_events:
            tool_id = body.get("tool_use_id")
            if kind is EventKind.TOOL_CALL_STARTED and cli in _EXIT_CODE_IN_TRANSCRIPT:
                tool_input = body.get("input")
                session.commands[str(tool_id)] = (
                    tool_input.get("command") if isinstance(tool_input, dict) else None
                )
            if kind is EventKind.TOOL_CALL_FINISHED and tool_id in session.finished_from_transcript:
                session.finished_from_transcript.discard(str(tool_id))
                continue
            if (
                kind is EventKind.TOOL_CALL_FINISHED
                and cli in _EXIT_CODE_IN_TRANSCRIPT
                and session.tail is not None
                and isinstance(tool_id, str)
            ):
                session.unsettled[tool_id] = body
                await self._settle_results(session, sid, source, event)
            else:
                await self._bus.publish(Event(kind=kind, source=source, payload=body))

        held = self._paused
        if event == "PreToolUse" and sid == self._registry.get_active_id():
            await self._hold_if_paused(sid, source)
        # Only a tool call that was held has a reason to take the mail: it's about to run.
        leave_to_plugin = session.has_mail_reader and not (held and event == "PreToolUse")
        if (
            event in ("PreToolUse", "UserPromptSubmit")
            and not payload.get("agent_id")
            and not leave_to_plugin
        ):
            # After the hold: what was said during the pause goes out now.
            reply = await self._take_reply(session, sid, source, event)
        if event == "SessionEnd":
            await self._detach(sid, source)
        self._sync_active_cli()
        if event == "SessionStart" and self._greeting is not None:
            session.notices.insert(0, self._greeting())
        if session.notices and event != "SessionEnd" and not session.has_mail_reader:
            reply = {**(reply or {}), "systemMessage": "\n".join(session.notices)}
            session.notices = []
        return reply

    def _attach(self, cli: str, sid: str, payload: dict[str, Any]) -> _HookSession:
        session = self._sessions.get(sid)
        if session is None:
            session = _HookSession(cli=cli)
            self._sessions[sid] = session
        self._registry.register_external(sid, label=cli, cli_id=cli)
        event = payload.get("hook_event_name")
        if event == "UserPromptSubmit":
            prompt = payload.get("prompt")
            self._registry.observe_request(
                sid,
                method="hook",
                path=str(event),
                request_bytes=0,
                query=prompt if isinstance(prompt, str) else None,
            )
        path = payload.get("transcript_path")
        if isinstance(path, str) and path and path != session.tail_path:
            # A fresh session is read from its first line. Anything else (a resume,
            # or VC started mid-session) has history that isn't news.
            fresh = event == "SessionStart" and payload.get("source") in (None, "startup")
            session.tail = TranscriptTail(Path(path), from_start=fresh)
            session.tail_path = path
        return session

    async def _publish_said(
        self, session: _HookSession, sid: str, source: str, payload: dict[str, Any]
    ) -> None:
        if session.tail is not None:
            said = await asyncio.to_thread(session.tail.read_new)
            # Results first: those commands ended before the agent said anything about them.
            await self._settle_results(session, sid, source, payload.get("hook_event_name"))
            for kind, text in said:
                if kind is EventKind.AGENT_TEXT:
                    echo, session.hook_said = session.hook_said, None
                    if text.strip() == (echo or "").strip():
                        continue  # The hook already gave us this one, ahead of the transcript.
                    session.last_text = text
                await self._bus.publish(
                    Event(kind=kind, source=source, payload={"session_id": sid, "text": text})
                )
        # The transcript may lag the turn's end; the hook itself has the final word.
        # A sub-agent's isn't the agent's: Claude Code also forks one to suggest the next prompt.
        last = None if payload.get("agent_id") else payload.get("last_assistant_message")
        if (
            isinstance(last, str)
            and last.strip()
            and last.strip() != (session.last_text or "").strip()
        ):
            session.last_text = last
            session.hook_said = last
            await self._bus.publish(
                Event(
                    kind=EventKind.AGENT_TEXT,
                    source=source,
                    payload={"session_id": sid, "text": last},
                )
            )

    async def _settle_results(
        self, session: _HookSession, sid: str, source: str, event: Any
    ) -> None:
        """Publish finished tools whose exit code is known, in the order they finished.

        Codex records a command after its `PostToolUse` hook returns, so the code usually
        turns up on the next hook. A sibling's `PostToolUse` proves nothing, as parallel
        calls may still be writing. Any later hook does, and past that point an
        unknown code (not a shell command, or a new format) stops holding the event up.
        """
        codes = session.tail.exit_codes if session.tail is not None else {}
        for tool_id, body in list(session.unsettled.items()):
            code = codes.get(tool_id)
            if code is None and event == "PostToolUse":
                continue
            del session.unsettled[tool_id]
            command = session.commands.pop(tool_id, None)
            if code and not _found_nothing(code, body, command):
                body = {
                    **body,
                    "is_error": True,
                    "preview": body.get("preview") or f"exit code {code}",
                }
            await self._bus.publish(
                Event(kind=EventKind.TOOL_CALL_FINISHED, source=source, payload=body)
            )
        await self._finish_silent_commands(session, sid, source)

    async def _finish_silent_commands(self, session: _HookSession, sid: str, source: str) -> None:
        """Report commands that ended with no `PostToolUse`: the transcript is all there is.

        Codex stops waiting for a command after 10 s or so, and one that ends after that
        fires no hook, which is exactly what a slow failing test run does. A hook may still
        follow the transcript's line, so whichever comes second is dropped.
        """
        if session.tail is None:
            return
        for tool_id, command in list(session.commands.items()):
            code = session.tail.exit_codes.get(tool_id)
            if code is None or tool_id in session.unsettled:
                continue
            del session.commands[tool_id]
            session.finished_from_transcript.add(tool_id)
            output = session.tail.outputs.get(tool_id, "")
            body: dict[str, Any] = {
                "session_id": sid,
                "tool_use_id": tool_id,
                "tool": "Bash",
                "is_error": False,
                "preview": output,
            }
            if code and not _found_nothing(code, body, command):
                body = {**body, "is_error": True, "preview": output or f"exit code {code}"}
            await self._bus.publish(
                Event(kind=EventKind.TOOL_CALL_FINISHED, source=source, payload=body)
            )

    async def _hold_if_paused(self, sid: str, source: str) -> None:
        if self._resume.is_set():
            return
        try:
            await asyncio.wait_for(self._resume.wait(), timeout=self._hold_timeout_s)
        except TimeoutError:
            await self._bus.publish(
                Event(
                    kind=EventKind.ERROR,
                    source=source,
                    payload={
                        "session_id": sid,
                        "where": "hold",
                        "message": (
                            f"Agent let through after a {self._hold_timeout_s:.0f} s pause: "
                            "a CLI kills a hook that runs longer. Still paused: "
                            "the next tool call is held again."
                        ),
                    },
                )
            )

    def _drain(self, session: _HookSession) -> str:
        pending, session.mailbox = session.mailbox, []
        return "\n\n".join(pending)

    async def _announce_delivery(self, sid: str, source: str, text: str, via: str) -> None:
        await self._bus.publish(
            Event(
                kind=EventKind.USER_MESSAGE,
                source=source,
                payload={"session_id": sid, "text": text, "delivery": "delivered", "via": via},
            )
        )

    async def _take_reply(
        self, session: _HookSession, sid: str, source: str, event: str
    ) -> dict[str, Any] | None:
        if not session.mailbox:
            return None
        text = self._drain(session)
        await self._announce_delivery(sid, source, text, event)
        if event == "PreToolUse":
            return {
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "permissionDecision": "deny",
                    "permissionDecisionReason": _REFUSAL_FRAME + text,
                }
            }
        # Said while idle, so it goes with the prompt the user just typed.
        return {
            "hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": _MESSAGE_FRAME + text,
            }
        }

    async def _take_stop_reply(
        self, session: _HookSession, sid: str, source: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        if not session.mailbox or payload.get("agent_id") or session.has_mail_reader:
            return None
        text = self._drain(session)
        await self._announce_delivery(sid, source, text, "Stop")
        return {"decision": "block", "reason": _MESSAGE_FRAME + text}

    async def _detach(self, sid: str, source: str) -> None:
        session = self._sessions.pop(sid, None)
        self._registry.remove(sid)
        if session is not None and session.mailbox:
            lost = self._drain(session)
            await self._bus.publish(
                Event(
                    kind=EventKind.ERROR,
                    source=source,
                    payload={
                        "session_id": sid,
                        "where": "deliver",
                        "message": f"Session ended before this message was delivered: {lost}",
                    },
                )
            )

    def _sync_active_cli(self) -> None:
        sid = self._registry.get_active_id()
        session = self._sessions.get(sid) if sid else None
        cli = session.cli if session else None
        # With no session left, keep the last CLI: the narrator is still
        # finishing that session's last line through it.
        if cli is None or cli == self._active_cli:
            return
        self._active_cli = cli
        if self._on_active_cli is not None:
            with contextlib.suppress(Exception):
                self._on_active_cli(cli)
