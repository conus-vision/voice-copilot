"""Voice Copilot plugin for Hermes Agent: your Hermes session, narrated by voice.

Reports the session to a running Voice Copilot, which speaks short updates:
your prompt, each tool call with its result, the answer, approval prompts.
Voice Copilot can also answer back: hold a tool call while you have the agent
paused, refuse it after a Supervisor STOP, or pass on a message you said by
voice (Hermes injects it into the running turn).

Install: copy this folder to ``~/.hermes/plugins/voice_copilot/`` (or run
``voice-copilot integrate hermes``) and enable it in ``~/.hermes/config.yaml``::

    plugins:
      enabled: [voice_copilot]

When Voice Copilot is not running every call fails at once and Hermes works
as usual. ``VOICE_COPILOT_HOOKS=off`` turns it off without removing it.
Standard library only.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

BASE = os.environ.get("VOICE_COPILOT_URL", "http://127.0.0.1:8765/api/companion/v1").rstrip("/")
LAUNCH = os.environ.get("VOICE_COPILOT_LAUNCH", "")
MODE = os.environ.get("VOICE_COPILOT_MODE", "")
CLI = "hermes"
GATE_TIMEOUT_S = 3600.0
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback only


def _post(path: str, body: dict[str, Any], timeout: float = 5.0) -> dict[str, Any] | None:
    """POST JSON to Voice Copilot; None when it is not reachable."""
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(body, default=str).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            raw = response.read()
    except Exception:
        return None
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _text(value: Any, limit: int = 2000) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    return text[:limit]


class _Bridge:
    """Sends events from a background thread so the agent never waits on them."""

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self._session = ""
        self._outbox: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self._poller: threading.Thread | None = None
        threading.Thread(target=self._send_loop, name="voice-copilot-events", daemon=True).start()

    def _envelope(self, session_id: str = "") -> dict[str, Any]:
        return {
            "cli": CLI,
            "session_id": session_id or self._session,
            "launch": LAUNCH,
            "mode": MODE,
            "cwd": os.getcwd(),
        }

    def _emit(self, session_id: str, kind: str, payload: dict[str, Any] | None = None) -> None:
        if session_id:
            self._session = session_id
        self._outbox.put((self._session, {"kind": kind, "payload": payload or {}}))

    def _send_loop(self) -> None:
        carry: tuple[str, dict[str, Any]] | None = None
        while True:
            session, event = carry or self._outbox.get()
            carry = None
            batch = [event]
            while len(batch) < 50:
                try:
                    following = self._outbox.get_nowait()
                except queue.Empty:
                    break
                if following[0] != session:
                    carry = following  # keeps the order: sent next round
                    break
                batch.append(following[1])
            _post("/events", {**self._envelope(session), "events": batch})

    def _poll_commands(self) -> None:
        while True:
            command = _post("/commands/next", {**self._envelope(), "wait_s": 25}, timeout=35.0)
            if command is None:
                threading.Event().wait(5.0)  # Voice Copilot is not running
                continue
            name = command.get("name")
            if not name:
                continue
            ok, error = True, None
            if name == "send_user_message":
                text = str((command.get("payload") or {}).get("text") or "")
                ok = bool(self._ctx.inject_message(text))
                error = None if ok else "Hermes has no interactive CLI to inject into"
            else:
                ok, error = False, f"unsupported command {name}"
            _post(
                "/commands/result",
                {
                    **self._envelope(),
                    "type": "command_result",
                    "name": name,
                    "request_id": command.get("request_id"),
                    "ok": ok,
                    "error": error,
                    "payload": command.get("payload"),
                },
            )

    # ---------------------------------------------------------------- hooks

    def on_session_start(self, session_id: str = "", **_: Any) -> None:
        self._emit(session_id, "session.started", {"cwd": os.getcwd()})
        if self._poller is None:
            self._poller = threading.Thread(
                target=self._poll_commands, name="voice-copilot-commands", daemon=True
            )
            self._poller.start()

    def pre_llm_call(self, session_id: str = "", user_message: Any = None, **_: Any) -> None:
        self._emit(session_id, "user.message", {"text": _text(user_message)})
        self._emit(session_id, "turn.started")

    def post_llm_call(self, session_id: str = "", assistant_response: Any = None, **_: Any) -> None:
        self._emit(session_id, "agent.output", {"text": _text(assistant_response)})

    def pre_tool_call(
        self,
        tool_name: str = "",
        args: Any = None,
        session_id: str = "",
        tool_call_id: str = "",
        **_: Any,
    ) -> dict[str, Any] | None:
        self._emit(
            session_id, "tool.call.started", {"id": tool_call_id, "tool": tool_name, "args": args}
        )
        verdict = _post(
            "/gate",
            {**self._envelope(session_id), "tool": tool_name, "tool_call_id": tool_call_id},
            timeout=GATE_TIMEOUT_S,
        )
        if verdict and verdict.get("decision") == "deny":
            return {"action": "block", "message": str(verdict.get("reason") or "Stopped")}
        return None

    def post_tool_call(
        self,
        tool_name: str = "",
        args: Any = None,
        result: Any = None,
        session_id: str = "",
        tool_call_id: str = "",
        status: str = "",
        error_message: Any = None,
        **_: Any,
    ) -> None:
        failed = status not in ("", "ok", "success", "succeeded") or bool(error_message)
        self._emit(
            session_id,
            "tool.call.finished",
            {
                "id": tool_call_id,
                "tool": tool_name,
                "args": args,
                "ok": not failed,
                "output": _text(error_message or result),
            },
        )

    def on_session_end(self, session_id: str = "", interrupted: bool = False, **_: Any) -> None:
        # Hermes fires this at the end of every run: one turn, not the process.
        self._emit(session_id, "turn.ended", {"final": True, "interrupted": bool(interrupted)})

    def pre_approval_request(self, command: str = "", description: str = "", **_: Any) -> None:
        self._emit(
            "", "agent.awaiting_input", {"reason": "permission", "message": description or command}
        )


def register(ctx: Any) -> None:
    if os.environ.get("VOICE_COPILOT_HOOKS", "").lower() in ("off", "0", "false"):
        return
    bridge = _Bridge(ctx)
    for hook in (
        "on_session_start",
        "pre_llm_call",
        "post_llm_call",
        "pre_tool_call",
        "post_tool_call",
        "on_session_end",
        "pre_approval_request",
    ):
        ctx.register_hook(hook, getattr(bridge, hook))
