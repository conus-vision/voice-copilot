"""Where the running companion can be found: host, port and a per-run token.

The hook process and the server are unrelated processes, so they meet through a
small file next to the config. The token keeps a web page from posting to the
hook endpoint: its replies are injected into a live coding agent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import NamedTuple

from platformdirs import user_config_path

#: Set in every process VC spawns or wraps. Hooks seen with it are VC's own
#: narrator calling `claude -p`, or a CLI already narrated through the proxy.
NO_HOOKS_ENV = "VOICE_COPILOT_NO_HOOKS"
#: Overrides the directory of the runtime file, for tests and isolated runs.
RUNTIME_DIR_ENV = "VOICE_COPILOT_RUNTIME_DIR"
TOKEN_HEADER = "X-Voice-Copilot-Token"


# A tuple rather than a dataclass: importing dataclasses costs every hook 40 ms.
class RuntimeInfo(NamedTuple):
    host: str
    port: int
    token: str
    pid: int


def runtime_dir() -> Path:
    override = os.environ.get(RUNTIME_DIR_ENV)
    return Path(override) if override else user_config_path("voice-copilot", appauthor=False)


def runtime_file() -> Path:
    return runtime_dir() / "companion.json"


def _session_switch(session_id: str) -> Path:
    # Plugins in other languages look at the same path: keep this rule simple.
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in session_id)
    return runtime_dir() / "sessions" / safe


def is_switched_off(session_id: str | None = None) -> bool:
    """Whether Voice Copilot is off for this session, or for all if none is given.

    `vc off` stops one session, `vc off all` stops them all and every new one, and
    `vc on` in a session overrides the latter. Both stick until changed.
    """
    if session_id:
        try:
            return _session_switch(session_id).read_text(encoding="ascii") == "off"
        except (OSError, ValueError):
            pass  # No word about this session in particular.
    return (runtime_dir() / "switched-off").exists()


def set_switched_off(off: bool) -> None:
    """Turn it on or off everywhere, whatever individual sessions were set to."""
    from shutil import rmtree

    path = runtime_dir() / "switched-off"
    rmtree(runtime_dir() / "sessions", ignore_errors=True)
    if off:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    else:
        path.unlink(missing_ok=True)


def set_session_switched_off(session_id: str, off: bool) -> None:
    path = _session_switch(session_id)
    if not off and not (runtime_dir() / "switched-off").exists():
        path.unlink(missing_ok=True)  # On is what a session without a word gets.
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("off" if off else "on", encoding="ascii")


def forget_old_session_switches(*, older_than_s: float = 7 * 24 * 3600.0) -> None:
    """A session's word has no other way to go: its hooks stop once it is off."""
    from contextlib import suppress
    from time import time

    with suppress(OSError):
        for path in (runtime_dir() / "sessions").iterdir():
            if time() - path.stat().st_mtime > older_than_s:
                path.unlink(missing_ok=True)


def language() -> str:
    """The language to answer in, from the config file, without loading the config."""
    import re

    try:
        text = (runtime_dir() / "config.yaml").read_text(encoding="utf-8")
    except OSError:
        return "en"
    found = re.search(r"^commentator_language:\s*['\"]?(\w+)", text, re.MULTILINE)
    return found.group(1) if found else "en"


def read_runtime() -> RuntimeInfo | None:
    try:
        doc = json.loads(runtime_file().read_text(encoding="utf-8"))
        return RuntimeInfo(
            host=str(doc["host"]),
            port=int(doc["port"]),
            token=str(doc["token"]),
            pid=int(doc["pid"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_runtime(info: RuntimeInfo) -> None:
    from contextlib import suppress

    path = runtime_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(info._asdict()), encoding="utf-8")
    # Owner-only on POSIX; on Windows the profile directory's ACL already is.
    with suppress(OSError):
        tmp.chmod(0o600)
    os.replace(tmp, path)


def clear_runtime(*, token: str) -> None:
    """Remove the runtime file, but only if it still describes this run."""
    from contextlib import suppress

    current = read_runtime()
    if current is not None and current.token == token:
        with suppress(OSError):
            runtime_file().unlink()
