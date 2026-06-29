"""Shared helpers for CLI-subprocess LLM providers (copilot-cli, auto)."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

from voice_copilot.providers.llm.base import LLMMessage


def run_cli(
    cmd: list[str], *, stdin_text: str | None = None, timeout: float = 60.0
) -> tuple[str, str]:
    """Run `cmd` to completion. If `stdin_text` is given, feed it then close
    stdin (signals end-of-input for interactive CLIs). Returns decoded
    (stdout, stderr). Raises RuntimeError on timeout.
    """
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if stdin_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        out, err = proc.communicate(
            input=stdin_text.encode("utf-8") if stdin_text is not None else None,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise RuntimeError(f"cli runner: timeout after {timeout}s") from None
    return (
        out.decode("utf-8", errors="replace") if out else "",
        err.decode("utf-8", errors="replace") if err else "",
    )


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
