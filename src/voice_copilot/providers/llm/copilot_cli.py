"""GitHub Copilot CLI subprocess provider.

Delegates to the `copilot` binary (GitHub Copilot CLI v1.x).
No token extraction required — the CLI manages its own auth.

IMPORTANT: we feed the prompt via STDIN (interactive mode), NOT via `-p`.
Tests show that `-p` mode triggers the code-agent system prompt — the model
searches the repo for section labels and never reads the provided events.
Stdin / interactive mode treats the message as a chat turn and follows the
narration instructions correctly.

`-s / --silent` suppresses the TUI chrome. `--no-auto-update` avoids a
version-check round-trip. `stdin` is closed after writing, which signals
end-of-input so copilot processes one message and exits.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import AsyncIterator, Sequence

from voice_copilot.providers.llm.base import LLMMessage, LLMProvider
from voice_copilot.providers.registry import register

log = logging.getLogger(__name__)

_BASE_FLAGS = [
    "--allow-all",  # skip tool-permission prompts
    "--no-auto-update",  # don't check for updates mid-narration
    "-s",  # silent: model response only
]


def _find_copilot() -> str | None:
    return shutil.which("copilot")


def _make_cmd(binary: str, model: str) -> list[str]:
    """Return command list; wrap .cmd/.bat in cmd.exe /C on Windows."""
    import sys

    args = ["--model", model, *_BASE_FLAGS]
    if sys.platform == "win32" and binary.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/C", binary, *args]
    return [binary, *args]


def _build_prompt(system: str | None, messages: Sequence[LLMMessage]) -> str:
    """Concatenate system + user messages into a single flat string.

    Section labels like [NEW_EVENTS] are intentionally avoided here — they
    trigger file-search behaviour when passed via -p. The actual section
    content comes from the caller (format.py uses non-bracket headers for CLI).
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


@register("llm", "copilot-cli")
class CopilotCLIProvider(LLMProvider):
    """Commentator LLM via copilot CLI stdin (interactive mode)."""

    name = "copilot-cli"
    prompt_style = "cli"

    def __init__(
        self,
        model: str = "gpt-5-mini",
        copilot_bin: str | None = None,
    ) -> None:
        self._model = model
        self._bin = copilot_bin or _find_copilot()
        if not self._bin:
            raise RuntimeError(
                "GitHub Copilot CLI not found on PATH. "
                "Install from https://github.com/cli/cli and run `copilot login`."
            )
        log.info("copilot-cli: bin=%s model=%s", self._bin, self._model)

    async def stream_chat(
        self,
        messages: Sequence[LLMMessage],
        *,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.4,
    ) -> AsyncIterator[str]:
        prompt = _build_prompt(system, messages)
        if not prompt:
            return

        assert self._bin is not None
        cmd = _make_cmd(self._bin, self._model)
        log.debug("copilot-cli: cmd=%s", cmd[:4])

        loop = asyncio.get_running_loop()
        try:
            stdout, stderr = await loop.run_in_executor(
                None,
                _run_via_stdin,
                cmd,
                prompt,
            )
        except Exception as e:
            raise RuntimeError(f"copilot-cli: subprocess failed: {e}") from e

        if stderr:
            log.debug("copilot-cli stderr: %s", stderr[:400])
        text = (stdout or "").strip()
        if not text:
            log.warning("copilot-cli: empty response (model=%s)", self._model)
            return
        log.info("copilot-cli: response %d chars", len(text))
        yield text


def _run_via_stdin(cmd: list[str], prompt: str) -> tuple[str, str]:
    """Feed prompt via stdin; close stdin to signal end-of-input."""
    import subprocess

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = proc.communicate(
            input=prompt.encode("utf-8"),
            timeout=60,
        )
        return stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise RuntimeError("copilot-cli: timeout after 60 s") from None
