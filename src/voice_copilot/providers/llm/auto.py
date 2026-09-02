"""`auto` commentator provider — generate narration by shelling out to the
same CLI the user launched via `vc`, using that CLI's own auth (no keys).

Generalises the copilot-cli pattern via a per-CLI narration-profile table.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
from collections.abc import AsyncIterator, Sequence

from voice_copilot.commentator.cli_profiles import (
    NARRATION_PROFILES,
    build_narration_command,
    profile_needs_system_file,
)
from voice_copilot.providers.llm._cli_runner import build_flat_prompt, run_cli
from voice_copilot.providers.llm.base import LLMMessage, LLMProvider
from voice_copilot.providers.registry import register

log = logging.getLogger(__name__)


def _explain_silence(stderr: str) -> str:
    """The most useful line of a failed CLI run, for the popup and the trace.

    Agent CLIs report a rejected model or a bad login on stderr and still exit
    0, so the only signal we get is empty stdout. Surfacing their own words
    beats a bare "no narration" the user cannot act on.
    """
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    for line in reversed(lines):
        if line.lower().startswith(("error", "warning: model")):
            return line[:400]
    return lines[-1][:400] if lines else "no output on stdout or stderr"


@register("llm", "auto")
class AutoCommentatorProvider(LLMProvider):
    name = "auto"
    prompt_style = "cli"

    def __init__(
        self, cli: str | None = None, binary: str | None = None, model: str | None = None
    ) -> None:
        self._cli = cli
        self._binary = binary
        self._model = model

    async def stream_chat(
        self,
        messages: Sequence[LLMMessage],
        *,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.4,
    ) -> AsyncIterator[str]:
        if not self._cli or not self._binary:
            raise RuntimeError(
                "auto commentator: no launched CLI to narrate with — run via "
                "`vc <cli>`, or pick a provider in the Commentator tab."
            )
        if self._cli not in NARRATION_PROFILES:
            raise RuntimeError(
                f"auto commentator: no narration profile for '{self._cli}' — "
                f"pick a provider in the Commentator tab."
            )
        user_content = build_flat_prompt(None, messages)
        if not user_content:
            return

        # Some CLIs (claude) need the system prompt in a temp file so the big
        # multi-line arg survives the Windows .cmd batch layer.
        system_path: str | None = None
        if profile_needs_system_file(self._cli):
            fd, system_path = tempfile.mkstemp(suffix=".txt", prefix="vc-narr-sys-")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(system or "")
        try:
            argv, stdin_text = build_narration_command(
                self._cli,
                self._binary,
                system,
                user_content,
                model=self._model,
                system_file_path=system_path,
            )
            log.info(
                "auto(%s): %s",
                self._cli,
                " ".join(argv if stdin_text is not None else argv[:-1]),
            )
            loop = asyncio.get_running_loop()
            stdout, stderr = await loop.run_in_executor(
                None, lambda: run_cli(argv, stdin_text=stdin_text, timeout=60.0)
            )
        finally:
            if system_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(system_path)

        if stderr:
            log.debug("auto(%s) stderr: %s", self._cli, stderr[:400])
        text = (stdout or "").strip()
        if not text:
            # Fail loud: silence here used to look exactly like "the commentator
            # never started", with the real cause (a model the account cannot
            # use, an expired login) buried in the log file.
            raise RuntimeError(
                f"auto commentator: {self._cli} narrated nothing — {_explain_silence(stderr)}"
            )
        yield text
