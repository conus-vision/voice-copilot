"""Forward one hook call from a coding CLI to Voice Copilot.

    voice-copilot-hook <dialect> [event] [--cli NAME]

A CLI runs this for each hook it fires (Codex, Gemini CLI, Copilot CLI, Qwen
Code, ...). It reads the hook's JSON on stdin, posts it to the running Voice
Copilot, prints the reply for the CLI and always exits 0. When Voice Copilot
is not running the post fails at once and the agent carries on as if the hook
were not there.

Where to post comes from the environment a Voice Copilot launch sets for the
CLI: ``VOICE_COPILOT_URL`` (default ``http://127.0.0.1:8765/api/companion/v1``),
``VOICE_COPILOT_LAUNCH`` and ``VOICE_COPILOT_MODE``. ``VOICE_COPILOT_TOKEN``,
when you set one for the panel, goes along as a header.
``VOICE_COPILOT_HOOKS=off`` silences it without uninstalling.

Standard library only, and nothing from the rest of the package: the CLI
starts this once per hook, so it has to start fast.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8765/api/companion/v1"
#: Tool gates: a paused agent is held here until the user resumes it.
_GATE_EVENTS = frozenset({"pretooluse", "beforetool"})
_GATE_TIMEOUT_S = 3600.0
_TIMEOUT_S = 30.0


def _event_name(explicit: str | None, raw: bytes) -> str:
    if explicit:
        return explicit
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return ""
    if isinstance(body, dict):
        name = body.get("hook_event_name") or body.get("hookEventName") or ""
        return str(name)
    return ""


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cli: str | None = None
    if "--cli" in args:
        i = args.index("--cli")
        cli = args[i + 1] if i + 1 < len(args) else None
        del args[i : i + 2]
    if "--tag" in args:  # marks our entries in the CLI's config; nothing to do
        i = args.index("--tag")
        del args[i : i + 2]
    dialect = args[0] if args else "claude"
    event = args[1] if len(args) > 1 else None
    raw = sys.stdin.buffer.read()
    if os.environ.get("VOICE_COPILOT_HOOKS", "").lower() in ("off", "0", "false"):
        return 0

    query = {"cli": cli or dialect}
    if event:
        query["event"] = event
    for key, env in (("launch", "VOICE_COPILOT_LAUNCH"), ("mode", "VOICE_COPILOT_MODE")):
        value = os.environ.get(env)
        if value:
            query[key] = value
    base = (os.environ.get("VOICE_COPILOT_URL") or DEFAULT_URL).rstrip("/")
    url = f"{base}/hooks/{urllib.parse.quote(dialect)}?{urllib.parse.urlencode(query)}"
    name = _event_name(event, raw).replace("_", "").lower()
    timeout = _GATE_TIMEOUT_S if name in _GATE_EVENTS else _TIMEOUT_S

    headers = {"Content-Type": "application/json"}
    token = os.environ.get("VOICE_COPILOT_TOKEN", "").strip()
    if token:
        headers["X-Voice-Copilot-Token"] = token
    request = urllib.request.Request(url, data=raw or b"{}", method="POST", headers=headers)
    # Loopback only: never route this through HTTP(S)_PROXY.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            reply = response.read()
    except Exception:
        return 0
    if reply.strip():
        sys.stdout.buffer.write(reply)
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
