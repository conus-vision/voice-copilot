"""`auto` commentator provider — generate narration by shelling out to the
same CLI the user launched via `vc`, using that CLI's own auth (no keys).

Generalises the copilot-cli pattern via a per-CLI narration-profile table.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence

from voice_copilot.commentator.cli_profiles import build_narration_command
from voice_copilot.providers.llm._cli_runner import build_flat_prompt, run_cli
from voice_copilot.providers.llm.base import LLMMessage, LLMProvider
from voice_copilot.providers.registry import register

log = logging.getLogger(__name__)


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
        prompt = build_flat_prompt(system, messages)
        if not prompt:
            return
        try:
            argv, stdin_text = build_narration_command(
                self._cli, self._binary, prompt, model=self._model
            )
        except KeyError:
            raise RuntimeError(
                f"auto commentator: no narration profile for '{self._cli}' — "
                f"pick a provider in the Commentator tab."
            ) from None

        # Log the command (minus the prompt arg) so the session log shows what
        # actually ran — useful when tuning per-CLI narration profiles.
        log.info("auto(%s): %s", self._cli, " ".join(argv if stdin_text is not None else argv[:-1]))
        loop = asyncio.get_running_loop()
        stdout, stderr = await loop.run_in_executor(
            None, lambda: run_cli(argv, stdin_text=stdin_text, timeout=60.0)
        )
        if stderr:
            log.debug("auto(%s) stderr: %s", self._cli, stderr[:400])
        text = (stdout or "").strip()
        if not text:
            log.warning("auto(%s): empty narration response", self._cli)
            return
        yield text
