"""Shared helpers for CLI-subprocess LLM providers (copilot-cli, auto)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence

from voice_copilot.core.child_env import child_env
from voice_copilot.providers.llm.base import LLMMessage


async def run_cli(
    cmd: list[str], *, stdin_text: str | None = None, timeout: float = 60.0
) -> tuple[str, str]:
    """Run `cmd` to completion. If `stdin_text` is given, feed it then close
    stdin (signals end-of-input for interactive CLIs). Returns decoded
    (stdout, stderr). Raises RuntimeError on timeout.

    The child is killed on timeout and on cancellation alike: a narration
    call in flight when the user quits must not hold the exit for a minute.
    Without `stdin_text` the child gets no stdin at all, never the terminal,
    which under `vc` belongs to the wrapped agent.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=child_env(),
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(stdin_text.encode("utf-8") if stdin_text is not None else None),
            timeout=timeout,
        )
    except TimeoutError:
        await _kill(proc)
        raise RuntimeError(f"cli runner: timeout after {timeout}s") from None
    except BaseException:
        await _kill(proc)
        raise
    return (
        out.decode("utf-8", errors="replace") if out else "",
        err.decode("utf-8", errors="replace") if err else "",
    )


async def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    with contextlib.suppress(Exception):
        await asyncio.shield(proc.wait())


def build_flat_prompt(system: str | None, messages: Sequence[LLMMessage]) -> str:
    """Concatenate system + user/assistant messages into a single flat string.

    Bracket section labels are intentionally avoided by callers for CLI mode
    (they trigger file-search behaviour in some agent CLIs).
    """
    parts: list[str] = []
    if system:
        parts.append(system)
    for m in messages:
        if m.role == "user":
            parts.append(m.content)
        elif m.role == "assistant" and m.content:
            parts.append(f"[assistant]: {m.content}")
    return "\n\n".join(p.strip() for p in parts if p.strip())
