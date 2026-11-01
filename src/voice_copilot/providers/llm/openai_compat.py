"""OpenAI-compatible local endpoints (Ollama, LM Studio, llama.cpp server).

Thin subclass of the OpenAI provider pointing at a local base_url.
"""

from __future__ import annotations

import os

from voice_copilot.core.secrets import get_secret
from voice_copilot.providers.llm.openai import OpenAIProvider
from voice_copilot.providers.registry import register


@register("llm", "openai-compat")
class OpenAICompatProvider(OpenAIProvider):
    name = "openai-compat"

    def __init__(
        self,
        model: str = "llama3.1",
        base_url: str | None = None,
        api_key: str | None = None,
        reasoning_effort: str | None = "none",
    ) -> None:
        base = base_url or os.getenv("OPENAI_COMPAT_BASE_URL") or "http://127.0.0.1:11434/v1"
        key = api_key or get_secret("OPENAI_COMPAT_API_KEY") or "local"
        super().__init__(model=model, api_key=key, base_url=base)
        # A thinking model on Ollama spends the whole budget on reasoning and returns no
        # text. That goes for the supervisor too: with 4000 tokens to think in, qwen3.5:9b
        # still left 9 of 26 checkpoints without a verdict. Empty string lets a model think.
        if reasoning_effort:
            self._request_options["reasoning_effort"] = reasoning_effort
