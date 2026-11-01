"""Reach a CLI's idle prompt from outside, as a real user message.

A hook can only answer while the agent is running, so an idle session needs another
way in. Codex has one: `codex queue` hands a running interactive session a message,
which starts a new turn. Other CLIs have none yet, and wait for the next prompt.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

Waker = Callable[[str, str], Awaitable[None]]

_QUEUE_TIMEOUT_S = 30.0

#: The script an npm shim runs: `"%dp0%\node_modules\@openai\codex\bin\codex.js" %*`.
_SHIM_SCRIPT = re.compile(r'"%dp0%\\([^"]+\.js)"')


def _direct_command(binary: str) -> list[str]:
    """How to run a CLI so that nothing re-parses its arguments.

    On Windows `codex` is usually an npm `.cmd` shim, and cmd.exe would read the
    user's words as a command line: `%PATH%` expands, a quote followed by `&` starts
    another command. So we start what the shim starts, and refuse if we can't tell.
    """
    if not binary.lower().endswith((".cmd", ".bat")):
        return [binary]
    shim = Path(binary)
    match = _SHIM_SCRIPT.search(shim.read_text(encoding="utf-8", errors="replace"))
    node = shutil.which("node")
    if match is None or node is None:
        raise RuntimeError(f"can't pass a message safely through {shim.name}")
    return [node, str(shim.parent / match.group(1))]


async def wake_codex(thread: str, text: str) -> None:
    binary = shutil.which("codex")
    if binary is None:
        raise RuntimeError("codex is not on PATH")
    proc = await asyncio.create_subprocess_exec(
        *_direct_command(binary),
        "queue",
        "--thread",
        thread,
        "--message",
        text,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_QUEUE_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        raise RuntimeError(f"codex queue did not answer in {_QUEUE_TIMEOUT_S:.0f} s") from None
    if proc.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"codex queue failed: {detail[-300:]}")


WAKERS: dict[str, Waker] = {"codex": wake_codex}
