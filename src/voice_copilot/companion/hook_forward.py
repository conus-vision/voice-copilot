"""`voice-copilot-hook <cli>` — what a CLI's plugin runs on every hook.

Reads the hook's JSON from stdin, hands it to the companion and prints the
companion's reply for the CLI to act on. Two rules shape everything here:

  * It runs on every tool call, so it imports nothing heavy.
  * It must never break the agent: whatever goes wrong, the exit code is 0 and
    the agent carries on. Only `SessionStart` reports a failure, once, as a
    message the user sees, instead of repeating it on every tool call.

It also owns the `vc ...` words, because a hook sees a request before anyone else: they
are stopped here and never reach the model, and hooks run outside the agent's sandbox, so
the switch works where the agent itself couldn't write. See `commands.py` for what counts.

Plugins that run inside their CLI call `voice-copilot-hook --chat` for the same thing.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from voice_copilot.companion.commands import Command, parse, say
from voice_copilot.companion.runtime import (
    NO_HOOKS_ENV,
    TOKEN_HEADER,
    RuntimeInfo,
    is_switched_off,
    language,
    read_runtime,
    runtime_dir,
    runtime_file,
    set_session_switched_off,
    set_switched_off,
)

#: A hook that is being held (the agent is paused) answers only on resume. The
#: CLIs kill a hook at 600 s and the server lets go before that.
_REPLY_TIMEOUT_S = 590.0
#: Importing the server takes a moment on a cold start.
_START_TIMEOUT_S = 20.0
_LOCK_STALE_S = 30.0
#: Session-scoped variables of the CLI that spawned this hook. The companion
#: outlives that session, and its narrator must not look nested in it. Auth
#: and config variables such as CLAUDE_CODE_USE_BEDROCK stay.
_SESSION_ENV = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_ENV_FILE",
    "CLAUDE_PLUGIN_ROOT",
    "CLAUDE_PLUGIN_DATA",
    "CLAUDE_PROJECT_DIR",
    "PLUGIN_ROOT",
    "PLUGIN_DATA",
)

#: `--chat` says this when the text isn't a command, so that the plugin lets it through.
_NOT_A_COMMAND_EXIT = 3
#: Blocking a prompt typed during a turn ends that turn here, with the agent's work unfinished.
#: A `vc` word mustn't cost that, so the model is told to ignore it instead.
_BLOCKING_ENDS_THE_TURN = {"codex"}
_IGNORE_THE_SWITCH = (
    "The user's message above is a command for the Voice Copilot plugin, and the plugin has "
    "handled it. Don't answer it or mention it. Carry on with the task."
)
#: Run by the agent when asked in words (the plugin's skill tells it to).
_AGENT_SWITCH = r"\s*voice-copilot\s+switch\s+(on|off|status)\s*;?\s*"


def _post(info: RuntimeInfo, path: str, body: bytes | None) -> bytes:
    """POST to the companion, or GET with no body. Raises OSError if unreachable."""
    # Here and below, imported when needed: http.client alone is a third of the start-up,
    # and while switched off no hook gets this far.
    import http.client

    conn = http.client.HTTPConnection(info.host, info.port, timeout=_REPLY_TIMEOUT_S)
    try:
        conn.request(
            "GET" if body is None else "POST",
            path,
            body=body,
            headers={"Content-Type": "application/json", TOKEN_HEADER: info.token},
        )
        response = conn.getresponse()
        data = response.read()
    except http.client.HTTPException as e:
        raise OSError(str(e)) from e
    finally:
        conn.close()
    if response.status >= 400:
        raise OSError(f"companion answered HTTP {response.status}")
    return data


def _spawn_companion() -> None:
    import subprocess

    env = {k: v for k, v in os.environ.items() if k not in _SESSION_ENV}
    kwargs: dict[str, object] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(
        [sys.executable, "-m", "voice_copilot", "companion", "--autostarted"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        cwd=Path.home(),
        env=env,
        **kwargs,  # type: ignore[call-overload]
    )


def _take_start_lock() -> Path | None:
    """Two sessions starting together must not spawn two companions."""
    import contextlib

    lock = runtime_dir() / "companion.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        if time.time() - lock.stat().st_mtime > _LOCK_STALE_S:
            lock.unlink()
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except OSError:
        return None
    return lock


def _ensure_companion() -> RuntimeInfo | None:
    import contextlib

    lock = _take_start_lock()
    try:
        if lock is not None:
            _spawn_companion()
        deadline = time.monotonic() + _START_TIMEOUT_S
        while time.monotonic() < deadline:
            info = read_runtime()
            if info is not None:
                return info
            time.sleep(0.1)
        return None
    finally:
        if lock is not None:
            with contextlib.suppress(OSError):
                lock.unlink()


def _forward(cli: str, raw: bytes, event: object) -> bytes:
    import contextlib

    info = read_runtime()
    if info is not None:
        try:
            return _post(info, f"/api/hooks/{cli}", raw)
        except OSError:
            # Left behind by a crash. Drop it, or every later hook of this
            # session pays for a refused connection, which is seconds on Windows.
            with contextlib.suppress(OSError):
                runtime_file().unlink()
    if event != "SessionStart":
        return b""
    # Only a new session may start the companion. Mid-session, a missing
    # companion means the user quit it, and it must stay quit.
    info = _ensure_companion()
    if info is None:
        raise OSError(f"the companion did not start within {_START_TIMEOUT_S:.0f} s")
    return _post(info, f"/api/hooks/{cli}", raw)


def _live_companion() -> RuntimeInfo | None:
    import socket

    info = read_runtime()
    if info is None:
        return None
    try:
        socket.create_connection((info.host, info.port), timeout=1.0).close()
    except OSError:
        return None
    return info


def _panel_url(info: RuntimeInfo) -> str:
    return f"http://{info.host}:{info.port}/"


def apply_switch(action: str, *, by_agent: bool = False, session_id: str | None = None) -> str:
    """Turn Voice Copilot on or off, or report on it. Returns the words for the user.

    With a session, for that session. Without one, everywhere.
    """
    lang = language()
    if action == "on":
        if session_id:
            set_session_switched_off(session_id, False)
        else:
            set_switched_off(False)
        info = _live_companion() or _ensure_companion()
        if info is None:
            return say("on_no_panel", lang)
        return say("on_here" if session_id else "on_all", lang, url=_panel_url(info))
    if action == "off":
        info = _live_companion()
        if info is not None:
            body = json.dumps({"by_agent": by_agent, "session_id": session_id}).encode()
            try:
                refused = json.loads(_post(info, "/api/switch", body) or b"{}").get("refused")
            except (OSError, ValueError):
                refused = None  # A companion that can't answer can't object either.
            if refused:
                return str(refused)
        if session_id:
            set_session_switched_off(session_id, True)
            return say("off_here", lang)
        set_switched_off(True)
        return say("off_all", lang)
    if is_switched_off(session_id):
        return say("status_off", lang)
    info = _live_companion()
    if info is None:
        return say("status_waiting", lang)
    return say("status_on", lang, url=_panel_url(info))


def run_command(command: Command, *, session_id: str | None = None) -> dict[str, Any]:
    """Carry out a `vc` word: `{"text": what to show, "choices": what to pick from, if any}`."""
    lang = language()
    if command.verb == "help":
        return {"text": say("menu", lang)}
    if command.verb in ("on", "off"):
        # From a terminal there is no "this session" to mean.
        scope = None if command.args else session_id
        return {"text": apply_switch(command.verb, session_id=scope)}
    if command.verb == "status":
        return {"text": apply_switch("status", session_id=session_id)}
    if is_switched_off(session_id):
        return {"text": say("status_off", lang)}
    info = _live_companion()
    if info is None:
        return {"text": say("no_companion", lang)}
    body = json.dumps(
        {"verb": command.verb, "args": list(command.args), "session_id": session_id}
    ).encode()
    try:
        answer = json.loads(_post(info, "/api/command", body))
    except (OSError, ValueError) as e:
        return {"text": say("failed", lang, error=e)}
    return answer if isinstance(answer, dict) else {"text": str(answer)}


def _requested_switch(payload: dict[str, Any]) -> tuple[Command, bool] | None:
    """The command this hook carries, and whether the agent asked rather than the user."""
    event = payload.get("hook_event_name")
    if event == "UserPromptSubmit":
        command = parse(str(payload.get("prompt") or ""))
        return (command, False) if command is not None else None
    if event == "PreToolUse":
        tool_input = payload.get("tool_input")
        text = tool_input.get("command") if isinstance(tool_input, dict) else None
        if not isinstance(text, str) or "voice-copilot" not in text:
            return None
        import re

        match = re.fullmatch(_AGENT_SWITCH, text)
        return (Command(match.group(1)), True) if match else None
    return None


def _turn_is_running(cli: str, payload: dict[str, Any]) -> bool:
    """Whether the agent is mid-turn. Only asked where the answer changes the reply."""
    import urllib.parse

    info = _live_companion()
    if cli not in _BLOCKING_ENDS_THE_TURN or info is None:
        return False
    sid = urllib.parse.quote(str(payload.get("session_id")))
    try:
        answer = _post(info, f"/api/hooks/{cli}/working?session_id={sid}", None)
        return bool(json.loads(answer).get("working"))
    except (OSError, ValueError):
        return False


def _switch_reply(
    cli: str, event: object, verb: str, by_agent: bool, text: str, *, mid_turn: bool = False
) -> dict[str, Any]:
    if mid_turn:
        return {
            "systemMessage": text,
            "hookSpecificOutput": {"hookEventName": event, "additionalContext": _IGNORE_THE_SWITCH},
        }
    if event == "UserPromptSubmit":
        reply: dict[str, Any] = {"decision": "block", "reason": text}
        if cli == "claude":
            # Its block message otherwise repeats "Original prompt: vc off".
            reply["hookSpecificOutput"] = {
                "hookEventName": event,
                "suppressOriginalPrompt": True,
            }
        return reply
    # The command never runs: the agent reads the result as the refusal's reason.
    reply = {
        "hookSpecificOutput": {
            "hookEventName": event,
            "permissionDecision": "deny",
            "permissionDecisionReason": f"The plugin carried this out in place of the command, so it did work and there is nothing to retry. {text}",
        }
    }
    if by_agent and verb == "off":
        # Whoever watches the agent should know when the agent turned the watch off.
        reply["systemMessage"] = f"The agent ran the switch. {text}"
    return reply


def _chat(args: list[str]) -> int:
    """`--chat <cli> [--session <id>] -- <text>`: for plugins that catch prompts themselves."""
    session_id = args[args.index("--session") + 1] if "--session" in args else None
    text = " ".join(args[args.index("--") + 1 :]) if "--" in args else ""
    command = parse(text)
    if command is None:
        return _NOT_A_COMMAND_EXIT
    sys.stdout.write(json.dumps(run_command(command, session_id=session_id or None)))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if os.environ.get(NO_HOOKS_ENV):
        return 0
    if args and args[0] == "--chat":
        return _chat(args[1:])
    cli = args[0] if args else "unknown"
    raw = sys.stdin.buffer.read()
    try:
        payload = json.loads(raw)
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    event = payload.get("hook_event_name")
    session_id = payload.get("session_id")
    session_id = session_id if isinstance(session_id, str) and session_id else None
    switch = _requested_switch(payload)
    if switch is not None:
        command, by_agent = switch
        # Asked first: turning it off lets go of the session.
        mid_turn = event == "UserPromptSubmit" and _turn_is_running(cli, payload)
        if by_agent:
            text = apply_switch(command.verb, by_agent=True, session_id=session_id)
        else:
            text = str(run_command(command, session_id=session_id).get("text"))
        answer = _switch_reply(cli, event, command.verb, by_agent, text, mid_turn=mid_turn)
        sys.stdout.write(json.dumps(answer))
        return 0
    if is_switched_off(session_id):
        return 0  # Before anything can start the companion or reach it.
    try:
        reply = _forward(cli, raw, event)
    except OSError as e:
        if event == "SessionStart":
            message = f"Voice Copilot is not narrating this session: {e}"
            sys.stdout.write(json.dumps({"systemMessage": message}))
        return 0
    if reply:
        sys.stdout.write(reply.decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
