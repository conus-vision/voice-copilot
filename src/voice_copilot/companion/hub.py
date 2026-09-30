"""Sessions that coding CLIs report through their own plugin or hook system.

A CLI with a Voice Copilot plugin (Claude Code, Pi, ...) tells us what it does
from the inside: prompts, tool calls with results, visible text, waiting for a
permission. The hub turns those reports into ordinary bus events, so the
narrator, the Supervisor and the panel treat them like proxied traffic.

It also answers back. A hook call is a moment when the CLI waits for us, so
the hub can hold a tool call while the user has the agent paused, refuse it
after a Supervisor STOP, or hand the model a message the user said by voice.
Plugins that run inside the CLI (Pi) poll for commands instead.

Two kinds of session:

* ``narrate``: the plugin is the only source, so its events are narrated.
* ``control``: this instance launched the CLI through the proxy, which already
  narrates the model traffic. The plugin then only adds what the proxy cannot
  see (the agent waiting for a permission) and the control channel.

Launch ids tie a hook call to the Voice Copilot instance that started the
CLI (``<panel port>-<random>``); a call carrying another instance's id is
ignored, so two instances never narrate the same terminal.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from voice_copilot.commentator.format import _trim
from voice_copilot.companion import dialects
from voice_copilot.companion.dialects import HookCall
from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import Event, EventKind
from voice_copilot.proxy.session import SessionRegistry
from voice_copilot.proxy.tool_events import file_paths_from_tool

log = logging.getLogger(__name__)

#: A paused agent waits at its next tool call for at most this long, then goes
#: on: a forgotten pause must not wedge a terminal forever. The plugin's hook
#: timeout is set above it.
MAX_HOLD_S = 3300.0
#: Longest a plugin's command poll is kept open.
MAX_POLL_S = 30.0
#: A session nothing was heard from for this long is forgotten. Some CLIs
#: never report their exit (Hermes, `opencode run`, a killed terminal).
STALE_AFTER_S = 1800.0
#: A permission prompt is reported twice by Claude Code (PermissionRequest at
#: once, Notification a few seconds later); speak it once.
_PERMISSION_DEDUP_S = 30.0

_VOICE_PREFIX = "Message from the user, said by voice while you were working: "


@dataclass
class Launch:
    """A CLI this instance started, so its plugin reports here."""

    id: str
    cli: str
    proxied: bool


@dataclass
class CompanionSession:
    key: str
    cli: str
    native_id: str
    transport: str  # "hook" | "plugin"
    narrate: bool
    cwd: str | None = None
    launch_id: str | None = None
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    # Narration bookkeeping.
    turn_text_seen: bool = False
    message_text: dict[str, str] = field(default_factory=dict)
    last_permission_at: float = 0.0
    # Control.
    pending: deque[str] = field(default_factory=deque)
    #: What the user sees: the agent is paused or stopped until they resume.
    paused: bool = False
    #: Tool calls wait at the gate (a pause, as opposed to a STOP).
    held: bool = False
    #: After a Supervisor STOP: every tool call is refused (agents run several
    #: at once) until the user resumes or types a new prompt.
    stop_reason: str | None = None
    resume: asyncio.Event = field(default_factory=asyncio.Event)
    commands: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)

    def delta(self, message_id: str | None, text: str) -> str:
        """New text only: some CLIs resend the whole message on every update."""
        if not message_id:
            return text
        before = self.message_text.get(message_id, "")
        self.message_text[message_id] = text if text.startswith(before) else before + text
        return text[len(before) :] if text.startswith(before) else text

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "cli": self.cli,
            "transport": self.transport,
            "mode": "narrate" if self.narrate else "control",
            "cwd": self.cwd,
            "launched_here": self.launch_id is not None,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "paused": self.paused,
            "pending_messages": len(self.pending),
        }


class CompanionHub:
    def __init__(
        self,
        bus: EventBus,
        sessions: SessionRegistry | None = None,
        *,
        port: int | None = None,
        owns_dialog: bool = False,
    ) -> None:
        self._bus = bus
        self._registry = sessions
        self._port = port
        #: True when no DialogManager runs (``serve``): the hub then routes the
        #: user's pause, interrupt and voice messages to plugin sessions.
        self.owns_dialog = owns_dialog
        self._launches: dict[str, Launch] = {}
        self._sessions: dict[str, CompanionSession] = {}
        self._cli_seen: dict[str, float] = {}

    # ------------------------------------------------------------ launches

    def new_launch(self, cli: str, *, proxied: bool) -> Launch:
        """Register a CLI about to be started; its id goes into the child's env."""
        prefix = str(self._port) if self._port else "vc"
        launch = Launch(id=f"{prefix}-{secrets.token_hex(6)}", cli=cli, proxied=proxied)
        self._launches[launch.id] = launch
        return launch

    def _resolve_launch(
        self, query_launch: str | None, header_launch: str | None, mode: str | None
    ) -> tuple[bool, bool]:
        """(accept, proxied) for a call carrying these launch markers."""
        if not query_launch:
            # A plugin that knows the launch only from the environment (the
            # permanently installed Claude plugin inside a launched terminal)
            # duplicates the one the launcher injected: ignore it.
            return (not header_launch, False)
        launch = self._launches.get(query_launch)
        if launch is not None:
            return True, launch.proxied
        if self._port is not None and query_launch.split("-", 1)[0] == str(self._port):
            # Launched by this instance before a restart.
            return True, mode == "control"
        return False, False

    # ------------------------------------------------------------ sessions

    def _session(
        self,
        cli: str,
        native_id: str,
        *,
        transport: str,
        proxied: bool,
        launch_id: str | None,
        cwd: str | None,
    ) -> tuple[CompanionSession, bool]:
        self._prune()
        seed = native_id or launch_id or cwd or cli
        if not native_id:
            native_id = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
        # Hashed, not truncated: time-ordered ids (UUIDv7, Pi's) share their
        # first digits across sessions started within the same minute.
        key = f"{cli}-{hashlib.sha1(native_id.encode('utf-8')).hexdigest()[:8]}"
        sess = self._sessions.get(key)
        created = sess is None
        if sess is None:
            sess = CompanionSession(
                key=key,
                cli=cli,
                native_id=native_id,
                transport=transport,
                narrate=not proxied,
                cwd=cwd,
                launch_id=launch_id,
            )
            self._sessions[key] = sess
            if sess.narrate and self._registry is not None:
                self._registry.register_external(key, label=f"{cli} (plugin)", cli_id=cli)
            log.info("companion: %s session %s (%s)", transport, key, sess.to_dict()["mode"])
        sess.last_seen = time.time()
        if cwd and not sess.cwd:
            sess.cwd = cwd
        self._cli_seen[cli] = sess.last_seen
        return sess, created

    def _prune(self) -> None:
        cutoff = time.time() - STALE_AFTER_S
        for sess in [s for s in self._sessions.values() if s.last_seen < cutoff]:
            log.info(
                "companion: forgetting %s (quiet for %.0f min)",
                sess.key,
                (time.time() - sess.last_seen) / 60,
            )
            self._end(sess)

    def _end(self, sess: CompanionSession) -> None:
        self._sessions.pop(sess.key, None)
        sess.resume.set()
        if sess.narrate and self._registry is not None:
            self._registry.remove(sess.key)

    async def _publish(
        self, sess: CompanionSession, kind: EventKind, payload: dict[str, Any] | None = None
    ) -> None:
        body = dict(payload or {})
        if sess.narrate:
            body["session_id"] = sess.key
        elif kind is EventKind.AGENT_AWAITING_INPUT:
            # The proxy narrates this terminal under its own session id.
            active = self._registry.get_active_id() if self._registry is not None else None
            if active:
                body["session_id"] = active
        else:
            return
        await self._bus.publish(Event(kind=kind, source=f"companion.{sess.cli}", payload=body))

    async def _started(self, sess: CompanionSession, *, reason: str = "") -> None:
        await self._publish(
            sess,
            EventKind.SESSION_STARTED,
            {"target": sess.cli, "via": sess.transport, "cwd": sess.cwd, "reason": reason},
        )

    # --------------------------------------------------------------- hooks

    async def handle_hook(
        self,
        dialect: str,
        cli: str,
        body: dict[str, Any],
        *,
        event: str | None = None,
        launch: str | None = None,
        header_launch: str | None = None,
        mode: str | None = None,
    ) -> dict[str, Any] | None:
        """Take one hook call; return the reply body, or None for an empty 200."""
        accept, proxied = self._resolve_launch(launch, header_launch, mode)
        if not accept:
            return None
        call = dialects.parse(dialect, cli, body, event)
        sess, created = self._session(
            cli,
            call.session_id,
            transport="hook",
            proxied=proxied,
            launch_id=launch,
            cwd=call.cwd,
        )
        if created:
            await self._started(sess, reason=call.native_event)
        try:
            return await self._on_hook(sess, call)
        except Exception:
            # A broken report must never break the user's agent.
            log.exception("companion: %s %s hook failed", cli, call.native_event)
            return None

    async def _on_hook(self, sess: CompanionSession, call: HookCall) -> dict[str, Any] | None:
        ev = call.event
        if ev == dialects.USER_PROMPT:
            return await self._on_prompt(sess, call)
        if ev == dialects.PRE_TOOL:
            await self._publish(
                sess,
                EventKind.TOOL_CALL_STARTED,
                {"tool": call.tool, "input": call.tool_input, "tool_use_id": call.tool_use_id},
            )
            return await self._gate_reply(sess, call)
        if ev in (dialects.POST_TOOL, dialects.POST_TOOL_FAILURE):
            await self._tool_finished(
                sess,
                call.tool,
                call.tool_input,
                call.tool_use_id,
                failed=call.tool_failed,
                output=call.tool_output,
            )
            return self._boundary_reply(sess, call)
        if ev == dialects.MESSAGE:
            text = sess.delta(call.message_id, call.text)
            if text.strip():
                sess.turn_text_seen = True
                await self._publish(sess, EventKind.AGENT_TEXT, {"text": text})
            return None
        if ev in (dialects.STOP, dialects.SUBAGENT_STOP):
            subagent = ev == dialects.SUBAGENT_STOP
            if not subagent and not sess.turn_text_seen and call.text.strip():
                await self._publish(sess, EventKind.AGENT_TEXT, {"text": call.text})
            await self._publish(
                sess, EventKind.TURN_ENDED, {"final": True, "subagent": subagent, "via": "hook"}
            )
            if subagent:
                return None
            sess.turn_text_seen = False
            sess.message_text.clear()
            return self._stop_reply(sess, call)
        if ev == dialects.STOP_FAILURE:
            await self._publish(sess, EventKind.ERROR, {"message": call.notice or "turn failed"})
            await self._publish(sess, EventKind.TURN_ENDED, {"final": True, "via": "hook"})
            return None
        if ev == dialects.INTERRUPT:
            await self._publish(
                sess, EventKind.TURN_ENDED, {"final": True, "interrupted": True, "via": "hook"}
            )
            return None
        if ev in (dialects.PERMISSION_REQUEST, dialects.NOTIFICATION):
            await self._on_notice(sess, call)
            return self._boundary_reply(sess, call)
        if ev == dialects.SESSION_START:
            return self._boundary_reply(sess, call)
        if ev == dialects.SESSION_END:
            await self._publish(sess, EventKind.SESSION_ENDED, {"reason": call.notice})
            self._end(sess)
            return None
        return None

    async def _on_prompt(self, sess: CompanionSession, call: HookCall) -> dict[str, Any] | None:
        sess.turn_text_seen = False
        sess.message_text.clear()
        # The user typed into the terminal: they have taken over.
        await self._release(sess, "user_prompt")
        if sess.narrate and self._registry is not None and call.prompt.strip():
            self._registry.observe_query(sess.key, call.prompt)
        await self._publish(sess, EventKind.TURN_STARTED, {"via": "hook"})
        if call.prompt.strip():
            await self._publish(
                sess, EventKind.USER_MESSAGE, {"text": call.prompt, "delivery": "observed"}
            )
        return await self._deliver_as_context(sess, call)

    async def _on_notice(self, sess: CompanionSession, call: HookCall) -> None:
        now = time.time()
        if call.event == dialects.PERMISSION_REQUEST:
            sess.last_permission_at = now
            await self._publish(
                sess,
                EventKind.AGENT_AWAITING_INPUT,
                {"reason": "permission", "tool": call.tool, "input": call.tool_input},
            )
            return
        kind = call.notice_type.lower()
        wants_user = "permission" in kind or "elicitation" in kind or kind == "toolpermission"
        if not wants_user:
            return  # idle reminders, auth notices: nothing to narrate
        if now - sess.last_permission_at < _PERMISSION_DEDUP_S:
            return
        sess.last_permission_at = now
        await self._publish(
            sess, EventKind.AGENT_AWAITING_INPUT, {"reason": "permission", "message": call.notice}
        )

    async def _tool_finished(
        self,
        sess: CompanionSession,
        tool: str,
        tool_input: Any,
        tool_use_id: str | None,
        *,
        failed: bool,
        output: str,
    ) -> None:
        await self._publish(
            sess,
            EventKind.TOOL_CALL_FINISHED,
            {
                "tool": tool,
                "is_error": failed,
                "preview": _trim(output, 400),
                "tool_use_id": tool_use_id,
            },
        )
        if not failed:
            for path in file_paths_from_tool(tool, tool_input):
                await self._publish(sess, EventKind.FILE_EDITED, {"path": path, "via": "hook"})

    # -------------------------------------------------------------- replies

    async def _gate_reply(self, sess: CompanionSession, call: HookCall) -> dict[str, Any] | None:
        verdict = await self._gate(sess)
        if verdict is None:
            return None
        return dialects.deny(call, verdict, halt=True)

    def _boundary_reply(self, sess: CompanionSession, call: HookCall) -> dict[str, Any] | None:
        """At a tool boundary: end the turn after a STOP, else hand over a voice message."""
        if sess.stop_reason and call.event in (dialects.POST_TOOL, dialects.POST_TOOL_FAILURE):
            return dialects.halt(call, sess.stop_reason) or None
        if not sess.pending:
            return None
        reply = dialects.add_context(call, _VOICE_PREFIX + sess.pending[0])
        if reply is not None:
            self._delivered(sess, sess.pending.popleft(), "tool_boundary")
        return reply

    def _stop_reply(self, sess: CompanionSession, call: HookCall) -> dict[str, Any] | None:
        if not sess.pending or sess.stop_reason:
            return None
        text = "\n".join(sess.pending)
        reply = dialects.keep_going(call, _VOICE_PREFIX + text)
        if reply is not None:
            while sess.pending:
                self._delivered(sess, sess.pending.popleft(), "turn_end")
        return reply

    async def _deliver_as_context(
        self, sess: CompanionSession, call: HookCall
    ) -> dict[str, Any] | None:
        if not sess.pending:
            return None
        reply = dialects.add_context(call, _VOICE_PREFIX + "\n".join(sess.pending))
        if reply is not None:
            while sess.pending:
                self._delivered(sess, sess.pending.popleft(), "with_prompt")
        return reply

    def _delivered(self, sess: CompanionSession, text: str, how: str) -> None:
        task = asyncio.get_running_loop().create_task(
            self._bus.publish(
                Event(
                    kind=EventKind.USER_MESSAGE,
                    source="companion.delivery",
                    payload={"text": text, "delivery": "sent", "via": how, "cli": sess.cli},
                )
            )
        )
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    # --------------------------------------------------------------- gate

    async def _gate(self, sess: CompanionSession) -> str | None:
        """None to let the tool run, or the reason to refuse it and stop."""
        if sess.stop_reason:
            return sess.stop_reason
        if not sess.held:
            return None
        log.info("companion: holding a %s tool call while paused", sess.cli)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(sess.resume.wait(), timeout=MAX_HOLD_S)
        return sess.stop_reason

    # ------------------------------------------------------ native plugins

    async def handle_events(
        self,
        cli: str,
        native_id: str,
        events: list[dict[str, Any]],
        *,
        launch: str | None = None,
        mode: str | None = None,
        cwd: str | None = None,
    ) -> bool:
        """Take RFC 0001 events from a plugin running inside the CLI."""
        accept, proxied = self._resolve_launch(launch, None, mode)
        if not accept:
            return False
        sess, created = self._session(
            cli, native_id, transport="plugin", proxied=proxied, launch_id=launch, cwd=cwd
        )
        if created:
            await self._started(sess)
        for raw in events:
            try:
                await self._on_rfc_event(sess, raw)
            except Exception:
                log.exception("companion: bad %s event %r", cli, raw.get("kind"))
        return True

    async def _on_rfc_event(self, sess: CompanionSession, raw: dict[str, Any]) -> None:
        kind = str(raw.get("kind") or "")
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
        assert isinstance(payload, dict)
        text = str(payload.get("text") or payload.get("delta") or "")
        if kind == "session.started":
            if payload.get("cwd"):
                sess.cwd = str(payload["cwd"])
        elif kind == "session.ended":
            await self._publish(sess, EventKind.SESSION_ENDED, {})
            self._end(sess)
        elif kind == "turn.started":
            await self._publish(sess, EventKind.TURN_STARTED, {"via": "plugin"})
        elif kind == "turn.ended":
            await self._publish(
                sess,
                EventKind.TURN_ENDED,
                {"final": payload.get("final", True) is not False, "via": "plugin"},
            )
        elif kind == "user.message":
            await self._release(sess, "user_prompt")
            if text.strip():
                if sess.narrate and self._registry is not None:
                    self._registry.observe_query(sess.key, text)
                await self._publish(
                    sess, EventKind.USER_MESSAGE, {"text": text, "delivery": "observed"}
                )
        elif kind in ("agent.output", "agent.text"):
            if text:
                await self._publish(sess, EventKind.AGENT_TEXT, {"text": text})
        elif kind == "agent.thinking":
            if text:
                await self._publish(sess, EventKind.AGENT_THINKING, {"text": text})
        elif kind == "agent.awaiting_input":
            await self._publish(sess, EventKind.AGENT_AWAITING_INPUT, dict(payload))
        elif kind == "tool.call.started":
            await self._publish(
                sess,
                EventKind.TOOL_CALL_STARTED,
                {
                    "tool": payload.get("tool"),
                    "input": payload.get("args", payload.get("input")),
                    "tool_use_id": payload.get("id"),
                },
            )
        elif kind == "tool.call.finished":
            await self._tool_finished(
                sess,
                str(payload.get("tool") or ""),
                payload.get("args", payload.get("input")),
                payload.get("id"),
                failed=payload.get("ok") is False or bool(payload.get("is_error")),
                output=str(payload.get("output") or payload.get("error") or ""),
            )
        elif kind == "file.edited":
            if payload.get("path"):
                await self._publish(
                    sess, EventKind.FILE_EDITED, {"path": payload["path"], "via": "plugin"}
                )
        elif kind == "error":
            await self._publish(
                sess, EventKind.ERROR, {"message": str(payload.get("message") or "error")}
            )

    async def gate(
        self,
        cli: str,
        native_id: str,
        *,
        launch: str | None = None,
        mode: str | None = None,
    ) -> dict[str, Any]:
        """Tool-call gate for plugins: allow, or deny and stop."""
        accept, proxied = self._resolve_launch(launch, None, mode)
        if not accept or not native_id:
            return {"decision": "allow"}
        sess, _ = self._session(
            cli, native_id, transport="plugin", proxied=proxied, launch_id=launch, cwd=None
        )
        reason = await self._gate(sess)
        if reason is None:
            return {"decision": "allow"}
        return {"decision": "deny", "reason": reason, "stop": True}

    async def next_command(
        self,
        cli: str,
        native_id: str,
        *,
        wait_s: float,
        launch: str | None = None,
        mode: str | None = None,
    ) -> dict[str, Any] | None:
        accept, proxied = self._resolve_launch(launch, None, mode)
        if not accept or not native_id:
            # A plugin polling before its session started: nothing to deliver
            # yet, and no phantom session to route the user's controls to.
            await asyncio.sleep(min(wait_s, MAX_POLL_S))
            return None
        sess, _ = self._session(
            cli, native_id, transport="plugin", proxied=proxied, launch_id=launch, cwd=None
        )
        try:
            return await asyncio.wait_for(sess.commands.get(), timeout=min(wait_s, MAX_POLL_S))
        except TimeoutError:
            return None

    async def command_result(self, cli: str, native_id: str, result: dict[str, Any]) -> None:
        name = result.get("name")
        ok = bool(result.get("ok"))
        log.info("companion: %s answered %s (ok=%s)", cli, name, ok)
        if name == "send_user_message":
            text = str((result.get("payload") or {}).get("text") or result.get("text") or "")
            await self._bus.publish(
                Event(
                    kind=EventKind.USER_MESSAGE if ok else EventKind.ERROR,
                    source="companion.delivery",
                    payload=(
                        {"text": text, "delivery": "sent", "via": "plugin", "cli": cli}
                        if ok
                        else {"where": "deliver", "message": str(result.get("error") or "")}
                    ),
                )
            )

    # ------------------------------------------------------------ control

    def _target(self) -> CompanionSession | None:
        """The session the user's pause, interrupt or voice message is for."""
        if not self._sessions:
            return None
        active = self._registry.get_active_id() if self._registry is not None else None
        if active and active in self._sessions:
            return self._sessions[active]
        return max(self._sessions.values(), key=lambda s: s.last_seen)

    async def _paused_event(self, kind: EventKind, reason: str) -> None:
        await self._bus.publish(
            Event(
                kind=kind, source="companion.hub", payload={"reason": reason, "adapter": "plugin"}
            )
        )

    async def send_message(self, text: str, *, urgent: bool = False) -> bool:
        """Route a voice or typed message to the agent. False if nobody can take it."""
        sess = self._target()
        if sess is None:
            return False
        if sess.transport == "plugin":
            await sess.commands.put(
                {
                    "type": "command",
                    "name": "send_user_message",
                    "session_id": sess.native_id,
                    "request_id": secrets.token_hex(4),
                    "payload": {"text": text, "urgent": urgent},
                }
            )
        else:
            sess.pending.append(text)
        await self._bus.publish(
            Event(
                kind=EventKind.USER_MESSAGE,
                source="companion.delivery",
                payload={"text": text, "delivery": "queued", "cli": sess.cli},
            )
        )
        return True

    async def pause(self, reason: str) -> bool:
        """Hold the agent at its next tool call until the user resumes it."""
        sess = self._target()
        if sess is None or sess.paused:
            return False
        sess.paused = True
        sess.held = True
        sess.resume.clear()
        await self._paused_event(EventKind.AGENT_PAUSED, reason)
        return True

    async def resume(self, reason: str) -> bool:
        sess = self._target()
        return sess is not None and await self._release(sess, reason)

    async def _release(self, sess: CompanionSession, reason: str) -> bool:
        if not sess.paused:
            return False
        sess.paused = False
        sess.held = False
        sess.stop_reason = None
        sess.resume.set()
        await self._paused_event(EventKind.AGENT_RESUMED, reason)
        return True

    async def stop(self, message: str) -> bool:
        """Supervisor STOP: refuse the next tool call and end the agent's turn."""
        sess = self._target()
        if sess is None:
            return False
        sess.stop_reason = f"Voice Copilot supervisor stopped the agent: {message}".strip()
        sess.paused = True
        sess.held = False
        sess.resume.set()  # a held call wakes up and takes the STOP
        if sess.transport == "plugin":
            await sess.commands.put(
                {"type": "command", "name": "interrupt", "session_id": sess.native_id}
            )
        await self._paused_event(EventKind.AGENT_PAUSED, "supervisor")
        return True

    async def run(self) -> None:
        """Route the user's controls to plugin sessions when no DialogManager does."""
        if not self.owns_dialog:
            return
        async with self._bus.subscribe() as q:
            while True:
                ev = await q.get()
                try:
                    await self._on_control(ev)
                except Exception:
                    log.exception("companion control failed on %s", ev.kind)

    async def _on_control(self, ev: Event) -> None:
        k = ev.kind
        if k is EventKind.USER_MESSAGE and ev.source.startswith(("stt.", "web", "hotkey")):
            text = str(ev.payload.get("text") or "").strip()
            if text:
                await self.send_message(text, urgent=bool(ev.payload.get("urgent")))
        elif k is EventKind.USER_PAUSE_TOGGLE:
            sess = self._target()
            if sess is not None and sess.paused:
                await self.resume("toggle")
            else:
                await self.pause("toggle")
        elif k is EventKind.USER_INTERRUPT:
            await self.pause("interrupt")
        elif k is EventKind.SUPERVISOR_STOP:
            await self.stop(str(ev.payload.get("message") or ""))

    # ------------------------------------------------------------- status

    def status(self) -> dict[str, Any]:
        self._prune()
        return {
            "sessions": [s.to_dict() for s in self._sessions.values()],
            "clis_seen": dict(self._cli_seen),
            "owns_dialog": self.owns_dialog,
        }

    def session_count(self) -> int:
        return len(self._sessions)
